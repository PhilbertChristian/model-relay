"""Claude Code lane: `claude -p` inside the task's worktree, burning the Claude subscription.

Never --dangerously-skip-permissions: acceptEdits plus explicit allow/deny lists. The CLI gets a scrubbed
env (no API keys or tokens, so it bills the subscription, never the API) and its own process group,
killed on timeout. A usage limit ends the task as "limited" until the reset: no retries.
The process and limit helpers here are shared with the Codex lane.
"""
from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .. import value
from ..contracts import Emit, LaneResult, SwarmTask, redact
from .base import Lane, task_prompt

DEFAULT_DENY = ["Bash(git push:*)", "Bash(git remote:*)", "Bash(git checkout:*)", "Bash(git switch:*)",
                "Bash(git stash:*)", "Bash(git reset:*)", "Bash(rm -rf:*)", "Bash(sudo:*)", "Bash(curl:*)",
                "Bash(wget:*)", "Read(./.env*)", "Read(**/.env*)", "Read(~/.ssh/**)",
                # beyond the spec minimum: the swarm does the committing, and keychains / cloud creds stay unread
                "Bash(git commit:*)", "Bash(git clean:*)", "Bash(security:*)", "Read(~/.aws/**)", "Read(~/.netrc)"]
SAFE_BASH = ("git status", "git diff", "git log", "git show", "ls", "npm test", "npm run test", "npm run lint",
             "npm run build", "npx tsc", "pnpm test", "yarn test", "pytest", "python -m pytest", "python3 -m pytest",
             "python -m unittest", "python3 -m unittest", "go test", "go vet", "go build", "cargo test",
             "cargo check", "make test")
DEFAULT_ALLOW = ["Read", "Edit", "Write", "MultiEdit", "Glob", "Grep", "LS", "TodoWrite",
                 *(f"Bash({c}:*)" for c in SAFE_BASH)]
FORBIDDEN = ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "bypassPermissions")

# an allow entry that could grant something the deny list exists to stop (or smuggle a flag into argv)
_UNSAFE_TOOL = re.compile(r"^\s*-|dangerously|bypass|git\s+(push|remote|checkout|switch|stash|reset|commit|clean)"
                          r"|rm\s+-rf|sudo|curl|wget|\.env|\.ssh", re.I)
_SECRET_ENV = re.compile(r"(_API_KEY|_TOKEN|_KEY|PASSWORD|PASSWD)$|SECRET|CREDENTIAL"
                         r"|^(SUPABASE|MONID|CONTEXT_DEV|AGENT37)_|^SSH_AUTH_SOCK$", re.I)
# only trusted when the run ended in an error: a successful summary may well talk about rate limiting
LIMIT_RE = re.compile(r"usage[ _-]?limit|rate[ _-]?limit|hit your (\w+ )?limit|too many requests|\b429\b"
                      r"|((5-hour|weekly|session|daily|monthly|opus|sonnet)\s+){1,2}limit|insufficient_quota"
                      r"|exceeded your current quota", re.I)
# a final message that *is* a limit notice, whatever the exit status
LIMIT_HEAD_RE = re.compile(r"\W*(claude ai usage limit reached|you'?ve (hit|reached) your (\w+ )?limit"
                           r"|((5-hour|weekly|session|daily|opus|sonnet|usage)\s+){1,2}limit reached)", re.I)
BLOCKED_RE = re.compile(r"[\s*_>`#-]*blocked[*_`\s]*:", re.I)
GRACE = 3.0                                  # seconds between SIGTERM and SIGKILL
_LIVE: set[subprocess.Popen] = set()         # agent CLIs still running, killed if relay exits mid-run


# ------------------------------------------------------------------ helpers shared with the codex lane
def scrubbed_env(env: dict | None = None) -> dict[str, str]:
    """The environment minus API keys, tokens, secrets and sponsor credentials; PATH, HOME etc. stay."""
    return {k: v for k, v in (os.environ if env is None else env).items() if not _SECRET_ENV.search(k)}


