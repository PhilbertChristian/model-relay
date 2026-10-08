"""Orca lane: the task's agent runs in a visible Orca terminal, inside the swarm's worktree.

Orca is a desktop worktree/terminal app; its CLI schema is `orca agent-context --json`. It has no headless "run this
task" command (`orca --no-input <task>` does not exist), so the lane drives the real verbs:

  status                        available(): Orca is running and its runtime is reachable
  worktree show, repo add,      make sure Orca knows the worktree by its real path (a swarm worktree is a non-Orca
  repo set                      worktree, so the repo may need external worktrees shown)
  terminal create               a terminal in that worktree running `python -m relay.lanes.orca JOB`: the claude
                                lane's own argv (acceptEdits + allow/deny lists, never skip-permissions) through its
                                run_cli (scrubbed env, process group, timeout); output echoed there, logged for us
  terminal close                once the job has written its result file

Not `orchestration worker-start --agent`: that launches Orca's agent with the user's own launch settings, which the
lane cannot check for skip-permissions. No decision gates either: the swarm runs with no human in the loop, so a
BLOCKED reply is just status "blocked" with the reason in the summary, and the swarm moves on. Status, summary and
tokens come from the agent's output (the claude lane's parser, or the plain-output rules for a custom `agent` argv
with {prompt}/{workdir}); Orca never gets credit for work it did not do. Off unless the lane config says
"enabled": true. Binary: $ORCA_BIN, else the app bundle's CLI, else `orca` on PATH.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path

from ..contracts import Emit, LaneResult, SwarmTask, redact
from .base import Lane, task_prompt

APP_BIN = "/Applications/Orca.app/Contents/Resources/bin/orca"
ROOT = str(Path(__file__).resolve().parents[2])        # puts `relay` on the runner's PYTHONPATH


def orca_bin() -> str | None:
    """$ORCA_BIN, else the app bundle's CLI (the /usr/local/bin/orca symlink can be broken), else `orca` on PATH."""
    if os.environ.get("ORCA_BIN"):
        return os.environ["ORCA_BIN"]
    return APP_BIN if os.access(APP_BIN, os.X_OK) else shutil.which("orca")


def _first(text: str) -> str:
    return next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")


def _note(emit: Emit, text: str) -> None:
    try:
        emit("agent_progress", tokens=0, usd=0.0, note=redact(text)[:160])
    except Exception:
        pass


def _tail(path: str, pos: int, feed) -> int:
    """Feed the complete new lines of a growing log to `feed`; returns the new byte offset."""
    try:
        with open(path, "rb") as f:
            f.seek(pos)
            chunk = f.read()
    except OSError:
        return pos
    end = chunk.rfind(b"\n") + 1
    for line in chunk[:end].decode("utf-8", "replace").splitlines():
        if line.strip():
            feed(line)
    return pos + end


def unsafe_flag(argv: list[str]) -> str | None:
    """The first argv entry that would skip permissions or the sandbox (claude's or codex's forbidden flags)."""
    from .claude import FORBIDDEN as CLAUDE
    from .codex import FORBIDDEN as CODEX
    return next((a for a in map(str, argv) for f in CLAUDE + CODEX if a == f or a.startswith(f + "=")), None)


