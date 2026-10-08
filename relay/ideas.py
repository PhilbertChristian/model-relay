"""Your local AI chat history, mined for work: opt-in, local-only, redacted.

    mine(cfg)              -> {project_dir: [Idea]} best first; {} unless cfg["ideas"]["enabled"]; "" = no known repo
    mine(cfg, kinds=KINDS) -> the same plus principles, history and summaries, for chat search (never swarm tasks)
    classify(text)         -> "idea" | "plan" | "principle" | "history" | "summary"
    digest(ideas_by_dir)   -> {kind: [line]} for a CHATS.md;  report(ideas_by_dir) -> the same as colored text

Sources: Claude Code transcripts (<root>/*/*.jsonl), Codex sessions (<root>/**/*.jsonl) and a ChatGPT export
(conversations.json, its folder or the export .zip). Kinds come from cheap phrase heuristics:
  idea       something you meant to do and didn't ("we should…", "TODO", "later", "would be nice", "follow up"),
             or an assistant "Next steps:" item you never took up
  plan       a future plan you stated ("next step: …", "the plan is …", "tomorrow we'll …")
  principle  a design principle or decision ("always/never/prefer/avoid …", "we decided …", "Decision: …")
  history    what an agent reports it did ("Fixed …", "Added …"), per project and date
  summary    session titles and "Summary:" / "TL;DR:" lines
Questions, code and pasted logs are dropped. Transcripts never leave this machine and nothing here touches the
network: only redacted strings of at most 200 chars come out (Idea has no kind field; classify() recovers it).
"""
from __future__ import annotations

import functools
import json
import os
import re
import time
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import ui
from .contracts import Idea, is_secret_file, redact
from .discover import DATA_DIR, VERBS, _cut, _fit, _inside

KINDS = ("idea", "plan", "principle", "history", "summary")
ACTIONABLE = ("idea", "plan")            # what mine() returns by default: work a swarm agent can pick up
TITLES = {"idea": "abandoned ideas", "plan": "future plans", "principle": "design principles", "history": "history",
          "summary": "summaries"}
WEIGHT = {"idea": 20, "plan": 22, "principle": 15, "history": 10, "summary": 8}     # added to a 0-60 recency score
MAX_IDEA = 200
MAX_ROW = 2_000_000                      # a JSONL row this big is an image or a tool dump, not something you typed
MAX_EXPORT = 512_000_000

# what is not a person typing: injected context, hooks, command echoes, interruptions, compaction notes
_TAGGED = re.compile(r"<(system-reminder|command-[\w-]+|local-command-[\w-]+|bash-[\w-]+|user-prompt-submit-hook|"
                     r"environment_context|user_instructions|task-notification)>.*?</\1>", re.S)
_NOT_TYPED = re.compile(r"\s*(?:<[a-z][\w-]*[\s>/]|\[Request interrupted|Caveat: |This session is being continued|"
                        r"API Error|Base directory for this skill|# AGENTS\.md instructions)", re.I)
_AGENT = re.compile(r"\s*You are (?:one of several agents spending|working an unattended overnight shift)")  # our lanes
_CWD_TAG = re.compile(r"<cwd>\s*([^<\n]+?)\s*</cwd>")

# prose vs. code, logs and questions
_FENCE = re.compile(r"\s*(```|~~~)")
_LOGGY = re.compile(r"\s*(?:\[?\d{4}-\d\d-\d\d[T ]\d\d:\d\d|\[?(?:INFO|DEBUG|WARN(?:ING)?|ERROR|TRACE|FATAL|CRITICAL)\b|"
                    r"Traceback \(|File \"|at [\w.$<>]+ ?\(|npm (?:ERR|WARN)|error(?:\[\w+\])?:|warning:|\$ |>>> |"
                    r"[A-Z]:\\|/[\w.-]+/[\w./-]+:\d+)")
_LIST = re.compile(r"^(?:[-*•+]|\d+[.)])\s+(?:\[ \]\s+)?")
_ITEM = re.compile(r"\s{0,3}(?:[-*•+]|\d+[.)])\s+(?:\[ \]\s+)?(?P<t>.+)")
_SUBITEM = re.compile(r"\s+(?:[-*•+]|\d+[.)])\s+")
_SENT = re.compile(r"(?<=[.!?;])\s+")
_BOUNDARY = re.compile(r",\s*(?:(?:and|but|so|then)\s+)?|;\s*|\s+but\s+|:\s+")
_QUESTION = re.compile(r"\?\s*$|^(?:how|what|whats|what's|why|where|which|who|when)\b|^(?:is|are|was|were|does|did|"
                       r"can|could|would|will|should|shall|may|might|have|has)\s+(?:i|you|we|it|this|that|there|they|the)\b"
                       r"|^do\s+(?:i|you|we|they)\b", re.I)     # "do the migration" is an instruction, not a question

