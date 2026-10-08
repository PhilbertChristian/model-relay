"""Codex lane: `codex exec --json --sandbox workspace-write` inside the task's worktree.

Never --dangerously-bypass-approvals-and-sandbox / --yolo. The workspace-write sandbox (network forced off,
so no push or curl) is Codex's deny list. Same scrubbed env, process group, timeout and limit handling as
the Claude lane. JSONL parsing is tolerant: thread/item events, or the older {"msg": {...}} events.
"""
from __future__ import annotations

import shutil
import time
from collections import deque

from ..contracts import Emit, LaneResult, SwarmTask, redact
from .base import Lane, task_prompt
from .claude import Progress, check_workdir, jsonl, num, outcome, rel, run_cli, safe_model, short

FORBIDDEN = ("--dangerously-bypass-approvals-and-sandbox", "--yolo", "danger-full-access")


def _d(v) -> dict:
    return v if isinstance(v, dict) else {}


class _Stream:
    """Folds `codex exec --json` lines into progress events and a LaneResult."""

    def __init__(self, workdir: str, progress: Progress, price: tuple[float, float]):
        self.workdir, self.progress, self.price = workdir, progress, price
        self.inp = self.out = 0
        self.text, self.errors, self.failed, self.completed = "", [], False, False
        self.resets: float | None = None         # from rate-limit snapshots that hit 100%
        self.raw: deque[str] = deque(maxlen=12)

    def usd(self) -> float:
        return (self.inp * self.price[0] + self.out * self.price[1]) / 1e6

    def feed(self, line: str) -> None:
        ev = jsonl(line)
        if ev is None:
            self.raw.append(line)
            return
        msg = _d(ev.get("msg")) or ev
        kind, item = str(msg.get("type") or ""), _d(msg.get("item"))
        it = str(item.get("type") or kind)
        usage = (_d(msg.get("usage")) or _d(_d(msg.get("info")).get("total_token_usage"))
                 or (msg if kind == "token_count" else {}))
        if "input_tokens" in usage or "output_tokens" in usage:      # cumulative snapshots: keep the largest
            self.inp = max(self.inp, num(usage.get("input_tokens")))
            self.out = max(self.out, num(usage.get("output_tokens")))
        for w in map(_d, _d(msg.get("rate_limits")).values()):
            if num(w.get("used_percent")) >= 100:
                secs = num(w.get("resets_in_seconds"))
                self.resets = max(self.resets or 0, num(w.get("resets_at")) or (time.time() + secs if secs else 0)) or None
        note = ""
        if it == "agent_message" or kind == "task_complete":
            text = item.get("text") or msg.get("message") or msg.get("last_agent_message") or ""
            if isinstance(text, str) and text.strip():
                self.text = text.strip()
                note = self.text.splitlines()[0]
        elif it in ("command_execution", "exec_command_begin"):
            cmd = item.get("command") or msg.get("command") or ""
            note = "$ " + (" ".join(map(str, cmd)) if isinstance(cmd, list) else str(cmd))
        elif it in ("file_change", "patch_apply_begin"):
            ch = item.get("changes") or msg.get("changes") or []
            paths = [str(c.get("path", "")) for c in ch if isinstance(c, dict)] if isinstance(ch, list) else list(_d(ch))
            note = "editing " + ", ".join(rel(p, self.workdir) for p in paths[:3])
        elif it in ("reasoning", "agent_reasoning"):
            lines = str(item.get("text") or msg.get("text") or "").strip().splitlines()
            note = "thinking: " + (lines[0].strip("*# ") if lines else "")
        elif it == "mcp_tool_call":
            note = f"tool {item.get('server', '')}/{item.get('tool', '')}"
        elif it == "web_search":
            note = f"searching the web: {item.get('query', '')}"
        if kind in ("error", "turn.failed", "stream_error") or it == "error":
            e = str(msg.get("message") or _d(msg.get("error")).get("message") or item.get("message") or "error")
            self.errors.append(e)
            self.failed |= kind == "turn.failed"
            note = note or f"error: {e}"
        self.completed |= kind in ("turn.completed", "task_complete")
        if note or usage:
            self.progress(self.inp + self.out, self.usd(), note)

    def finish(self, rc: int | None, err: str, timed_out: bool, timeout: float, model: str) -> LaneResult:
        # an error without a completed turn is fatal; one followed by turn.completed was a retried hiccup
        errored = (timed_out or rc != 0 or self.failed or (bool(self.errors) and not self.completed)
                   or not (self.completed or self.text))
        summary = self.errors[-1] if errored and self.errors else self.text
        tail = redact("\n".join([*self.errors[-3:], *self.raw, err]).strip())[-600:]
        return outcome(summary, tail, rc=rc, errored=errored, timed_out=timed_out, timeout=timeout, cli="codex",
                       fallback=self.resets, input_tokens=self.inp, output_tokens=self.out, usd=round(self.usd(), 6),
                       model=model)


class CodexLane(Lane):
    """cfg: model, bin, progress_every, price_in / price_out (USD per 1M tokens; usd stays 0 without them)."""
    kind = "codex"

    def __init__(self, name: str | None = None, cfg: dict | None = None):
        super().__init__(name, cfg)
        self.bin = str(self.cfg.get("bin") or "codex")
        self.model = safe_model(self.cfg.get("model"))

    def available(self) -> tuple[bool, str]:
        exe = shutil.which(self.bin)
        return (True, f"codex CLI at {exe}") if exe else (False, f"{self.bin} not found on PATH (install Codex CLI)")

    def args(self, task: SwarmTask) -> list[str]:
        """argv after the executable: workspace-write sandbox with network off, never the bypass flags."""
        argv = ["exec", "--json", "--sandbox", "workspace-write", "-c", "sandbox_workspace_write.network_access=false"]
        argv += ["--model", self.model] if self.model else []
        argv.append(task_prompt(task))
        if any(a == f or a.startswith(f + "=") for a in argv for f in FORBIDDEN):
            raise ValueError("refusing to build a codex command that bypasses the sandbox")
        return argv

    def run(self, task: SwarmTask, workdir: str, emit: Emit, timeout: float = 1800) -> LaneResult:
        model = self.model or "codex"
        if self.is_limited():
            return LaneResult("limited", f"{self.name} is limited until its reset", model=model,
                              limited_until=self.limited_until)
        exe = shutil.which(self.bin)
        why = check_workdir(workdir) or ("" if exe else f"{self.bin} not found on PATH")
        if why:
            return LaneResult("failed", why, model=model)
        price = (float(self.cfg.get("price_in") or 0), float(self.cfg.get("price_out") or 0))
        st = _Stream(workdir, Progress(emit, float(self.cfg.get("progress_every", 0.5))), price)
        try:
            rc, err, timed_out = run_cli([exe, *self.args(task)], workdir, timeout, st.feed)
        except (OSError, ValueError) as e:
            return LaneResult("failed", short(redact(f"could not start codex: {e}"), 300), model=model)
        return st.finish(rc, err, timed_out, timeout, model)   # the swarm sets self.limited_until
