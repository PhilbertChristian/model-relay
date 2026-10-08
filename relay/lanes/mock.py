"""Mock lane: a lively, deterministic stand-in agent for demos and tests. No keys, no network.

Seeded per task id, it works in small steps (cfg `speed`), emits agent_progress with cumulative tokens and
usd at a plausible per-flavor price, and lands a small real change in the worktree (marks the task's TODO done
or ticks its checklist item, else adds a test or a note) so worktree.finalize has a diff. It narrates as its
"as" flavor, and like a real agent it answers BLOCKED: to tasks only a human should do (publish, deploy, keys).
cfg: speed, seconds, seed, outcomes {done, blocked, failed, limited}, limit_seconds, model, price_in, price_out.
"""
from __future__ import annotations

import os
import random
import re
import time
from pathlib import Path

from ..contracts import Emit, LaneResult, SwarmTask, is_secret_file, redact, slugify
from .base import Lane

# flavor -> (model, $/1M input, $/1M output, token scale). Agent input is mostly cache reads: a low blended price.
FLAVORS = {"claude": ("claude-sonnet-5", 0.6, 10.0, 1.0), "codex": ("gpt-5-codex", 0.35, 10.0, 0.8),
           "agent37": ("agent37/auto", 0.3, 1.5, 0.5), "relay": ("gpt-5-mini", 0.25, 2.0, 0.35),
           "orca": ("orca/claude-sonnet-5", 0.6, 10.0, 1.1), "mock": ("mock-1", 0.5, 5.0, 0.7)}
# how each flavor narrates a step: think, search, read, edit, test
VERBS = {"claude": ("planning: {x}", 'Grep "{k}"', "Read {f}", "Edit {f}", "$ {t}"),
         "codex": ("reasoning: {x}", 'exec rg -n "{k}"', "exec sed -n '1,160p' {f}", "apply_patch {f}", "exec {t}"),
         "": ("planning: {x}", 'searching for "{k}"', "reading {f}", "editing {f}", "running {t}")}
OUTCOMES = {"done": 0.82, "blocked": 0.1, "failed": 0.06, "limited": 0.02}
DONE = ["{T}. Kept the change small; {t_cmd} passes.", "Done: {t}. One focused change; {t_cmd} still green.",
        "{T}: implemented and verified with {t_cmd}."]
BLOCKED = ["BLOCKED: needs credentials for the staging API, which are not in the worktree.",
           "BLOCKED: needs a product decision: keep the old behaviour or change the public API?",
           "BLOCKED: needs a paid third-party account to run the integration tests.",
           "BLOCKED: depends on a schema migration someone else owns."]
FAILED = ["tests still failing after the change ({n} failures)", "agent exited 1: context window exceeded",
          "could not reproduce the bug; the attempted fix broke {n} tests"]
LIMITS = {"claude": "Claude AI usage limit reached", "codex": "You've hit your usage limit",
          "agent37": "Agent37 instance budget exhausted", "relay": "rate limit on every model in the ladder"}
TODO_RE = re.compile(r"\b(TODO|FIXME|XXX)\b[\s:(\-]*(.*)")
BOX_RE = re.compile(r"^(\s*[-*]\s+\[) (\]\s+)(.*)")                  # an open markdown checklist item
HUMAN_RE = re.compile(r"\b(publish\w*|deploy\w*|credentials?|api[ _-]?keys?|payments?|billing|app store|crates\.io"
                      r"|pypi)\b", re.I)                               # tasks only a human should do
SKIP_DIRS = {"node_modules", "__pycache__", "dist", "build", "target", "venv"}
TEXT_EXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".rb", ".java", ".kt", ".swift", ".c",
            ".h", ".cc", ".cpp", ".cs", ".php", ".sh", ".sql", ".css", ".html", ".md", ".txt", ".toml", ".yaml", ".yml"}