def check_workdir(workdir: str) -> str:
    """'' when `workdir` can host an agent, else why not. A `.git` directory means the user's own checkout."""
    p = Path(workdir)
    if not p.is_absolute() or not p.is_dir():
        return f"refusing to run outside an existing absolute worktree path: {workdir!r}"
    if (p / ".git").is_dir():
        return f"refusing to run in a primary checkout ({workdir}): burn agents only work in a git worktree"
    return ""


def safe_model(m) -> str:
    """The configured model id, or '' when missing or shaped like something other than a model id."""
    m = str(m or "").strip()
    return m if re.fullmatch(r"\w[\w.:/@\[\]-]*", m) else ""


def kill_group(p: subprocess.Popen, grace: float = GRACE) -> None:
    """SIGTERM the whole process group, allow `grace` seconds, then SIGKILL whatever is left."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(p.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        if sig == signal.SIGTERM:
            try:
                p.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass


atexit.register(lambda: [kill_group(p, 0.5) for p in list(_LIVE)])


def run_cli(argv: list[str], cwd: str, timeout: float, on_line: Callable[[str], None],
            env: dict | None = None) -> tuple[int | None, str, bool]:
    """Run an agent CLI in its own process group (scrubbed env), feeding each stdout line to on_line.
    The whole group is killed on timeout, and once the CLI exits (leftover dev servers, watchers).
    Returns (exit code, stderr tail, timed_out)."""
    p = subprocess.Popen(argv, cwd=cwd, env=scrubbed_env() if env is None else env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                         errors="replace", start_new_session=True)
    _LIVE.add(p)
    err: deque[str] = deque(maxlen=40)
    fired = threading.Event()
    timer = threading.Timer(timeout, lambda: (fired.set(), kill_group(p)))
    threads = [threading.Thread(target=err.extend, args=(p.stderr,)),           # never let stderr fill up
               threading.Thread(target=lambda: (p.wait(), kill_group(p, 0.5)))]  # leftovers die with the agent
    for t in (timer, *threads):
        t.daemon = True
        t.start()
    try:
        for line in p.stdout:
            if line.strip():
                on_line(line.rstrip("\n"))
        p.wait()
    finally:
        timer.cancel()
        kill_group(p, 0.5)
        _LIVE.discard(p)
        threads[0].join(2)
        p.stdout.close()
        if not threads[0].is_alive():
            p.stderr.close()
    return p.returncode, "".join(err), fired.is_set()


_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
_MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
_AT_RE = re.compile(r"(?:try again at|resets?(?:\s+(?:at|on))?)\s+(?:([a-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(?:at\s+)?)?"
                    r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\b\.?(?:\s*\(([\w/+-]+)\))?", re.I)


def reset_time(text: str, now: float | None = None) -> float | None:
    """When a usage limit resets, from the CLI's own words ("limit reached|<epoch>", "resets_in_seconds",
    "try again in 2 hours 5 minutes", "resets 3pm (America/Los_Angeles)"). None when it doesn't say."""
    now = time.time() if now is None else now
    if m := re.search(r"\|\s*(\d{10,13})\b|resets?_?at\W{0,4}(\d{10,13})\b", text, re.I):
        v = float(m[1] or m[2])
        return v / 1000 if v > 1e12 else v
    if m := re.search(r"resets?_in_seconds\W{0,4}(\d+)", text, re.I):
        return now + float(m[1])
    if m := re.search(r"(?:try again|resets?)\s+in\s+((?:\d+\s*[a-z]+\W{0,3}(?:and\s+)?)+)", text, re.I):
        secs = sum(int(n) * _UNITS.get(u[0].lower(), 0) for n, u in re.findall(r"(\d+)\s*([a-z]+)", m[1], re.I))
        if secs:
            return now + secs
    if m := _AT_RE.search(text):
        try:
            tz = ZoneInfo(m[6]) if m[6] else None
        except Exception:
            tz = None
        try:
            base = datetime.fromtimestamp(now, tz) if tz else datetime.fromtimestamp(now).astimezone()
            when = base.replace(hour=int(m[3]) % 12 + (12 if m[5].lower() == "p" else 0), minute=int(m[4] or 0),
                                second=0, microsecond=0)
            if m[1] and m[1].lower() in _MONTHS:
                when = when.replace(month=_MONTHS.index(m[1].lower()) + 1, day=int(m[2]))
                if when.timestamp() < now - 86400:
                    when = when.replace(year=when.year + 1)
            elif when.timestamp() <= now:
                when += timedelta(days=1)
            return when.timestamp()
        except ValueError:
            return None
    return None


