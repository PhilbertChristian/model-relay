"""Relay lane: Relay's own engine (relay.agent: one agent, a ladder of API models) works the task in the worktree.

Ladder: the burn config's `providers` + `models` (relay.json schema), else relay.config.DEFAULT, kept to the
providers this lane may spend (the lane's `providers`, else its subscription's). relay/agent.py is untouched: the
engine gets a Telemetry stand-in (_Feed) that turns its events into agent_progress and stops it between steps at
the deadline, after a provider-error streak, or as soon as every model on the lane is rate- or budget-limited (no
waiting out a limit). Limits carry over to the lane's later tasks. Tools are fenced to the worktree: no secret
files, no pushes/remotes/commits, key-like env vars unset in the shell, every tool output redacted before a model
sees it. The engine's console output is muted for its thread only, so the live dashboard stays intact.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ..agent import Agent
from ..config import DEFAULT, build_router
from ..contracts import Emit, LaneResult, SwarmTask, is_secret_file, redact
from ..providers import Provider
from ..router import Router
from ..tools import Toolbox, ToolError
from .base import Lane, task_prompt

KNOBS = ("start_tier", "deescalate_after", "base_cooldown", "max_steps")
LIMITS = ("rate_limit", "budget")
# never from an unattended burn agent: git state the swarm owns (it commits; nothing is pushed), publishing,
# privilege, network fetches, global installs, wiping outside the tree, credential stores
DENY_RE = re.compile(
    r"\bgit\s+(?:-\w\s+\S+\s+|--?[\w-]+(?:=\S+)?\s+)*(?:push|remote|commit|config|credential|stash|checkout|switch"
    r"|reset|rebase|merge|branch|tag|worktree|clean|update-ref|filter-branch|gc)\b"
    r"|\bgh\s+(?:pr|repo|release|api|gist|secret|auth)\b|\bsudo\b|\b(?:curl|wget|ssh|scp|nc)\b"
    r"|\b(?:npm|yarn|pnpm)\s+publish\b|\btwine\b|\bdocker\s+push\b|\bpip3?\s+install\b|\bbrew\s+install\b"
    r"|\bnpm\s+(?:i|install)\s+(?:-g|--global)\b|\brm\s+-\w+\s+(?:/|~|\.\.)|\.ssh\b|Keychains|\bsecurity\s+\w+-"
    r"|\bCookies\b|Login Data", re.I)
SECRET_ENV_RE = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH", re.I)


def refusal(command: str, root: Path | None = None) -> str | None:
    """Why a burn agent may not run this shell command, or None. A word counts as a secret file when it matches
    SECRET_FILE_RE and looks like a path (`.env`, `conf/credentials.yml`) or names a file in the worktree."""
    m = DENY_RE.search(command)
    if m:
        return m.group(0).strip()
    for w in re.split(r"[\s=;|&<>()'\"`]+", command):
        if w and is_secret_file(w) and ("/" in w or w.startswith(".") or (root is not None and (root / w).exists())):
            return f"secret file {w}"
    return None


def _clip(text: str, n: int = 500) -> str:
    return redact(" ".join(str(text or "").split()))[:n]


# ------------------------------------------------------------------ muting the engine's console, per thread
_MUTED: set[int] = set()
_MUTE_LOCK = threading.Lock()


class _MuteStdout:
    """sys.stdout stand-in: drops writes from threads running the engine, every other thread writes through."""

    def __init__(self, inner):
        self.inner = inner

    def write(self, s: str) -> int:
        return len(s) if threading.get_ident() in _MUTED else self.inner.write(s)

    def flush(self) -> None:
        if threading.get_ident() not in _MUTED:
            self.inner.flush()

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


@contextmanager
def _quiet():
    """Mute print() in this thread only (redirect_stdout is process-wide and would blank the dashboard)."""
    me = threading.get_ident()
    with _MUTE_LOCK:
        if not isinstance(sys.stdout, _MuteStdout):
            sys.stdout = _MuteStdout(sys.stdout)
        _MUTED.add(me)
    try:
        yield
    finally:
        with _MUTE_LOCK:
            _MUTED.discard(me)
            if not _MUTED and isinstance(sys.stdout, _MuteStdout):
                sys.stdout = sys.stdout.inner


# ------------------------------------------------------------------ tools, fenced for unattended work
class GuardedToolbox(Toolbox):
    """relay.tools.Toolbox for burn work: paths stay in the worktree, secrets stay out of the model's context."""

    def __init__(self, root: str):
        super().__init__(root)
        self.last = ""                     # the last tool call, for progress notes

    def _path(self, p: str):
        path = super()._path(p)
        if path != self.root and self.root not in path.parents:
            raise ToolError(f"{p}: outside this worktree")
        if is_secret_file(str(path.relative_to(self.root))):
            raise ToolError(f"{p}: secret files are off limits")
        return path

    def run(self, name: str, raw_args: str) -> tuple[str, bool]:
        try:
            a = json.loads(raw_args or "{}")
            self.last = f"{name} {a.get('command') or a.get('path') or a.get('reason') or ''}".strip()
        except (ValueError, AttributeError):
            self.last = name
        out, is_err = super().run(name, raw_args)
        return redact(out), is_err

    def t_bash(self, command: str, timeout: int = 60) -> str:
        why = refusal(command, self.root)
        if why:
            raise ToolError(f"refused by burn-week policy ({why}). Work only in this worktree; "
                            "the swarm commits and nothing is pushed.")
        keys = " ".join(k for k in os.environ if SECRET_ENV_RE.search(k) and re.fullmatch(r"[A-Za-z_]\w*", k))
        return super().t_bash(f"unset {keys}; {command}" if keys else command, timeout)