# kinds: classify() and the extractors share these, so a mined line always classifies as what it was mined as
_DONE_VERBS = (r"added|fixed|implemented|created|updated|renamed|removed|deleted|refactored|moved|replaced|switched|wired|"
               r"hooked|converted|migrated|extracted|cleaned|configured|installed|completed|finished|shipped|wrote|rewrote|"
               r"built|ran|introduced|improved|optimi[sz]ed|documented|tested|merged|released|deployed|upgraded|bumped|"
               r"simplified|reworked|ported|enabled|disabled|changed|adjusted|tweaked|resolved|debugged|investigated|"
               r"reviewed|verified|worked|drafted|designed|prototyped|integrated|exposed|restored|reverted|polished|"
               r"consolidated|generated|scaffolded|patched|hardened|tightened|trimmed|tuned")
_HISTORY = re.compile(rf"(?:{_DONE_VERBS})\b", re.I)                       # .match: "Fixed the rounding bug…"
_DONE = re.compile(rf"\b(?:{_DONE_VERBS}|done|split|set up)\b", re.I)       # .search: the work got done
_DID = re.compile(r"^(?:(?:i|we)(?:'ve| have)?\s+(?:also\s+)?|(?:done|all set|great|ok(?:ay)?)\b[\s:,!.—–-]+)+", re.I)
_LABEL = re.compile(r"^(?:summary|tl;?dr)\s*:\s*", re.I)
_PRINCIPLE = re.compile(r"(?:always|never(?! mind)|prefer|avoid)\b|(?:design |key )?(?:principle|decision|rule|"
                        r"convention|guideline)s?\s*[:\-–—]", re.I)
_DECIDED = re.compile(r"\b(?:we|i)\s+(?:have\s+)?(?:decided|agreed|chose|settled on|went with|standardi[sz]ed on)\b|"
                      r"\b(?:by design|as a rule|rule of thumb|non-negotiable)\b", re.I)
_PLAN_LEAD = re.compile(r"(?:next steps?|the plan|my plan|our plan|plan|roadmap|phase \d+|milestone(?: \d+)?|"
                        r"v\d+(?:\.\d+)*)(?:\s+(?:is|are|would be|will be)\b|\s*[:\-–—])", re.I)
_WHEN = r"(?:tomorrow|next (?:week|sprint|month|release)|this weekend|in v\d+)"
_WILL = r"(?:i'll|we'll|i will|we will|let's)"
_PLAN_ANY = re.compile(rf"\b(?:the plan is|(?:i|we)(?:'m|'re| am| are) planning to|(?:i|we) plan to)\b|"
                       rf"\b{_WHEN}\b.*\b{_WILL}|\b{_WILL}.*\b{_WHEN}\b", re.I)

# ideas
_STRONG = (re.compile(r"(?:\bTODO\b|(?i:\btodo\b)(?=\s*[:\-–—]))\s*[:\-–—]?\s*(?P<a>.+)"),
           re.compile(r"\b(?P<a>follow[ -]?up\s+(?:on|with)\b.+)", re.I),
           re.compile(r"\bwould\s+be\s+(?:really\s+|very\s+|so\s+)?(?:nice|great|cool|good|useful|handy|neat)\s+"
                      r"(?:to\s+(?:also\s+)?(?P<a>.+)|if\b.+)", re.I),
           re.compile(r"\b(?:remind me to|don'?t (?:let me )?forget to|note to self:?)\s+(?P<a>.+)", re.I))
_SHOULD = re.compile(r"\b(?:we|i)\s+(?:(?:really|probably|also|eventually|still|definitely|later)\s+)*"
                     r"(?:should|ought to)\s+(?P<a>.+)", re.I)
_LATER = re.compile(r"\b(?:later(?: on)?|eventually|at some point|some ?day|in the future|down the road|next time|"
                    r"in a follow-?up)\b", re.I)
_DEFER = re.compile(r"[\s,(]*\b(?:later(?: on)?|eventually|at some point|some ?day|in the future|down the road|next time|"
                    r"in a follow-?up|as well|too)\b[\s,)]*", re.I)
_LEAD = re.compile(r"^(?:(?:ok(?:ay)?|so|and|also|btw|oh|hmm+|well|then|but|anyway|actually|honestly|plus|ah)\b[\s,:-]*)+",
                   re.I)