def outcome(summary: str, tail: str, *, rc: int | None, errored: bool, timed_out: bool, timeout: float, cli: str,
            limit: str = "", until: float | None = None, fallback: float | None = None, **usage) -> LaneResult:
    """How an agent CLI run ended, as a LaneResult: limited > failed (timeout, crash, error) > blocked > done."""
    summary, limit = redact(summary or "").strip(), redact(limit or "").strip()
    text = "\n".join(s for s in (limit, summary, tail) if s)
    if LIMIT_HEAD_RE.match(summary) or (errored and (limit or LIMIT_RE.search(text))):
        why = limit or next((ln for ln in text.splitlines() if LIMIT_RE.search(ln)), summary or "usage limit")
        when = max(until or reset_time(text) or fallback or time.time() + 3600, time.time() + 300)
        return LaneResult("limited", short(re.sub(r"\|\s*\d{10,13}", "", why), 300), limited_until=when, **usage)
    if timed_out:
        return LaneResult("failed", short(f"{cli} timed out after {timeout:g}s" + (f": {tail[-300:]}" if tail else ""),
                                          600), **usage)
    if errored:
        how = f"{cli} exited {rc}" if rc else f"{cli} reported an error"
        return LaneResult("failed", short(": ".join([how, *[s for s in (summary, tail[-400:]) if s]]), 600), **usage)
    if BLOCKED_RE.match(summary):
        return LaneResult("blocked", short(summary, 600), **usage)
    return LaneResult("done", short(summary, 600) or "done", **usage)


def short(s, n: int = 90) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def jsonl(line: str) -> dict | None:
    s = line.strip()
    if not s.startswith("{"):
        return None
    try:
        v = json.loads(s)
    except ValueError:
        return None
    return v if isinstance(v, dict) else None


def num(v) -> int:
    return int(v) if isinstance(v, (int, float)) and v > 0 else 0


def rel(path: str, workdir: str) -> str:
    """`path` relative to the worktree when it is inside it (shorter dashboard notes)."""
    for root in {workdir.rstrip("/"), os.path.realpath(workdir).rstrip("/")}:
        if str(path).startswith(root + "/"):
            return str(path)[len(root) + 1:]
    return str(path)


class Progress:
    """Emits agent_progress (cumulative tokens, usd, short redacted note), at most once per `every` seconds."""

    def __init__(self, emit: Emit, every: float = 0.5):
        self.emit, self.every, self.last, self.prev, self.note = emit, every, -1e9, None, "starting"

    def __call__(self, tokens: int, usd: float, note: str = "") -> None:
        self.note = short(redact(note or self.note))
        now, key = time.monotonic(), (int(tokens), self.note)
        if key == self.prev or now - self.last < self.every:
            return
        self.last, self.prev = now, key
        self.emit("agent_progress", tokens=key[0], usd=round(float(usd), 6), note=key[1])


# ------------------------------------------------------------------ claude stream-json
def _prompt_tokens(u: dict) -> int:
    return num(u.get("input_tokens")) + num(u.get("cache_creation_input_tokens")) + num(u.get("cache_read_input_tokens"))