class OrcaLane(Lane):
    """cfg: enabled (must be true), agent ("claude" or argv), poll_s, progress_every, plus the claude lane's keys."""
    kind = "orca"

    def __init__(self, name: str | None = None, cfg: dict | None = None):
        super().__init__(name, cfg)
        self.agent = self.cfg.get("agent", "claude")
        self.poll = float(self.cfg.get("poll_s", 2.0))
        self._status: tuple[float, tuple[bool, str]] = (0.0, (False, ""))
        self._claude: Lane | None = None

    # ------------------------------------------------------------------ orca CLI
    def orca(self, *args: str, timeout: float = 30) -> tuple[bool, dict | str]:
        """One `orca ... --json` call: (True, result) or (False, why). Never raises."""
        try:
            r = subprocess.run([orca_bin() or "orca", *args, "--json"], capture_output=True, text=True,
                               timeout=timeout, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as e:
            return False, redact(str(e))[:200]
        try:
            data = json.loads(r.stdout or "{}")
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        if r.returncode == 0 and data.get("ok", True):
            return True, data["result"] if isinstance(data.get("result"), dict) else {}
        err = data.get("error")
        why = (err.get("message") if isinstance(err, dict) else err) or _first(r.stderr) or _first(r.stdout)
        return False, redact(str(why or f"exit {r.returncode}"))[:200]

    def status(self) -> tuple[bool, str]:
        """`orca status`, cached for 30 s: is Orca running with its runtime reachable?"""
        at, res = self._status
        if time.time() - at > 30:
            if not orca_bin():
                res = (False, "Orca CLI not found (install Orca.app or set ORCA_BIN)")
            else:
                ok, r = self.orca("status", timeout=10)
                app, rt = (r.get("app") or {}, r.get("runtime") or {}) if ok else ({}, {})
                if not ok:
                    res = (False, f"Orca CLI failed: {r}")
                elif rt.get("reachable", True):
                    res = (True, "ok")
                elif app.get("running") is False:
                    res = (False, "Orca is not running (open Orca.app)")
                else:
                    res = (False, f"Orca runtime {rt.get('state') or 'unreachable'}")
            self._status = (time.time(), res)
        return res

    # ------------------------------------------------------------------ availability
    def delegate(self) -> Lane:
        """The claude lane whose argv, run_cli and stream parser this lane reuses."""
        if self._claude is None:
            from .claude import ClaudeLane
            self._claude = ClaudeLane(self.name, {**self.cfg, "kind": "claude"})
        return self._claude

    def ready(self) -> tuple[bool, str]:
        """Can the agent run? The claude lane's own check, or a safe custom argv whose program is on PATH."""
        a = self.agent
        if isinstance(a, list) and a:
            bad = unsafe_flag(a)
            if bad:
                return False, f"refusing unsafe flag {bad}"
            return (True, "ok") if shutil.which(str(a[0])) else (False, f"{a[0]} not on PATH")
        if a != "claude":
            return False, f'"agent" must be "claude" or a command argv list, not {a!r}'
        try:
            return self.delegate().available()
        except Exception as e:                       # the claude lane's module is missing or broken
            return False, f"{type(e).__name__}: {e}"

    def available(self) -> tuple[bool, str]:
        if self.cfg.get("enabled") is not True:
            return False, 'off by default: set "enabled": true on the orca lane'
        ok, why = self.status()
        if not ok:
            return False, why
        ok, why = self.ready()
        who = Path(str(self.agent[0])).name if isinstance(self.agent, list) and self.agent else str(self.agent)
        return (True, f"Orca ready; {who} works in an Orca terminal") if ok else \
            (False, f"Orca ready, but {who}: {why}")

    # ------------------------------------------------------------------ a task
    def run(self, task: SwarmTask, workdir: str, emit: Emit, timeout: float = 1800) -> LaneResult:
        if self.is_limited():
            return LaneResult("limited", f"{self.name} is limited until its reset", limited_until=self.limited_until)
        ok, why = self.available()
        if not ok:
            return LaneResult("failed", f"orca lane unavailable: {why}")
        from .claude import Progress, check_workdir
        path = os.path.realpath(workdir)
        why = check_workdir(path) or self.register(path, task.project.path)
        if why:
            return LaneResult("failed", f"Orca cannot host this worktree: {why}")
        d = tempfile.mkdtemp(prefix="relay-orca-")
        try:
            progress = Progress(emit, float(self.cfg.get("progress_every", 0.5)))
            argv, scrub, feed, finish = self.job(task, path, progress)
            res = self.watch(task, path, d, {"argv": argv, "cwd": path, "timeout": timeout, "scrub": scrub,
                                              "claude": self.agent == "claude"}, feed, finish, emit, timeout)
        except Exception as e:                       # one broken task must never take the swarm down
            res = LaneResult("failed", redact(f"orca lane: {e}")[:300])
        finally:
            shutil.rmtree(d, ignore_errors=True)
        if res.status == "limited" and res.limited_until:
            self.limited_until = max(self.limited_until, res.limited_until)
        return res

    def register(self, path: str, repo: str) -> str:
        """'' once Orca knows the worktree at `path`, adding its repo / showing non-Orca worktrees only if needed."""
        sel, repo = f"path:{path}", os.path.realpath(repo)
        for fix in ((), ("repo", "add", "--path", repo),
                    ("repo", "set", "--repo", f"path:{repo}", "--external-worktree-visibility", "show")):
            if fix:
                self.orca(*fix)                      # "already added" is fine: the next show decides
            ok, why = self.orca("worktree", "show", "--worktree", sel)
            if ok:
                return ""
        return str(why)

    def job(self, task: SwarmTask, path: str, progress):
        """(argv, scrub env?, feed(line), finish(rc, err, timed_out, timeout) -> LaneResult) for the agent."""
        from .claude import _Stream, outcome
        if self.agent == "claude":
            lane = self.delegate()
            argv = [shutil.which(lane.bin) or lane.bin, *lane.args(task)]
            st = _Stream(path, progress, lane.model or "claude")
            feed, finish, scrub = st.feed, st.finish, True
        else:
            prompt = task_prompt(task)
            argv = [str(a).replace("{prompt}", prompt).replace("{workdir}", path) for a in self.agent]
            argv += [] if any("{prompt}" in str(a) for a in self.agent) else [prompt]
            lines: deque[str] = deque(maxlen=40)
            scrub = False                            # a custom agent CLI may need its own key from the env

            def feed(line: str) -> None:
                lines.append(line)
                progress(0, 0.0, line)

            def finish(rc, err, timed_out, timeout) -> LaneResult:
                return outcome(lines[-1] if lines else "", redact("\n".join([*list(lines)[-10:], err]))[-600:], rc=rc,
                               errored=timed_out or rc != 0, timed_out=timed_out, timeout=timeout,
                               cli=Path(argv[0]).name)
        bad = unsafe_flag(argv)
        if bad:
            raise ValueError(f"refusing to run an agent with {bad}")
        return argv, scrub, feed, finish

    def watch(self, task: SwarmTask, path: str, d: str, job: dict, feed, finish, emit: Emit,
              timeout: float) -> LaneResult:
        """Start the job in an Orca terminal, follow its log, then turn the end of it into a LaneResult."""
        log, done = os.path.join(d, "out.log"), os.path.join(d, "done.json")
        Path(d, "job.json").write_text(json.dumps({**job, "log": log, "done": done}))
        cmd = shlex.join(["env", f"PYTHONPATH={ROOT}", sys.executable, "-m", "relay.lanes.orca",
                          str(Path(d, "job.json"))])
        ok, r = self.orca("terminal", "create", "--worktree", f"path:{path}", "--title", f"burn {task.id}"[:60],
                          "--command", cmd)
        if not ok:                                   # a runner that still starts late finds no job: run() deletes d
            return LaneResult("failed", f"Orca did not open a terminal: {r}")
        handle = str((r.get("terminal") or {}).get("handle") or "")    # created without a handle: still follow it
        _note(emit, f"working in Orca terminal {handle or '(no handle)'}")
        try:
            pos, deadline = 0, time.time() + timeout + 60
            while not os.path.exists(done) and time.time() < deadline:
                time.sleep(self.poll)
                pos = _tail(log, pos, feed)
            _tail(log, pos, feed)
            try:
                end = json.loads(Path(done).read_text())
            except (OSError, ValueError):
                end = {"rc": None, "err": f"no result from Orca terminal {handle}", "timed_out": True}
        finally:                                     # closing the terminal also stops a job still running
            if handle:
                self.orca("terminal", "close", "--terminal", handle)
        return finish(end.get("rc"), str(end.get("err") or ""), bool(end.get("timed_out")), timeout)


# ---------------------------------------------------------------------- inside the Orca terminal
def _main(job_file: str) -> int:
    """Run the job's agent through the claude lane's run_cli: echo it (readably, for claude), log it, mark done."""
    from .claude import _Stream, run_cli
    job = json.loads(Path(job_file).read_text())
    show = lambda s: print(redact(s), flush=True)      # noqa: E731
    if job.get("claude"):
        show = _Stream(job["cwd"], lambda tok, usd, note: print(redact(f"[{tok:,} tok ${usd:.2f}] {note}"),
                                                                flush=True), "claude").feed
    print(f"relay burn-week · {Path(job['argv'][0]).name} in {job['cwd']}", flush=True)
    try:
        with open(job["log"], "w", encoding="utf-8") as log:
            def line(s: str) -> None:
                log.write(s + "\n")
                log.flush()
                show(s)
            rc, err, timed_out = run_cli(job["argv"], job["cwd"], float(job["timeout"]), line,
                                         env=None if job["scrub"] else dict(os.environ))
    except Exception as e:                           # agent never started: say so, don't leave the lane waiting
        rc, err, timed_out = None, f"{type(e).__name__}: {e}", False
    Path(job["done"] + ".tmp").write_text(json.dumps({"rc": rc, "err": err[-4000:], "timed_out": timed_out}))
    os.replace(job["done"] + ".tmp", job["done"])
    print(f"relay: agent exited {rc}{' (timed out)' if timed_out else ''}; the swarm takes it from here", flush=True)
    return 0


if __name__ == "__main__":
    for _sig in (signal.SIGHUP, signal.SIGTERM):    # terminal closed: run_cli then takes the agent's group down too
        signal.signal(_sig, lambda *_: sys.exit(1))
    sys.exit(_main(sys.argv[1]))