_FILLER = re.compile(r"^(?:(?:let'?s|let us|we can|we could|we'?ll|we will|i'?ll|i will|i want to|i'?d like to|we need to|"
                     r"i need to|you can|please|just|maybe|probably|perhaps|to|we|i)\b[\s,:-]*)+", re.I)

# assistant sections and replies
_HEAD_TAIL = r"\s*(?:\([^)\n]{0,40}\))?\s*(?:\*\*|__)?\s*:?\s*(?:\*\*|__)?\s*$"
_NEXT_HEAD = re.compile(r"(?:#{1,6}\s*)?(?:\*\*|__)?\s*(?:suggested |possible |recommended |potential |optional |remaining )?"
                        r"(?:next steps?|follow[- ]?ups?|future (?:work|improvements?)|todos?|to-?dos|remaining (?:work|"
                        r"items|tasks)|what'?s left|left to do|ideas for later|improvements to consider)" + _HEAD_TAIL, re.I)
_DECISION_HEAD = re.compile(r"(?:#{1,6}\s*)?(?:\*\*|__)?\s*(?:key |design |architecture |architectural )?(?:decisions|"
                            r"principles|conventions|trade-?offs)(?: made)?" + _HEAD_TAIL, re.I)
_LABELED = re.compile(r"(?:(?:suggested |recommended )?(?P<next>next steps?)|(?:design |key )?(?P<dec>decision|principle)s?"
                      r"|(?P<sum>summary|tl;?dr))\s*[:\-–—]\s*(?P<v>\S.*)", re.I)
_OFFER = re.compile(r"(?:if you(?:'d)? (?:want|like),?\s*)?(?:i|we) (?:can|could)\s+(?:also\s+)?", re.I)
_BORING = re.compile(r"(?:run|re-?run|review|check|verify|commit|push|deploy|restart|merge|monitor|test (?:it|this|the "
                     r"changes)|try (?:it|this|running)|open a pr|create a pr|let me know)\b", re.I)
_YES = re.compile(r"\W*(?:y|yes|yep|yeah|yup|sure|ok(?:ay)?|go ahead|go for it|do (?:it|that|this|them|those|all|both|"
                  r"#?\d+)|please do|sounds good|lgtm|ship it|continue|proceed|all of them|both|let'?s do (?:it|that|"
                  r"them|both|all)|#?\d+(?:\s*(?:,|and|&)\s*#?\d+)*)(?:\W|$)", re.I)
_STOP = frozenset("""the a an to of for and or in on at by with from into onto this that these those it its be is are was
were been we i you our your my me us should would could can will just also maybe some more then than so as do does did done
not no yes ok later next step steps todo please make sure have has""".split())


@dataclass
class _Msg:
    role: str                    # "user" | "assistant" | "summary"
    text: str
    ts: float
    cwd: str = ""


def _now() -> float:
    return time.time()


# --------------------------------------------------------------------------- small helpers
def _ts(v) -> float:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v) / (1000 if v > 1e12 else 1)
    if isinstance(v, str) and v.strip():
        try:
            d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    return 0.0


def _text(content) -> str:
    """Text of a message: a string, or the text blocks of a block list (tool calls/results, images skipped)."""
    if isinstance(content, str):
        return content
    blocks = [content] if isinstance(content, dict) else content if isinstance(content, list) else []
    return "\n".join(b if isinstance(b, str) else str(b.get("text") or "") for b in blocks
                     if isinstance(b, str) or (isinstance(b, dict) and b.get("type") in ("text", "input_text", "output_text")))


@functools.lru_cache(maxsize=4096)
def _norm(path: str | None) -> str:
    """Absolute, symlinks resolved (also for a removed worktree's path), so it compares with discover's paths."""
    return os.path.realpath(os.path.expanduser(path)) if path else ""


@functools.lru_cache(maxsize=1024)
def _project_dir(cwd: str) -> str:
    """The repo a conversation ran in (a linked worktree maps to its main checkout), so ideas line up with
    discover's ProjectInfo.path; the plain cwd when there is no repo above it; "" when unknown."""
    d = _norm(cwd)
    if not d or not os.path.isdir(d):
        return d
    home, p = _norm("~"), d
    while p != home and p != os.path.dirname(p):
        git = os.path.join(p, ".git")
        if os.path.isdir(git):
            return p
        if os.path.isfile(git):
            try:
                with open(git, encoding="utf-8", errors="replace") as f:
                    m = re.match(r"gitdir:\s*(.+?)/\.git/worktrees/", f.read(1024))
            except OSError:
                m = None
            return _norm(os.path.join(p, m.group(1))) if m else p
        p = os.path.dirname(p)
    return d