class _Stream:
    """Folds `claude -p --output-format stream-json --verbose` lines into progress events and a LaneResult.
    Input tokens include cache reads/writes: that is what the subscription burns."""

    def __init__(self, workdir: str, progress: Progress, model: str):
        self.workdir, self.progress, self.model = workdir, progress, model
        self.usage: dict[str, dict] = {}         # API message id -> its usage (every content block repeats it)
        self.text, self.result, self.limit, self.until = "", None, "", None
        self.raw: deque[str] = deque(maxlen=12)  # non-JSON stdout, for failure tails

    def feed(self, line: str) -> None:
        ev = jsonl(line)
        if ev is None:
            self.raw.append(line)
        elif ev.get("type") == "system" and isinstance(ev.get("model"), str):
            self.model = ev["model"]
        elif ev.get("type") == "assistant" and isinstance(ev.get("message"), dict):
            self._assistant(ev["message"])
        elif ev.get("type") == "result":
            self.result = ev
        elif ev.get("type") == "rate_limit_event":
            info = ev.get("rate_limit_info") if isinstance(ev.get("rate_limit_info"), dict) else {}
            if info.get("status") == "rejected":     # "allowed" / "allowed_warning" are just usage reports
                self.limit = f"{info.get('rateLimitType') or 'usage'} limit reached"
                self.until = num(info.get("resetsAt")) or None

    def _assistant(self, msg: dict) -> None:
        if isinstance(msg.get("usage"), dict):
            self.usage[str(msg.get("id") or f"anon-{len(self.usage)}")] = msg["usage"]
        synthetic = msg.get("model") == "<synthetic>"
        if isinstance(msg.get("model"), str) and not synthetic:
            self.model = msg["model"]
        note = ""
        for b in msg.get("content") if isinstance(msg.get("content"), list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                note = self._tool(b)
            elif b.get("type") == "text" and str(b.get("text") or "").strip():
                self.text = str(b["text"]).strip()
                note = note or self.text.splitlines()[0]
                if synthetic and LIMIT_RE.search(self.text):
                    self.limit = self.text
        inp, out = self.tokens()
        self.progress(inp + out, self.estimate(), note or "thinking")

    def _tool(self, b: dict) -> str:
        name, inp = str(b.get("name") or "tool"), b.get("input") if isinstance(b.get("input"), dict) else {}
        if name == "Bash":
            return f"$ {inp.get('command', '')}"
        if name == "TodoWrite":
            return "updating the todo list"
        arg = next((str(inp[k]) for k in ("file_path", "notebook_path", "path", "pattern", "url", "query", "description")
                    if inp.get(k)), "")
        return f"{name} {rel(arg, self.workdir)}".strip()

    def tokens(self) -> tuple[int, int]:
        us = self.usage.values()
        return sum(_prompt_tokens(u) for u in us), sum(num(u.get("output_tokens")) for u in us)

    def estimate(self) -> float:
        """usd at API list rates while running; the result event's total_cost_usd replaces it."""
        total = {k: sum(num(u.get(k)) for u in self.usage.values()) for k in value.TOKEN_FIELDS}
        return value.price(self.model, total) or 0.0

    def finish(self, rc: int | None, err: str, timed_out: bool, timeout: float) -> LaneResult:
        r = self.result or {}
        ru = r.get("usage") if isinstance(r.get("usage"), dict) else {}
        mu = [m for m in r["modelUsage"].values() if isinstance(m, dict)] if isinstance(r.get("modelUsage"), dict) else []
        inp, out = self.tokens()
        inp = max(inp, _prompt_tokens(ru), sum(num(m.get("inputTokens")) + num(m.get("cacheReadInputTokens"))
                                               + num(m.get("cacheCreationInputTokens")) for m in mu))
        out = max(out, num(ru.get("output_tokens")), sum(num(m.get("outputTokens")) for m in mu))
        usd = r.get("total_cost_usd")
        if not isinstance(usd, (int, float)) or isinstance(usd, bool):
            usd = sum(float(m.get("costUSD") or 0) for m in mu) or self.estimate()
        summary = (r.get("result") if isinstance(r.get("result"), str) else "") or self.text
        errored = (timed_out or rc != 0 or r.get("is_error") is True or str(r.get("subtype") or "").startswith("error")
                   or not (r or self.text))
        tail = redact("\n".join([*self.raw, err]).strip())[-600:]
        return outcome(summary, tail, rc=rc, errored=errored, timed_out=timed_out, timeout=timeout, cli="claude",
                       limit=self.limit, until=self.until, input_tokens=inp, output_tokens=out,
                       usd=round(float(usd), 6), model=self.model)


class ClaudeLane(Lane):
    """cfg: model, allowed_tools, disallowed_tools (added to the safe defaults), bin, progress_every."""
    kind = "claude"

    def __init__(self, name: str | None = None, cfg: dict | None = None):
        super().__init__(name, cfg)
        self.bin = str(self.cfg.get("bin") or "claude")
        self.model = safe_model(self.cfg.get("model"))

    def available(self) -> tuple[bool, str]:
        exe = shutil.which(self.bin)
        return (True, f"claude CLI at {exe}") if exe else (False, f"{self.bin} not found on PATH (install Claude Code)")

    def tools(self, task: SwarmTask) -> tuple[list[str], list[str]]:
        """(allow, deny): safe defaults + the project's test command + cfg, minus entries that would grant a denied
        capability. Deny rules beat allow rules in Claude Code, so cfg can only ever add to the deny list."""
        listed = lambda v: [v] if isinstance(v, str) else [str(x) for x in v or []]
        test = (task.project.test_cmd or "").strip()
        allow = [*DEFAULT_ALLOW, *([f"Bash({test}:*)"] if test else []), *listed(self.cfg.get("allowed_tools"))]
        deny = [*DEFAULT_DENY, *listed(self.cfg.get("disallowed_tools"))]
        return ([t for t in dict.fromkeys(allow) if t.strip() and not _UNSAFE_TOOL.search(t)],
                [t for t in dict.fromkeys(deny) if t.strip() and not t.lstrip().startswith("-")])

    def args(self, task: SwarmTask) -> list[str]:
        """argv after the executable: never --dangerously-skip-permissions."""
        allow, deny = self.tools(task)
        argv = ["-p", task_prompt(task), "--output-format", "stream-json", "--verbose", "--permission-mode", "acceptEdits"]
        argv += ["--model", self.model] if self.model else []
        argv += ["--allowedTools", *allow, "--disallowedTools", *deny]
        if any(a == f or a.startswith(f + "=") for a in argv for f in FORBIDDEN):
            raise ValueError("refusing to build a claude command that skips permissions")
        return argv

    def run(self, task: SwarmTask, workdir: str, emit: Emit, timeout: float = 1800) -> LaneResult:
        if self.is_limited():
            return LaneResult("limited", f"{self.name} is limited until its reset", model=self.model,
                              limited_until=self.limited_until)
        exe = shutil.which(self.bin)
        why = check_workdir(workdir) or ("" if exe else f"{self.bin} not found on PATH")
        if why:
            return LaneResult("failed", why, model=self.model)
        st = _Stream(workdir, Progress(emit, float(self.cfg.get("progress_every", 0.5))), self.model or "claude")
        try:
            rc, err, timed_out = run_cli([exe, *self.args(task)], workdir, timeout, st.feed)
        except (OSError, ValueError) as e:
            return LaneResult("failed", short(redact(f"could not start claude: {e}"), 300), model=self.model)
        return st.finish(rc, err, timed_out, timeout)   # the swarm sets self.limited_until (and emits lane_limited)