# ------------------------------------------------------------------ the engine's telemetry, turned into progress
class _Stop(Exception):
    def __init__(self, status: str, why: str):
        super().__init__(why)
        self.status = status


class _Feed:
    """Telemetry stand-in for relay.agent.Agent, which only calls .emit(): engine events become agent_progress,
    and the run stops between steps at the deadline, after a provider-error streak, or once every model is limited."""

    def __init__(self, router: Router, tools: GuardedToolbox, emit: Emit, timeout: float, max_errors: int = 6):
        self.router, self.tools, self.out, self.timeout, self.max_errors = router, tools, emit, timeout, max_errors
        self.deadline = time.time() + timeout
        self.limits: dict[str, str] = {}       # model id -> "rate_limit" | "budget", seen this run
        self.streak = 0

    def note(self, event: str, d: dict) -> str | None:
        if event == "call":
            return f"{d.get('model')} {d.get('input_tokens', 0)}+{d.get('output_tokens', 0)} tok"
        if event == "tool":
            return ("✗ " if d.get("error") else "") + self.tools.last
        if event == "switch":
            old, new = d.get("old"), d.get("model")
            return f"{old} → {new}: {d.get('reason', '')}" if old else f"start on {new}"
        if event == "provider_error":
            return f"{d.get('model')}: {d.get('kind')} {d.get('status') or ''}".strip()
        if event in ("blocker", "hint", "refusal", "reset"):
            return f"{event}: {d.get('reason') or d.get('model') or ''}"
        return None

    def emit(self, event: str, **d) -> dict:
        r, now = self.router, time.time()
        if event == "call":
            self.streak = 0
        elif event == "provider_error":
            self.streak += 1
            if d.get("kind") in LIMITS:
                self.limits[str(d.get("model"))] = d["kind"]
        note = self.note(event, d)
        if note:
            t = r.totals()
            try:
                self.out("agent_progress", tokens=t["input_tokens"] + t["output_tokens"], usd=round(t["usd"], 6),
                         note=_clip(note, 160))
            except Exception:
                pass                              # progress is best effort: a broken dashboard never stops work
        if event == "provider_error":
            last = f"{d.get('kind')} {_clip(d.get('detail', ''), 160)}"
            if self.limits and not any(r._healthy(m, now) for m in r.models):
                raise _Stop("limited", f"every model on this lane is limited (last: {last})")
            if self.streak >= self.max_errors:
                raise _Stop("failed", f"{self.streak} provider errors in a row (last: {last})")
        if now > self.deadline and event not in ("session_start", "session_end"):
            raise _Stop("failed", f"timed out after {self.timeout:.0f}s")
        return {}