def _stem(w: str) -> str:
    if len(w) > 4:
        for suf, rep in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
            if w.endswith(suf):
                w = w[:-len(suf)] + rep
                break
    return w.rstrip("e") if len(w) > 4 else w


def _words(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9_]{3,}", text.lower()) if w not in _STOP}


def _ikey(text: str) -> str:
    return " ".join(sorted(_words(text))) or text.lower()


def _sentence(s: str) -> str:
    """One mined line: collapsed, capitalized, redacted, at most 200 chars; "" when too short or not short at all."""
    s = " ".join(s.replace("**", "").replace("__", "").split()).strip(" -–—:;,.!*")
    if len(s) < 12 or len(s.split()) < 3 or len(s) > 260:
        return ""
    return _fit(s[0].upper() + s[1:], n=MAX_IDEA)


def _clause(s: str, pos: int) -> str:
    """The clause of `s` holding position `pos`: "Fixed it, but tomorrow we'll add X" -> "tomorrow we'll add X"."""
    return s[max((m.end() for m in _BOUNDARY.finditer(s, 0, pos)), default=0):]


def _codey(line: str) -> bool:
    s = re.sub(r"`[^`]*`", "", line).strip()
    sym = sum(ch in "{}[];=<>|\\$" for ch in s)
    return s.endswith(("{", "}", ";")) or (sym >= 3 and sym * 15 > len(s)) or bool(
        re.match(r"(?:def|class|function|const|let|var|fn|func|pub|package|return)\s+\w+.*[(=:{]", s))


def _prose(text: str):
    """Lines that read as prose: no fenced, indented or quoted blocks, log lines or code."""
    fence = False
    for line in text.splitlines():
        if _FENCE.match(line):
            fence = not fence
        elif (not fence and line.strip() and not line.startswith(("    ", "\t")) and not line.lstrip().startswith(">")
              and not _LOGGY.match(line) and not _codey(line)):
            yield line.strip()


# --------------------------------------------------------------------------- what counts as what
def classify(text: str) -> str:
    """Kind of a chat-mined line, from its phrasing: "summary" | "history" | "principle" | "plan" | "idea"."""
    t = (text or "").strip().lstrip("-*•> ").replace("**", "")
    if _LABEL.match(t):
        return "summary"
    if _HISTORY.match(t):
        return "history"
    if _PRINCIPLE.match(t) or _DECIDED.search(t):
        return "principle"
    if _PLAN_LEAD.match(t) or _PLAN_ANY.search(t):
        return "plan"
    return "idea"


def _action(a: str) -> str:
    """The doable part of a sentence: deferrals and fillers out ("let's add X later" -> "add X")."""
    a = _DEFER.sub(" ", a.replace("**", ""))
    a = _FILLER.sub("", _LEAD.sub("", a.strip(), count=1).strip(), count=1).strip(" \t-–—:;,.!*")
    return " ".join(("add " + a[5:] if a[:5].lower() == "have " else a).split())


def _idea_action(s: str) -> str:
    for rx in _STRONG:
        if m := rx.search(s):
            return _action(m.groupdict().get("a") or s)     # "would be nice if …" keeps the whole sentence
    m, later = _SHOULD.search(s), _LATER.search(s)
    if m or later:
        a = _action(m.group("a") if m else _clause(s, later.start()))
        if a.split()[:1] and a.split()[0].lower() in VERBS:  # weak markers need an imperative: "add X", not "it fails"
            return a
    return ""


def _from_user(text: str):
    """(line, kind) from what you typed, one per sentence: principles, then plans, then deferred ideas."""
    for line in _prose(text):
        for s in _SENT.split(_LIST.sub("", line)):
            s = _LEAD.sub("", s.strip(), count=1).strip()
            if len(s) < 12 or _QUESTION.search(s):
                continue
            if _PRINCIPLE.match(s) or (m := _DECIDED.search(s)):
                yield (s if _PRINCIPLE.match(s) else _clause(s, m.start())), "principle"
            elif _PLAN_LEAD.match(s) or (m := _PLAN_ANY.search(s)):
                yield (s if _PLAN_LEAD.match(s) else _clause(s, m.start())), "plan"
            elif a := _idea_action(s):
                yield a, "idea"