TEST_PY = '''# Added by the relay burn {flavor} lane for: {text}
import unittest


class BurnRegression(unittest.TestCase):
    def test_task_is_tracked(self):
        self.assertTrue({literal})


if __name__ == "__main__":
    unittest.main()
'''


class MockLane(Lane):
    kind = "mock"

    def available(self) -> tuple[bool, str]:
        return True, "demo lane: no keys, no network"

    def run(self, task: SwarmTask, workdir: str, emit: Emit, timeout: float = 1800) -> LaneResult:
        model, p_in, p_out, scale = FLAVORS.get(self.flavor, FLAVORS["mock"])
        model = str(self.cfg.get("model") or model)
        p_in, p_out = float(self.cfg.get("price_in", p_in)), float(self.cfg.get("price_out", p_out))
        if self.is_limited():
            return LaneResult("limited", f"{self.name} is limited until its reset", model=model,
                              limited_until=self.limited_until)
        if not os.path.isdir(workdir):
            return LaneResult("failed", f"no worktree at {workdir}", model=model)
        rng = random.Random(f"{self.cfg.get('seed', 0)}:{task.id}")
        weights = {k: max(0.0, float(w)) for k, w in (self.cfg.get("outcomes") or {}).items() if k in OUTCOMES}
        weights = weights if sum(weights.values()) > 0 else OUTCOMES
        status = rng.choices(list(weights), list(weights.values()))[0]
        if status in ("done", "failed") and HUMAN_RE.search(task.text):
            status = "blocked"                                          # like a real agent told to say BLOCKED:
        files = _files(workdir)
        target, line = _target(workdir, files, task.text, task.id)
        summary, steps = self._script(rng, task, files, target, status)
        speed = max(float(self.cfg.get("speed", 1.0)), 1e-3)
        per_step = float(self.cfg.get("seconds", 14)) * rng.uniform(0.6, 1.5) / speed / len(steps)
        deadline, tin, tout = time.monotonic() + timeout, 0, 0
        usd = lambda: round((tin * p_in + tout * p_out) / 1e6, 6)
        for note, edit in steps:
            if not _nap(per_step * rng.uniform(0.4, 1.6), deadline):
                return LaneResult("failed", f"timed out after {timeout:g}s", tin, tout, usd(), model)
            tin += int(rng.randint(14_000, 48_000) * scale)
            tout += int(rng.randint(150, 2_200) * scale)
            if edit:
                _apply(workdir, target, line, task.text, self.flavor)
            emit("agent_progress", tokens=tin + tout, usd=usd(), note=redact(note))
        until = time.time() + float(self.cfg.get("limit_seconds", 3600)) if status == "limited" else None
        return LaneResult(status, redact(summary), tin, tout, usd(), model, until)   # the swarm marks the lane

    def _script(self, rng: random.Random, task: SwarmTask, files: list[str], target: str,
                status: str) -> tuple[str, list[tuple[str, bool]]]:
        """(summary, [(note, writes_the_change)]): what the agent will say and the steps it narrates."""
        think, search, read, edit, test = VERBS.get(self.flavor, VERBS[""])
        cmd = task.project.test_cmd or "the test suite"
        text = _short(task.text, 120)
        steps = [(think.format(x=_short(task.text, 52)), False), (search.format(k=_keyword(task.text)), False)]
        reads = rng.sample(files, min(rng.randint(2, 4), len(files))) if files else ["README.md"]
        steps += [(read.format(f=f), False) for f in reads]
        if status == "blocked":
            m = HUMAN_RE.search(task.text)
            summary = (f"BLOCKED: needs a human ({m[0]}): burn agents never publish, deploy or handle credentials."
                       if m else rng.choice(BLOCKED))
            return summary, steps + [(summary, False)]
        if status == "limited":
            summary = LIMITS.get(self.flavor, "usage limit reached")
            return summary, steps[:rng.randint(2, len(steps))] + [(f"{summary}: stopping", False)]
        steps += [(edit.format(f=target), True), (test.format(t=cmd), False)]
        if status == "failed":
            n = rng.randint(1, 4)
            return rng.choice(FAILED).format(n=n), steps + [(f"{n} tests failing", False)]
        steps += [(f"{rng.randint(6, 160)} tests passed", False), ("reviewing the diff", False)]
        summary = rng.choice(DONE).format(T=text[:1].upper() + text[1:], t=text[:1].lower() + text[1:], t_cmd=cmd)
        return summary, steps + [("writing the summary", False)]