# ------------------------------------------------------------------ the lane
class RelayLane(Lane):
    kind = "relay"
    hint = ""                                     # appended when the lane is not available

    def __init__(self, name: str | None = None, cfg: dict | None = None):
        super().__init__(name, cfg)
        self._lock = threading.Lock()
        self._hold: dict[str, tuple[float, int]] = {}   # model id -> (usable again at, error count)

    def sub(self) -> dict:
        """This lane's subscription entry from the burn config ({} when there is none)."""
        return next((s for s in (self.cfg.get("_root") or {}).get("subscriptions", [])
                     if s.get("name") == self.subscription), {})

    def allowed(self, provider: str) -> bool:
        names = self.cfg.get("providers") or self.sub().get("providers")
        return not names or provider in names

    def ladder(self) -> dict:
        """relay.json-shaped config for this lane: the burn config's ladder (else relay.config.DEFAULT), kept to
        the providers this lane may spend."""
        root = self.cfg.get("_root") or {}
        for src in ([root] if root.get("providers") and root.get("models") else []) + [DEFAULT]:
            models = [m for m in src["models"] if m.get("provider") in src["providers"] and self.allowed(m["provider"])]
            if models:
                break
        used = {m["provider"] for m in models}
        knobs = {k: v for k, v in {**src, **root, **self.cfg}.items() if k in KNOBS}
        return {**knobs, "providers": {k: v for k, v in src["providers"].items() if k in used}, "models": models}

    def available(self) -> tuple[bool, str]:
        """Ok when a provider on the ladder has its API key set, or needs none. Env checks only, no network."""
        try:
            provs = [Provider(name=n, **p) for n, p in self.ladder()["providers"].items()]
        except (KeyError, TypeError, ValueError) as e:
            return False, f"bad provider config: {e}"
        if not provs:
            return False, "no models on the ladder for this lane's providers" + self.hint
        checks = [(p.name, *p.available()) for p in provs]
        ok = [n for n, good, _ in checks if good]
        if ok:
            return True, "ok: " + ", ".join(ok)
        return False, "; ".join(f"{n}: {why}" for n, _, why in checks) + self.hint

    def run(self, task: SwarmTask, workdir: str, emit: Emit, timeout: float = 1800) -> LaneResult:
        try:
            cfg = self.ladder()
            router = build_router(cfg)
        except (KeyError, TypeError, ValueError) as e:
            return LaneResult("failed", _clip(f"bad relay lane config: {e}"))
        now = time.time()
        with self._lock:                          # a model that just said stop stays parked for this lane
            for mid, (until, errors) in self._hold.items():
                if until > now and mid in router.state:
                    router.state[mid].cooldown_until, router.state[mid].errors = until, errors
        if not any(router._healthy(m, now) for m in router.models):
            return self._result(router, "no_capacity", "", 0)
        for p in router.providers.values():
            p.timeout = min(p.timeout, max(30.0, timeout))
        tools = GuardedToolbox(workdir)
        tools.max_timeout = int(min(tools.max_timeout, max(10, timeout)))
        feed = _Feed(router, tools, emit, timeout, int(self.cfg.get("max_errors", 6)))
        steps = int(cfg.get("max_steps", 40))
        try:
            agent = Agent(router, tools, feed, max_steps=steps, summary=False)
            with _quiet():
                final = agent.run(task_prompt(task))
            status, why = agent.outcome, final
        except _Stop as e:
            status, why = e.status, str(e)
        except Exception as e:                    # one broken task must never take the swarm down
            status, why = "failed", f"engine error: {type(e).__name__}: {e}"
        self._remember(router, feed.limits)
        return self._result(router, status, why, steps)

    def _result(self, router: Router, status: str, why: str, steps: int) -> LaneResult:
        until = None
        if status == "blocked":
            why = re.sub(r"^\s*BLOCKED:?\s*", "", why, flags=re.I)
        elif status == "refused":                 # upheld by the engine's triage: no model-shopping around it
            status, why = "blocked", f"refused: {why}"
        elif status == "max_steps":
            status, why = "failed", f"ran out of steps ({steps}) before finishing"
        elif status in ("no_capacity", "limited"):
            until = self._when(router) or (time.time() + 60 if status == "limited" else None)
            if until:
                status, why = "limited", why or "every model on this lane is rate- or budget-limited"
                self.limited_until = max(self.limited_until, until)
            else:
                status, why = "failed", "no model available: " + (
                    "; ".join(f"{k}: {v}" for k, v in router.dead_providers.items()) or "all retired")
        t = router.totals()
        model = max(router.models, key=lambda m: router.state[m.id].calls).id if t["calls"] else ""
        return LaneResult(status, _clip(why), t["input_tokens"], t["output_tokens"], round(t["usd"], 6), model, until)

    def _remember(self, router: Router, limits: dict[str, str]) -> None:
        """Carry this run's limits to the lane's later tasks, so none of them re-hits a model that said stop."""
        broke = {router.spec(mid).provider for mid, kind in limits.items() if kind == "budget" and mid in router.state}
        reset = self._budget_reset() if broke else 0.0
        with self._lock:
            for m in router.models:
                s = router.state[m.id]
                if m.provider in broke:
                    self._hold[m.id] = (reset, 0)
                elif limits.get(m.id) == "rate_limit":
                    self._hold[m.id] = (s.cooldown_until, s.errors)
                elif s.calls and not s.errors:
                    self._hold.pop(m.id, None)

    def _when(self, router: Router) -> float | None:
        """Soonest moment a model on this lane is usable again, when a limit (not a missing key) stops it."""
        now = time.time()
        with self._lock:
            times = [max(router.state[m.id].cooldown_until, self._hold.get(m.id, (0.0, 0))[0]) for m in router.models
                     if router.state[m.id].retired is None
                     and (m.provider not in router.dead_providers or self._hold.get(m.id, (0.0, 0))[0] > now)]
        return min(times) if times and min(times) > now else None

    def _budget_reset(self) -> float:
        """A burned budget refills at the subscription's monthly reset (UTC, `reset_day`), as relay.capacity counts."""
        day = min(int(self.sub().get("reset_day", 1)), 28)
        now = datetime.now(timezone.utc)
        nxt = now.replace(day=day, hour=0, minute=0, second=0, microsecond=0)
        if nxt <= now:
            y, m = divmod(now.month, 12)
            nxt = nxt.replace(year=now.year + y, month=m + 1)
        return nxt.timestamp()