def _section(text: str, head: re.Pattern) -> list[str]:
    """List items under a heading such as "Next steps:" (### or bold, or plain ending in a colon), outside fences."""
    items, grab, pre, fence = [], False, 0, False
    for line in text.splitlines():
        if _FENCE.match(line):
            fence, grab = not fence, False
            continue
        s = line.strip()
        if fence or not s:
            continue
        if head.match(s) and (s.startswith(("#", "**", "__")) or s.rstrip("*_ ").endswith(":")):
            grab, pre = True, 0
        elif grab and (m := _ITEM.match(line)):
            items.append(m.group("t"))
        elif grab and not _SUBITEM.match(line):
            pre += 1                         # one line of prose may introduce the list; prose after it ends it
            grab = not items and pre <= 1
    return items


def _step(item: str) -> str:
    """An assistant's suggested next step as an idea, unless it is a question, code or routine advice."""
    t = item.replace("**", "").replace("__", "").strip()
    t = t[m.end():] if (m := _OFFER.match(t)) else t
    if _QUESTION.search(t) or _codey(t) or (_BORING.match(t) and len(t.split()) < 8):
        return ""
    return _action(t)


def _from_assistant(text: str, history: bool):
    """(line, kind) from an assistant reply: next steps (ideas), decisions (principles), "Summary:" lines and, with
    `history`, sentences reporting finished work ("I've added X" -> "Added X")."""
    for item in _section(text, _NEXT_HEAD):
        yield _step(item), "idea"
    for item in _section(text, _DECISION_HEAD):
        yield f"Decision: {_action(item)}", "principle"
    for line in _prose(text):
        s = _LIST.sub("", line).replace("**", "").strip()
        if m := _LABELED.match(s):
            v = m.group("v")
            yield ((_step(v), "idea") if m.group("next") else (f"Decision: {v}", "principle") if m.group("dec")
                   else (f"Summary: {v}", "summary"))
        elif history:
            for sent in _SENT.split(s):
                if (h := _DID.sub("", sent.strip(), count=1)) and _HISTORY.match(h) and len(h.split()) >= 4:
                    yield h, "history"


# --------------------------------------------------------------------------- one conversation
def _facts(msgs: list[_Msg]) -> list[tuple[_Msg, set[str], list[set[str]]]]:
    return [(m, _words(m.text) if m.role == "user" else set(),
             [_words(s) for s in _SENT.split(m.text) if _DONE.search(s)] if m.role == "assistant" else []) for m in msgs]


def _accepted(text: str) -> bool:
    """A go-ahead ("yes, do both", "sounds good", "1 and 3"), not a new request that happens to start with "ok"."""
    t = text.strip()
    return bool(_YES.match(t)) and (len(t.split()) <= 6 or bool(re.match(r"\W*\w+[,.!]", t)))


def _acted(words: set[str], later: list, offered: bool) -> bool:
    """The rest of the session shows it was taken up: the assistant reports doing it, or (for an assistant's
    suggestion) your next reply accepted it ("yes, do it") or you asked for it in your own words."""
    if len(words) < 2:
        return False
    first = True
    for m, said, done in later:
        if m.role == "assistant" and any(len(words & d) >= 0.6 * len(words) for d in done):
            return True
        if m.role == "user" and offered:
            if (first and _accepted(m.text)) or len(words & said) >= 0.5 * len(words):
                return True
            first = False
    return False


def _session(msgs: list[_Msg], source: str, cutoff: float, now: float, mtime: float, skip: list[str],
             kinds: tuple) -> list[Idea]:
    found = []                                       # (message index, ts, line, kind)
    for i, m in enumerate(msgs):
        ts = m.ts or mtime
        if (ts and ts < cutoff) or (m.cwd and any(_inside(_norm(m.cwd), s) for s in skip)):
            continue
        text = redact(m.text)
        pairs = (_from_user(text) if m.role == "user" else
                 _from_assistant(text, history="history" in kinds and source != "chatgpt") if m.role == "assistant"
                 else [(f"Summary: {text}", "summary")])
        for raw, kind in pairs:
            line = _sentence(raw or "")
            k = classify(line) if line else ""
            if k and (k == kind or (k in ACTIONABLE and kind in ACTIONABLE)) and k in kinds:
                found.append((i, ts, line, k))
    if not found:
        return []
    facts = _facts(msgs) if any(k in ACTIONABLE for *_, k in found) else []
    out, per_day = [], Counter()
    for i, ts, line, kind in reversed(found):        # newest first, so history keeps a day's last few reports
        if kind in ACTIONABLE and _acted(_words(line), facts[i + 1:], offered=msgs[i].role == "assistant"):
            continue
        if kind == "history":
            day = _day(ts)
            per_day[day] += 1
            if per_day[day] > 4:
                continue
        age = max(0.0, now - ts) / 86400 if ts else 30.0
        score = 60 * 0.5 ** (age / 7) + WEIGHT[kind] + 5 * (line.split()[0].lower() in VERBS) + 3 * (len(line) <= 140)
        out.append(Idea(text=line, project_dir=_project_dir(msgs[i].cwd) or None, source=source, ts=ts,
                        score=round(score, 2)))
    return out