def _files(workdir: str, limit: int = 300) -> list[str]:
    """Text files in the worktree, sorted (deterministic), minus hidden/build dirs, symlinks and secret files."""
    out: list[str] = []
    for root, dirs, names in os.walk(workdir):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for n in sorted(names):
            full = os.path.join(root, n)
            r = os.path.relpath(full, workdir)
            if n.startswith(".") or Path(n).suffix.lower() not in TEXT_EXT or os.path.islink(full) or is_secret_file(r):
                continue
            out.append(r)
            if len(out) >= limit:
                return out
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _target(workdir: str, files: list[str], text: str, task_id: str) -> tuple[str, int | None]:
    """Where the change lands: the task's own TODO line or open checklist item when we can find it, else a new
    test (Python project with a tests/ dir), else NOTES.md."""
    want = _norm(text)
    for r in files:
        p = Path(workdir) / r
        try:
            lines = p.read_text(encoding="utf-8").splitlines() if p.stat().st_size < 256_000 else []
        except (OSError, UnicodeDecodeError):
            continue
        for i, ln in enumerate(lines):
            m = TODO_RE.search(ln) or BOX_RE.match(ln)
            got = _norm(m.groups()[-1]) if m else ""
            if len(got) >= 6 and (got[:40] in want or want[:40] in got):
                return r, i
    if os.path.isdir(os.path.join(workdir, "tests")) and any(f.endswith(".py") for f in files):
        return f"tests/test_burn_{slugify(task_id).replace('-', '_')[:32]}.py", None
    return "NOTES.md", None


def _apply(workdir: str, target: str, line: int | None, text: str, flavor: str) -> None:
    """The real change: mark the TODO done or tick the checklist item, else add a small test / a note line."""
    p, text = Path(workdir) / target, _short(redact(text), 160)
    try:
        if line is not None:
            lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
            old = lines[line]
            body = old.rstrip("\r\n")
            if m := TODO_RE.search(body):
                new = f"{body[:m.start(1)]}Done{body[m.end(1):]}"
            elif m := BOX_RE.match(body):
                new = f"{m[1]}x{m[2]}{m[3]}"
            else:
                return
            lines[line] = f"{new} (relay burn, {flavor}){old[len(body):]}"
            p.write_text("".join(lines), encoding="utf-8")
        elif target.endswith(".py"):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(TEST_PY.format(flavor=flavor, text=text, literal=repr(text)), encoding="utf-8")
        else:
            head = "" if p.exists() else "# Notes\n\n"
            with p.open("a", encoding="utf-8") as f:
                f.write(f"{head}- [x] {text} ({flavor} lane, relay burn)\n")
    except (OSError, IndexError):
        pass


def _keyword(text: str) -> str:
    """Something an agent would grep for: a call-looking identifier, else the longest word."""
    calls = re.findall(r"([A-Za-z_][\w.]{2,})\(", text)
    words = re.findall(r"[A-Za-z_]\w{3,}", text)
    return calls[0] if calls else max(words, key=len) if words else "TODO"


def _short(s: str, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _nap(seconds: float, deadline: float) -> bool:
    """Sleep in small ticks; False when the run's deadline comes first."""
    end = min(time.monotonic() + max(0.0, seconds), deadline)
    while (left := end - time.monotonic()) > 0:
        time.sleep(min(0.2, left))
    return time.monotonic() < deadline