def _top(ideas, limit: int) -> list[Idea]:
    """Best first, at most `limit`, shared fairly across kinds so a busy history can't crowd out ideas."""
    seen, ranked = Counter(), []
    for i in sorted(ideas, key=lambda i: (-i.score, -i.ts, i.text)):
        k = classify(i.text)
        ranked.append((seen[k], i))
        seen[k] += 1
    picked = [i for _, i in sorted(ranked, key=lambda r: r[0])][:max(0, int(limit))]
    return sorted(picked, key=lambda i: (-i.score, -i.ts, i.text))


def _mine(sessions, source: str, since_days: float, limit: int, kinds, data_dir: str | None) -> list[Idea]:
    now = _now()
    cutoff, skip, kinds = now - float(since_days) * 86400, [_norm(data_dir)] if data_dir else [], tuple(kinds)
    best: dict[tuple[str, str], Idea] = {}
    for msgs, mtime in sessions:
        for idea in _session(msgs, source, cutoff, now, mtime, skip, kinds):
            k = (idea.project_dir or "", _ikey(idea.text))
            if k not in best or idea.score > best[k].score:
                best[k] = idea
    return _top(best.values(), limit)


# --------------------------------------------------------------------------- sources
def _rows(path: Path):
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    with f:
        for line in f:
            if len(line) > MAX_ROW or not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row


def _add(out: list[_Msg], role, text, ts: float, cwd: str) -> bool:
    """Append a typed message; False when it is one of burn-week's own agent prompts (skip that session)."""
    text = _TAGGED.sub("", text if isinstance(text, str) else "").strip()
    if role == "user" and _AGENT.match(text):
        return False
    if role in ("user", "assistant") and text and not _NOT_TYPED.match(text):
        out.append(_Msg(role, text, ts, cwd if isinstance(cwd, str) else ""))
    return True


def _files(root: str, pattern: str, since_days: float) -> list[tuple[Path, float]]:
    """(transcript, mtime) under root changed within since_days; never symlinks or secret-looking names."""
    base = Path(os.path.expanduser(str(root)))
    if not base.is_dir():
        return []
    cutoff, out = _now() - float(since_days) * 86400, []
    for f in sorted(base.glob(pattern)):
        try:
            if not is_secret_file(f) and not f.is_symlink() and f.is_file() and f.stat().st_mtime >= cutoff:
                out.append((f, f.stat().st_mtime))
        except OSError:
            continue
    return out


def _claude(path: Path) -> list[_Msg]:
    out, cwd, ts, titles = [], "", 0.0, []
    for r in _rows(path):
        cwd = r["cwd"] if isinstance(r.get("cwd"), str) and r["cwd"] else cwd
        ts = _ts(r.get("timestamp")) or ts
        if r.get("type") == "summary" and isinstance(r.get("summary"), str):
            titles.append(r["summary"])
            continue
        if r.get("type") not in ("user", "assistant") or r.get("isSidechain") or r.get("isMeta") \
                or r.get("isCompactSummary"):
            continue
        msg = r.get("message") if isinstance(r.get("message"), dict) else {}
        if not _add(out, msg.get("role") or r["type"], _text(msg.get("content")), _ts(r.get("timestamp")), cwd):
            return []
    return out + [_Msg("summary", t, ts, cwd) for t in dict.fromkeys(titles)]


def _codex(path: Path) -> list[_Msg]:
    out, cwd, t0, seen = [], "", 0.0, set()
    for r in _rows(path):
        ts = _ts(r.get("timestamp")) or t0
        t0 = t0 or ts
        kind, p = r.get("type"), r.get("payload")
        if not isinstance(p, dict):                  # spring-2025 rollouts: response items at the top level
            kind, p = ("response_item", r) if kind == "message" else (kind, {})
        if kind in ("session_meta", "turn_context"):
            cwd = p.get("cwd") if isinstance(p.get("cwd"), str) and p.get("cwd") else cwd
            continue
        if kind == "response_item" and p.get("type") == "message":
            role, text = p.get("role"), _text(p.get("content"))
        elif kind == "event_msg" and p.get("type") in ("user_message", "agent_message"):
            role, text = ("user" if p["type"] == "user_message" else "assistant"), p.get("message")
        else:
            continue
        if not isinstance(text, str) or (role, text.strip()) in seen:
            continue                                 # each turn is logged twice: response_item + event_msg
        seen.add((role, text.strip()))
        if "<environment_context>" in text and (m := _CWD_TAG.search(text)):
            cwd = m.group(1)
        if not _add(out, role, text, ts, cwd):
            return []
    return out


def _export(path: str):
    p = Path(os.path.expanduser(str(path)))
    if p.is_dir():
        p = p / "conversations.json"
    try:
        if is_secret_file(p) or p.is_symlink() or not p.is_file():
            return None
        if p.suffix.lower() == ".zip":               # read conversations.json only, nothing else in the export
            with zipfile.ZipFile(p) as z:
                name = next((n for n in z.namelist() if n.rsplit("/", 1)[-1] == "conversations.json"), None)
                if not name or z.getinfo(name).file_size > MAX_EXPORT:
                    return None
                return json.loads(z.read(name).decode("utf-8", errors="replace"))
        if p.stat().st_size > MAX_EXPORT:
            return None
        return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError, zipfile.BadZipFile):
        return None


def _chatgpt(path: str):
    data = _export(path)
    for conv in data if isinstance(data, list) else []:
        if not isinstance(conv, dict) or not isinstance(conv.get("mapping"), dict):
            continue
        nodes, chain, cur = conv["mapping"], [], conv.get("current_node")
        while isinstance(cur, str) and isinstance(nodes.get(cur), dict) and len(chain) < 100_000:
            chain.append(nodes[cur])                 # the branch you kept: current_node back to the root
            cur = nodes[cur].get("parent")
        when = _ts(conv.get("update_time")) or _ts(conv.get("create_time"))
        msgs = []
        for n in reversed(chain):
            m = n.get("message") if isinstance(n.get("message"), dict) else {}
            content = m.get("content") if isinstance(m.get("content"), dict) else {}
            hidden = (m.get("metadata") if isinstance(m.get("metadata"), dict) else {}).get("is_visually_hidden_from_conversation")
            if content.get("content_type") in ("text", "multimodal_text") and not hidden:
                parts = content.get("parts") if isinstance(content.get("parts"), list) else []
                author = m.get("author") if isinstance(m.get("author"), dict) else {}
                _add(msgs, author.get("role"), "\n".join(p for p in parts if isinstance(p, str)),
                     _ts(m.get("create_time")) or when, "")
        if isinstance(conv.get("title"), str) and conv["title"].strip():
            msgs.append(_Msg("summary", conv["title"], when))
        yield msgs


def mine_claude_code(root: str = "~/.claude/projects", since_days: float = 21, limit: int = 200, *,
                     kinds=ACTIONABLE, data_dir: str = DATA_DIR) -> list[Idea]:
    """From Claude Code transcripts: <root>/<project>/<session>.jsonl rows with cwd, timestamp, type user/assistant
    (or summary) and message.content as a string or blocks. Sidechains, meta rows, tool results and sessions run by
    burn-week's own agents (their prompt, or a cwd inside data_dir) are skipped."""
    return _mine(((_claude(f), t) for f, t in _files(root, "*/*.jsonl", since_days)), "claude-code", since_days, limit,
                 kinds, data_dir)


def mine_codex(root: str = "~/.codex/sessions", since_days: float = 21, limit: int = 200, *,
               kinds=ACTIONABLE, data_dir: str = DATA_DIR) -> list[Idea]:
    """From Codex rollouts (<root>/**/*.jsonl): session_meta/turn_context give the cwd, response_item and event_msg
    rows the messages; injected AGENTS.md and environment context are skipped."""
    return _mine(((_codex(f), t) for f, t in _files(root, "**/*.jsonl", since_days)), "codex", since_days, limit,
                 kinds, data_dir)


def mine_chatgpt_export(path: str, since_days: float = 60, limit: int = 200, *, kinds=ACTIONABLE) -> list[Idea]:
    """From a ChatGPT data export: conversations.json, the folder holding it or the export .zip (only that member
    is read). Only the branch you kept is used; titles become summaries. No cwd, so project_dir stays None."""
    return _mine(((msgs, 0.0) for msgs in _chatgpt(path)), "chatgpt", since_days, limit, kinds, None)


def _default_root(env: str, home: str, sub: str) -> str:
    return os.path.join((os.environ.get(env) or "").split(",")[0].strip() or home, sub)


def mine(cfg: dict | None = None, *, kinds=ACTIONABLE) -> dict[str, list[Idea]]:
    """Everything mined from the enabled sources, deduped, best first, keyed by project_dir ("" when unknown).
    Opt-in: {} unless cfg["ideas"]["enabled"]. By default only ideas and plans, the work an agent can pick up; pass
    kinds=KINDS for chat search. cfg["ideas"]: enabled, since_days (21), limit (200), sources (claude-code, codex,
    chatgpt), claude_root, codex_root, chatgpt_export (a path), chatgpt_since_days (60). cfg["data_dir"] is skipped."""
    cfg = cfg if isinstance(cfg, dict) else {}
    ic = cfg.get("ideas") if isinstance(cfg.get("ideas"), dict) else {}
    if not ic.get("enabled"):
        return {}
    days, limit = float(ic.get("since_days", 21)), int(ic.get("limit", 200))
    want, data_dir = set(ic.get("sources") or ("claude-code", "codex", "chatgpt")), cfg.get("data_dir") or DATA_DIR
    found: list[Idea] = []
    if "claude-code" in want:
        found += mine_claude_code(ic.get("claude_root") or _default_root("CLAUDE_CONFIG_DIR", "~/.claude", "projects"),
                                  days, limit, kinds=kinds, data_dir=data_dir)
    if "codex" in want:
        found += mine_codex(ic.get("codex_root") or _default_root("CODEX_HOME", "~/.codex", "sessions"), days, limit,
                            kinds=kinds, data_dir=data_dir)
    if "chatgpt" in want and ic.get("chatgpt_export"):
        found += mine_chatgpt_export(ic["chatgpt_export"], float(ic.get("chatgpt_since_days", 60)), limit, kinds=kinds)
    best: dict[tuple[str, str], Idea] = {}
    for idea in sorted(found, key=lambda i: (-i.score, -i.ts)):
        best.setdefault((idea.project_dir or "", _ikey(idea.text)), idea)
    out: dict[str, list[Idea]] = {}
    for idea in _top(best.values(), limit):
        out.setdefault(idea.project_dir or "", []).append(idea)
    return out


# --------------------------------------------------------------------------- digest + report
def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "undated"


def _where(d: str) -> str:
    home = os.path.expanduser("~")
    return ("~" + d[len(home):] if d.startswith(home + "/") else d) or "no project"


def digest(ideas_by_dir: dict[str, list[Idea]]) -> dict[str, list[str]]:
    """{kind: [line]} for every kind in KINDS, grouped by classify(): history and summaries as
    "YYYY-MM-DD project: text", newest first; the rest as "text (project, YYYY-MM-DD)", best first."""
    rows: dict[str, list[tuple[str, Idea]]] = {k: [] for k in KINDS}
    for d, xs in (ideas_by_dir or {}).items():
        for i in xs or []:
            rows[classify(i.text)].append((d, i))
    out = {}
    for k, items in rows.items():
        if k in ("history", "summary"):
            items.sort(key=lambda r: (-r[1].ts, r[1].text))
            out[k] = [redact(f"{_day(i.ts)} {_where(d)}: {_LABEL.sub('', i.text) if k == 'summary' else i.text}")
                      for d, i in items]
        else:
            items.sort(key=lambda r: (-r[1].score, r[1].text))
            out[k] = [redact(f"{i.text} ({_where(d)}, {_day(i.ts)})") for d, i in items]
    return out


def report(ideas_by_dir: dict[str, list[Idea]]) -> str:
    """Compact colored text grouped by kind: abandoned ideas, future plans, principles, history, summaries."""
    groups = digest(ideas_by_dir)
    total = sum(len(v) for v in groups.values())
    if not total:
        return ui.c("2", "nothing mined · chat mining is opt-in (ideas.enabled or --ideas) and never leaves this machine")
    places = sum(1 for xs in (ideas_by_dir or {}).values() if xs)
    lines = [ui.c("1", f"{total} items from your chats across {places} place{'s' * (places != 1)}")
             + ui.c("2", " · mined locally, redacted, never uploaded")]
    for k in KINDS:
        if rows := groups[k]:
            lines.append(ui.c("1;36", f"  {TITLES[k]}") + ui.c("2", f"  {len(rows)}"))
            lines += [f"    {_cut(r, 116)}" for r in rows[:6]]
            lines += [ui.c("2", f"    … {len(rows) - 6} more")] if len(rows) > 6 else []
    return redact("\n".join(lines))
