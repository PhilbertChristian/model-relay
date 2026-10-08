"""burn-week swarm: a paced pool of agents that spends this week's leftover subscription capacity on your repos.

    discover (repos + TODOs) ─┐
    ideas (local, opt-in) ────┼─> build_queue ─> pace (agents per lane) ─> run_swarm: agent slots 1..N
    usage (left per sub) ─────┘   per task: worktree.create -> enrich (Monid) -> lane.run -> worktree.finalize
                                  every event -> Bus (.relay/events.jsonl, dash, web)                  -> BURN.md

Each agent works in its own git worktree on a local `relay/burn/<task-id>` branch (relay/worktree.py): the user's
checkout and remotes are never touched. A lane that reports a usage limit gets no more work until its reset, and a
task is never retried on a lane that was limited on it.
"""
from __future__ import annotations

import bisect
import hashlib
import importlib
import re
import shlex
import threading
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import is_dataclass, replace
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue

from . import worktree
from .bus import Bus
from .contracts import LaneResult, ProjectInfo, SwarmTask, Usage, redact, to_dict
from .lanes.base import Lane
from .plan import parse as parse_plan

SOURCES = ("plan", "todo", "idea", "generic")         # queue priority, highest first
STATUSES = ("done", "blocked", "failed", "limited")
SWARM_EVENTS = {"swarm_start", "pace", "task_queued", "agent_start", "agent_end", "lane_limited", "swarm_end"}
FIN_KEYS = ("commit", "diffstat", "files", "insertions", "deletions", "tests_ok", "tests_tail")


def _mod(name: str):
    """Sibling module relay.<name>, or None while it doesn't exist."""
    try:
        return importlib.import_module(f"relay.{name}")
    except ModuleNotFoundError as e:
        if e.name != f"relay.{name}":
            raise
        return None


def _need(name: str):
    mod = _mod(name)
    if mod is None:
        raise RuntimeError(f"relay.{name} is missing")
    return mod


def _key(path) -> str:
    return str(Path(str(path)).expanduser().resolve())


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", " ", text.lower()).strip()


def _scrub(x):
    """Redact every string in a nested event payload (Bus only redacts top-level strings)."""
    if isinstance(x, str):
        return redact(x)
    if isinstance(x, dict):
        return {k: _scrub(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_scrub(v) for v in x]
    return x


def _num(v, cast=float):
    try:
        return cast(v or 0)
    except (TypeError, ValueError):
        return cast(0)


# ── queue ─────────────────────────────────────────────────────────────────────────────────────────────────────

def build_queue(projects, ideas=None, plan_path: str | None = None, per_project: int = 3,
                limit: int | None = None) -> list[SwarmTask]:
    """SwarmTasks, highest priority first: plan items, then project TODOs, then mined ideas, then generic chores.
    Within a source, projects take turns in discovery order. Duplicate texts are dropped per project, ignoring
    case and punctuation. Each project gets at most `per_project` tasks. Plan items are the user's explicit list,
    so they are never cut, but they count toward the cap. `limit` caps the whole queue."""
    projects, cap = list(projects or []), 10 ** 9 if per_project is None else int(per_project)
    cands: list[tuple[tuple, ProjectInfo, str]] = []          # ((source, rank, project order), project, text)
    if plan_path:
        items, overrides = _plan_items(plan_path, projects)
        projects = [overrides.get(_key(p.path), p) for p in projects]
        cands += [((0, i, 0), p, text) for i, (p, text) in enumerate(items)]
    for i, p in enumerate(projects):
        cands += [((1, rank, i), p, text) for rank, text in enumerate(p.todos or [])]
    cands += _idea_items(ideas, projects)
    discover = _mod("discover")
    if discover:
        for i, p in enumerate(projects):
            cands += [((3, rank, i), p, text) for rank, text in enumerate(discover.generic_tasks(p) or [])]
    cands.sort(key=lambda c: c[0])
    out, seen, ids, count = [], set(), set(), Counter()
    for (src, _, _), p, text in cands:
        text, repo = redact(" ".join(str(text).split()))[:300], _key(p.path)
        if not text or (repo, _norm(text)) in seen or (src and count[repo] >= cap):
            continue
        seen.add((repo, _norm(text)))
        count[repo] += 1
        tid = base = f"{p.slug}-{hashlib.sha1(text.encode()).hexdigest()[:6]}"
        n = 2
        while tid in ids:                                     # two repos with one name and one chore
            tid, n = f"{base}-{n}", n + 1
        ids.add(tid)
        out.append(SwarmTask(id=tid, project=p, text=text, source=SOURCES[src]))
    out = out if limit is None else out[:max(0, int(limit))]
    for i, t in enumerate(out):
        t.priority = float(len(out) - i)
    return out


def _plan_items(plan_path: str, projects: list[ProjectInfo]):
    """Plan todo items on local repos -> ([(project, text)], {repo: project carrying the plan's `test:`})."""
    plan = parse_plan(plan_path)
    items, mapped = [], {}
    for pp, task in plan.queue:
        if pp.name not in mapped:
            mapped[pp.name] = _plan_project(pp, projects, plan.path.parent)
        if mapped[pp.name]:
            items.append((mapped[pp.name], task.text))
    return items, {_key(p.path): p for p in mapped.values() if p}


def _plan_project(pp, projects: list[ProjectInfo], base: Path) -> ProjectInfo | None:
    """A plan's `## project` -> the local repo it names (`dir:`, else by name). Never clones a `repo:` URL."""
    hit = None
    if pp.dir:
        d = Path(pp.dir).expanduser()
        d = d if d.is_absolute() else base / d
        hit = next((p for p in projects if _key(p.path) == _key(d)), None)
        if hit is None and (d / ".git").exists():
            discover = _mod("discover")
            hit = discover.inspect_project(_key(d)) if discover else ProjectInfo(path=_key(d), name=pp.name)
    else:
        hit = next((p for p in projects if p.slug == pp.slug or p.name == pp.name), None)
    return replace(hit, test_cmd=pp.test) if hit and pp.test else hit


def _idea_items(ideas, projects: list[ProjectInfo]) -> list:
    """Ideas whose conversation happened inside a known repo (the deepest match wins), best score first."""
    if not ideas:
        return []
    if not isinstance(ideas, dict):
        grouped = defaultdict(list)
        for idea in ideas:
            grouped[idea.project_dir or ""].append(idea)
        ideas = grouped
    roots = [(Path(_key(p.path)), i) for i, p in enumerate(projects)]
    per = defaultdict(list)
    for d, items in ideas.items():
        if d:                                                 # "" = unknown dir: no repo to put it in
            dp = Path(_key(d))
            hits = [(len(r.parts), i) for r, i in roots if r == dp or r in dp.parents]
            if hits:
                per[max(hits)[1]].extend(items)
    return [((2, rank, i), projects[i], idea.text) for i, items in per.items()
            for rank, idea in enumerate(sorted(items, key=lambda x: -(x.score or 0)))]


# ── swarm ─────────────────────────────────────────────────────────────────────────────────────────────────────

def _available(lane: Lane) -> tuple[bool, str]:
    try:
        ok, why = lane.available()
        return bool(ok), str(why or "")
    except Exception as e:
        return False, redact(f"{type(e).__name__}: {e}")


def _coerce(res) -> LaneResult:
    """Whatever a lane returned -> a LaneResult with a known status. Anything else counts as failed."""
    if isinstance(res, dict):
        try:
            res = LaneResult(**{k: v for k, v in res.items() if k in LaneResult.__dataclass_fields__})
        except TypeError:
            return LaneResult("failed", "lane returned a dict without a status")
    if not isinstance(res, LaneResult):
        return LaneResult("failed", f"lane returned {type(res).__name__}, not a LaneResult")
    if res.status not in STATUSES:
        res = replace(res, status="failed", summary=f"unknown lane status {res.status!r}: {res.summary}")
    return replace(res, summary=redact(str(res.summary or "")).strip()[:500])


def _message(task: SwarmTask, lane: str, status: str, summary: str) -> str:
    subject = ("" if status == "done" else f"WIP ({status}): ") + task.text
    subject = subject if len(subject) <= 72 else subject[:69].rstrip() + "..."
    body = [f"Task: {task.text}", f"Project: {task.project.name} · source: {task.source} · lane: {lane}"]
    body += [f"Summary: {summary}"] if summary else []
    return redact(subject + "\n\n" + "\n".join(body + [f"Relay-Task: {task.id}"]))


def _outcome(status: str, summary: str) -> dict:
    return {"status": status, "summary": summary, "commit": None, "diffstat": "", "files": 0, "insertions": 0,
            "deletions": 0, "tests_ok": None, "tests_tail": ""}


def _as_dict(obj) -> dict:
    return to_dict(obj) if is_dataclass(obj) else dict(obj) if isinstance(obj, dict) else dict(vars(obj))


def _usage_for(lane: Lane, usages: list[Usage]) -> Usage | None:
    """The subscription a lane burns: by name, else the one whose `lane` names this lane."""
    return next((u for u in usages if u.name == lane.subscription), None) or \
        next((u for u in usages if u.lane in (lane.name, lane.kind)), None)


def _stop(lanes: list[Lane], usages: list[Usage], deadline: float | None) -> float | None:
    """When the whole swarm stops: the caller's deadline, else the latest reset among the lanes being burned
    (plans reset on different clocks; each lane also stops at its own reset)."""
    resets = [u.resets_at for l in lanes if (u := _usage_for(l, usages)) is not None]
    return deadline if deadline is not None else max(resets) if resets else None


def _plan_for(lane: Lane, plans):
    return next((p for p in plans if p.lane == lane.name), None) or \
        next((p for p in plans if p.subscription == lane.subscription), None)


class _Swarm:
    """One run_swarm call: a scheduler loop in the caller's thread and one daemon thread per running agent.
    Only the scheduler touches the queue, the slots and the books. Workers report back through `inbox`."""

    def __init__(self, cfg, queue, lanes, bus, data_dir, max_agents, deadline, usages, enrich):
        self.cfg, self.bus, self.enrich, self.deadline = cfg or {}, bus, enrich, deadline
        self.data_dir = str(Path(data_dir).expanduser())
        self.max_agents = max(1, int(max_agents or 1))
        self.usages = self.live = list(usages or [])
        self.target_pct = float((self.cfg.get("week") or {}).get("target_pct", 0.97))
        self.queue, ids = [], set()
        for t in sorted(queue, key=lambda t: -t.priority):       # stable: equal priorities keep queue order
            if t.id not in ids:
                ids.add(t.id)
                self.queue.append(t)
        self.pending = list(self.queue)
        self.lanes, self.skipped = [], []
        for lane in lanes:
            ok, why = _available(lane)
            if ok:
                self.lanes.append(lane)
            else:
                self.skipped.append({"name": lane.name, "kind": lane.kind, "subscription": lane.subscription,
                                     "as": lane.flavor, "available": False, "why": why})
        self.usage_of = {l.name: _usage_for(l, self.usages) for l in self.lanes}
        self.stop_at = {n: u.resets_at for n, u in self.usage_of.items() if u is not None}   # each lane's own reset
        self.deadline = _stop(self.lanes, self.usages, deadline)
        self.held = {l.name for l in self.lanes if l.is_limited()}                          # limited, for re-plans
        self.replan = False
        self.cap = {l.name: max(0, min(l.max_agents, self.max_agents)) for l in self.lanes}
        self.busy, self.free = Counter(), list(range(1, self.max_agents + 1))
        self.running: dict[str, dict] = {}
        self.inbox: Queue = Queue()
        self.tried: dict[str, set] = defaultdict(set)         # task id -> lanes that were limited on it
        self.places: dict[str, tuple[str, str]] = {}          # task id -> worktree kept for the next lane
        self.final: dict[str, dict] = {}
        self.by_lane = {l.name: {"tokens": 0, "usd": 0.0, "tasks": 0} for l in self.lanes}
        self.plans, self.lock = [], threading.Lock()

    def run(self) -> dict:
        self.t0 = self.last_plan = time.time()
        self.last_burn: dict = {}
        self.run_id = f"burn-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
        self.report = str(Path(self.cfg.get("report") or Path(self.data_dir) / "BURN.md").expanduser())
        self._plan(first=True)
        resets = sorted(self.stop_at.values())
        soonest = next((r for r in resets if r > self.t0), resets[0] if resets else self.deadline)
        self.bus.emit("swarm_start", run=self.run_id, max_agents=self.max_agents,
                      lanes=_scrub([{"name": l.name, "kind": l.kind, "subscription": l.subscription, "as": l.flavor}
                                    for l in self.lanes]),
                      queue=len(self.pending), usages=_scrub([to_dict(u) for u in self.usages]), resets_at=soonest)
        self._emit_pace()
        for t in self.pending:
            self.bus.emit("task_queued", task_id=t.id, project=t.project.name, text=t.text, source=t.source)
        try:
            reason = self._loop()
        except KeyboardInterrupt:
            self._abandon("interrupted (Ctrl-C) while running; partial work left in its worktree")
            reason = "interrupted"
        self._wrap_up()
        result = self._result(reason)
        try:
            write_report(result, self.report)
        except OSError as e:                                  # no report is bad; no swarm_end would be worse
            result["report_error"] = redact(str(e))
        self.bus.emit("swarm_end", **{k: _scrub(result[k]) for k in ("run", "done", "blocked", "failed", "limited",
                                                                      "tokens", "usd", "by_lane", "report", "seconds",
                                                                      "reason")})
        return result

    # scheduler (caller's thread)
    def _loop(self) -> str:
        tick = max(0.01, float(self.cfg.get("tick", 0.25)))
        every = float(self.cfg.get("repace_seconds", 300))
        grace = float(self.cfg.get("grace_seconds", 600))
        while True:
            now = time.time()
            if self.deadline is not None and now >= self.deadline:
                if not self.running:
                    return "deadline" if self.pending else "queue empty"
                if now >= self.deadline + grace:
                    self._abandon("still running at the deadline; partial work left in its worktree")
                    return "deadline"
            else:
                lifted = {l.name for l in self.lanes if l.name in self.held and not l.is_limited(now)}
                self.held -= lifted                           # a limit hit or lifted: re-plan now, not in 5 min
                if self.usages and (lifted or self.replan or now - self.last_plan >= every) and self._plan():
                    self._emit_pace()
                self.replan = False
                self._fill(now)
                if not self.running:
                    if not self.pending:
                        return "queue empty"
                    why = self._stuck(now)
                    if why:
                        return why
            self._drain(tick)

    def _fill(self, now: float) -> None:
        while self.free and self.pending:
            for i, task in enumerate(self.pending):
                lane = self._pick(task, now)
                if lane:
                    del self.pending[i]
                    self._launch(task, lane)
                    break
            else:
                return

    def _end(self, lane: Lane) -> float | None:
        """When this lane stops launching: its subscription's reset or the swarm's deadline, whichever is first."""
        ends = [e for e in (self.deadline, self.stop_at.get(lane.name)) if e is not None]
        return min(ends) if ends else None

    def _past(self, lane: Lane, now: float) -> bool:
        end = self.stop_at.get(lane.name)
        return end is not None and now >= end

    def _pick(self, task: SwarmTask, now: float) -> Lane | None:
        """The least-loaded lane, relative to its cap, that may take this task now."""
        ok = [l for l in self.lanes if self.busy[l.name] < self.cap[l.name] and not l.is_limited(now)
              and not self._past(l, now) and l.name not in self.tried[task.id]]
        return min(ok, key=lambda l: self.busy[l.name] / self.cap[l.name], default=None)

    def _stuck(self, now: float) -> str:
        """Why nothing pending can ever run, or "" while some lane's limit lifts before that lane has to stop."""
        if not self.lanes:
            return "no lanes available"
        for l in self.lanes:
            end = self._end(l)
            if l.is_limited(now) and end is not None and l.limited_until < end \
                    and any(l.name not in self.tried[t.id] for t in self.pending):
                return ""
        open_ = [l for l in self.lanes if not self._past(l, now)]
        if not open_:
            return "all lanes past their reset"
        if all(l.is_limited(now) or all(l.name in self.tried[t.id] for t in self.pending) for l in open_):
            return "all lanes limited"
        return "no capacity left on any lane"

    def _launch(self, task: SwarmTask, lane: Lane) -> None:
        slot = self.free.pop(0)
        self.busy[lane.name] += 1
        task.attempts += 1
        rec = {"slot": slot, "task": task, "lane": lane, "t0": time.time(), "tokens": 0, "usd": 0.0,
               "place": self.places.pop(task.id, None), "branch": "", "worktree": "", "state": "running"}
        self.running[task.id] = rec
        threading.Thread(target=self._work, args=(rec,), name=f"relay-agent-{slot}", daemon=True).start()

    def _drain(self, timeout: float) -> None:
        try:
            item = self.inbox.get(timeout=timeout) if timeout > 0 else self.inbox.get_nowait()
        except Empty:
            return
        while True:
            self._settle(*item)
            try:
                item = self.inbox.get_nowait()
            except Empty:
                return

    def _settle(self, rec: dict, out: dict) -> None:
        """Free the slot, book the burn, and requeue a task whose lane hit its limit (for another lane)."""
        task, lane = rec["task"], rec["lane"]
        self.running.pop(task.id, None)
        self.busy[lane.name] -= 1
        bisect.insort(self.free, rec["slot"])
        book = self.by_lane[lane.name]
        book["tokens"] += rec["tokens"]
        book["usd"] = round(book["usd"] + rec["usd"], 6)
        book["tasks"] += 1
        if out["status"] == "limited" and lane.name not in self.held:
            self.held.add(lane.name)
            self.replan = True                                # pace gives a limited lane 0 agents
        if out["status"] == "limited" and rec.get("keep"):
            self.tried[task.id].add(lane.name)
            self.places[task.id] = rec["keep"]
            self.pending.insert(0, task)
        prev = self.final.get(task.id, {})
        self.final[task.id] = {"task_id": task.id, "project": task.project.name, "repo": task.project.path,
                               "text": task.text, "source": task.source, "lane": lane.name, "branch": rec["branch"],
                               "worktree": rec["worktree"], "attempts": task.attempts,
                               "tokens": prev.get("tokens", 0) + rec["tokens"],
                               "usd": round(prev.get("usd", 0.0) + rec["usd"], 6),
                               "seconds": round(prev.get("seconds", 0.0) + time.time() - rec["t0"], 1), **out}

    def _abandon(self, why: str) -> None:
        """Stop waiting for agents that are still out (their threads are daemons) and book them as failed."""
        self._drain(0)
        for rec in list(self.running.values()):
            with self.lock:
                if rec["state"] != "running":
                    continue
                rec["state"] = "abandoned"
            out = _outcome("failed", why)
            if not rec.get("started"):
                self._emit_start(rec)
            self._emit_end(rec, out)
            self._settle(rec, out)
        until = time.time() + 5                                # finished ones are on their way to the inbox
        while self.running and time.time() < until:
            self._drain(0.05)

    def _wrap_up(self) -> None:
        """A task that ended limited with no lane left to take it keeps its partial work as a WIP commit."""
        for task_id, (path, _branch) in list(self.places.items()):
            task, row = next((t for t in self.pending if t.id == task_id), None), self.final.get(task_id)
            if task is None or row is None:
                continue
            try:
                fin = worktree.finalize(path, _message(task, row["lane"], "limited", row.get("summary", "")), None)
                row.update({k: fin.get(k) for k in FIN_KEYS})
                if not self.cfg.get("keep_worktrees"):
                    worktree.remove(task.project.path, path)
            except Exception as e:
                row["summary"] = redact(f"{row.get('summary', '')} (partial work left in {path}: {e})")[:500]
            self.pending.remove(task)
            del self.places[task_id]

    # pacing
    def _burned(self) -> dict:
        """Tokens/usd per lane so far: settled attempts plus what running agents have reported."""
        with self.lock:
            burned = {n: {"tokens": b["tokens"], "usd": b["usd"]} for n, b in self.by_lane.items()}
            for rec in self.running.values():
                burned[rec["lane"].name]["tokens"] += rec["tokens"]
                burned[rec["lane"].name]["usd"] += rec["usd"]
        return burned

    def _spent(self, u: Usage, burned: dict) -> float:
        """What this run burned from subscription `u`, in its unit."""
        unit = {"tokens": "tokens", "usd": "usd"}.get(u.unit)
        return float(sum(burned[n][unit] for n, v in self.usage_of.items() if v is u)) if unit else 0.0

    def _plan(self, first: bool = False) -> bool:
        """Pace plans -> per-lane caps: relay.pace's plan first, then pace.adjust from the observed burn rate.
        All or nothing: if pace fails, the current caps stay."""
        pace = _mod("pace") if self.usages else None
        if pace is None:
            return False
        now, burned, caps = time.time(), self._burned(), dict(self.cap)
        live = [replace(u, used=u.used + self._spent(u, burned)) for u in self.usages]
        try:
            plans = pace.plan_pace(live, self.lanes, max_agents=self.max_agents,      # Lane objects: pace reads
                                   target_pct=self.target_pct, now=now)               # limited_until -> 0 agents
            hours = max(now - self.last_plan, 1.0) / 3600
            for lane in self.lanes:
                plan, hi = _plan_for(lane, plans), min(lane.max_agents, self.max_agents)
                if plan is None:
                    continue
                if first or plan.agents <= 0 or hi <= 0:
                    cap = min(int(plan.agents), hi)
                else:
                    unit = "usd" if plan.unit == "usd" else "tokens"
                    rate = (burned[lane.name][unit] - self.last_burn.get(lane.name, {}).get(unit, 0)) / hours
                    cap = pace.adjust(self.cap[lane.name], rate, plan.target_rate, lo=1, hi=hi)
                caps[lane.name] = max(0, int(cap))
        except Exception:
            self.last_plan = now                              # pacing is advice: try again next round
            return False
        self.cap, self.plans, self.live, self.last_plan, self.last_burn = caps, plans, live, now, burned
        return True

    def _emit_pace(self) -> None:
        for plan in self.plans:
            lane = next((l for l in self.lanes if _plan_for(l, [plan])), None)
            u = next((u for u in self.live if u.name == plan.subscription), None)
            self.bus.emit("pace", lane=lane.name if lane else plan.lane, subscription=plan.subscription,
                          unit=plan.unit, used=u.used if u else None, limit=u.limit if u else None,
                          pct=u.pct if u else None, remaining=plan.remaining,
                          agents=self.cap[lane.name] if lane else plan.agents, target_rate=plan.target_rate,
                          hours_left=plan.hours_left)

    # one agent (worker thread)
    def _work(self, rec: dict) -> None:
        """worktree -> brief -> lane.run -> finalize -> agent_end. Nothing in here can take the swarm down."""
        task, lane = rec["task"], rec["lane"]
        out = _outcome("failed", "")
        try:
            path, branch = rec["place"] or worktree.create(task.project.path, self.data_dir, task.id,
                                                           task.project.slug)
            rec.update(worktree=path, branch=branch)
            self._emit_start(rec)
            self._brief(rec)
            res = self._run_lane(rec, path)
            out.update(status=res.status, summary=res.summary)
            with self.lock:
                rec["tokens"], rec["usd"] = max(rec["tokens"], res.tokens), max(rec["usd"], float(res.usd or 0))
            if res.status == "limited":
                self._limited(lane, res)
                rec["keep"] = (path, branch)                  # the next lane carries on in this worktree
            else:
                out.update(self._finalize(rec, res, path))
        except BaseException as e:                            # even a lane calling sys.exit() frees its slot
            out.update(status="failed", summary=redact(f"{type(e).__name__}: {e}")[:300])
        with self.lock:
            if rec["state"] == "abandoned":
                return
            rec["state"] = "finished"
        if not rec.get("started"):
            self._emit_start(rec)
        self._emit_end(rec, out)
        self.inbox.put((rec, out))

    def _timeout(self, lane: Lane) -> float:
        """A task's time box: task_timeout, cut to the lane's reset or the deadline (at least a minute)."""
        t, end = float(self.cfg.get("task_timeout", 1800)), self._end(lane)
        return t if end is None else max(60.0, min(t, end - time.time()))

    def _binder(self, rec: dict):
        """The emit a lane gets: binds agent / task_id / lane, and goes quiet once the attempt is over."""
        live = [True]
        task, lane, slot = rec["task"], rec["lane"], rec["slot"]

        def emit(event: str, **data) -> None:
            if not live[0] or event in SWARM_EVENTS:
                return
            if event != "agent_progress":
                self.bus.emit(event, **{**_scrub(data), "agent": slot, "task_id": task.id, "lane": lane.name})
                return
            with self.lock:
                rec["tokens"] = max(rec["tokens"], _num(data.get("tokens"), int))
                rec["usd"] = max(rec["usd"], _num(data.get("usd")))
                tokens, usd = rec["tokens"], rec["usd"]
            self.bus.emit("agent_progress", agent=slot, task_id=task.id, lane=lane.name, tokens=tokens,
                          usd=round(usd, 6), note=str(data.get("note") or "")[:240])
        return emit, lambda: live.__setitem__(0, False)

    def _run_lane(self, rec: dict, path: str) -> LaneResult:
        emit, close = self._binder(rec)
        try:
            return _coerce(rec["lane"].run(rec["task"], path, emit, timeout=self._timeout(rec["lane"])))
        except Exception as e:
            return LaneResult("failed", redact(f"lane crashed: {type(e).__name__}: {e}")[:300])
        finally:
            close()

    def _brief(self, rec: dict) -> None:
        """Optional research brief (Monid) for the prompt. Any failure just means no brief."""
        task = rec["task"]
        if not self.enrich or task.brief:
            return
        try:
            task.brief = redact(str(self.enrich(task) or task.brief or "")).strip()[:4000]
        except Exception:
            task.brief = ""
        if task.brief:
            self.bus.emit("agent_progress", agent=rec["slot"], task_id=task.id, lane=rec["lane"].name,
                          tokens=rec["tokens"], usd=round(rec["usd"], 6), note="research brief ready")

    def _limited(self, lane: Lane, res: LaneResult) -> None:
        """Lane off until its reset: as reported, else its subscription's reset, else the deadline."""
        now = time.time()
        until = _num(res.limited_until) or self.stop_at.get(lane.name) or self.deadline or now + 5 * 3600
        with self.lock:
            fresh = not lane.is_limited(now)
            lane.limited_until = max(lane.limited_until, float(until),       # a stale until never means "go on"
                                     now + float(self.cfg.get("min_limit_seconds", 60)))
        if fresh:
            self.bus.emit("lane_limited", lane=lane.name, until=lane.limited_until,
                          reason=res.summary or "usage limit")

    def _finalize(self, rec: dict, res: LaneResult, path: str) -> dict:
        task = rec["task"]
        test_cmd = task.project.test_cmd if res.status == "done" else None
        fin = worktree.finalize(path, _message(task, rec["lane"].name, res.status, res.summary), test_cmd,
                                float(self.cfg.get("test_timeout", 600)))
        out = {k: fin.get(k) for k in FIN_KEYS}
        if fin.get("tests_ok") is False:                      # the agent said done; the tests disagree
            last = (fin.get("tests_tail") or "").strip().splitlines()[-1:] or [""]
            out.update(status="failed", summary=f"tests failed: {last[0]} · {res.summary}"[:500])
        if not self.cfg.get("keep_worktrees"):
            try:
                worktree.remove(task.project.path, path)      # the branch keeps the work
            except Exception:
                pass                                          # a leftover worktree is untidy, not unsafe
        return out

    def _emit_start(self, rec: dict) -> None:
        rec["started"] = True
        t = rec["task"]
        self.bus.emit("agent_start", agent=rec["slot"], task_id=t.id, project=t.project.name, text=t.text,
                      lane=rec["lane"].name, branch=rec["branch"], worktree=rec["worktree"])

    def _emit_end(self, rec: dict, out: dict) -> None:
        t = rec["task"]
        self.bus.emit("agent_end", agent=rec["slot"], task_id=t.id, project=t.project.name, lane=rec["lane"].name,
                      status=out["status"], summary=out["summary"], tokens=rec["tokens"], usd=round(rec["usd"], 6),
                      commit=out["commit"], diffstat=out["diffstat"] or "", tests_ok=out["tests_ok"],
                      seconds=round(time.time() - rec["t0"], 1))

    def _result(self, reason: str) -> dict:
        now, burned = time.time(), self._burned()
        tasks = [self.final[t.id] for t in self.queue if t.id in self.final]
        for row in tasks:
            if row.get("worktree") and not Path(row["worktree"]).exists():
                row["worktree"] = ""                          # removed: the branch has the work
        n = Counter(r["status"] for r in tasks)
        lanes = [{"name": l.name, "kind": l.kind, "subscription": l.subscription, "as": l.flavor, "available": True,
                  "why": "", "max_agents": l.max_agents, "agents": self.cap[l.name],
                  "resets_at": self.stop_at.get(l.name), "past_reset": self._past(l, now),
                  "limited_until": l.limited_until if l.is_limited(now) else None, **self.by_lane[l.name]}
                 for l in self.lanes] + [dict(s, tasks=0, tokens=0, usd=0.0) for s in self.skipped]
        return {"run": self.run_id, "reason": reason, "started": self.t0, "ended": now,
                "seconds": round(now - self.t0, 1), "deadline": self.deadline, "max_agents": self.max_agents,
                "data_dir": self.data_dir, "report": self.report, **{s: n[s] for s in STATUSES},
                "tokens": sum(b["tokens"] for b in self.by_lane.values()),
                "usd": round(sum(b["usd"] for b in self.by_lane.values()), 6), "by_lane": self.by_lane,
                "lanes": lanes, "tasks": tasks,
                "queued": [{"task_id": t.id, "project": t.project.name, "text": t.text, "source": t.source}
                           for t in self.pending],
                "usages": [to_dict(u) for u in self.usages],
                "usages_now": [to_dict(replace(u, used=u.used + self._spent(u, burned))) for u in self.usages],
                "pace": [_as_dict(p) for p in self.plans]}


def run_swarm(cfg: dict, queue: list[SwarmTask], lanes: list[Lane], bus: Bus, data_dir: str, max_agents: int = 8,
              deadline: float | None = None, usages: list[Usage] | None = None, enrich=None) -> dict:
    """Run the queue on the lanes, at most `max_agents` agents at once (slots 1..N). Each lane is capped by its own
    max_agents and, when `usages` are given, by relay.pace, re-planned every `repace_seconds` from the observed
    burn and whenever a lane's limit lifts. Lanes that aren't available() or are limited are skipped. Plans reset
    on different clocks, so each lane stops launching at its own subscription's resets_at, and the swarm stops at
    `deadline`, else at the latest of those resets. Writes BURN.md and returns the result.
    Optional cfg keys: tick, task_timeout, test_timeout, repace_seconds, grace_seconds, min_limit_seconds,
    keep_worktrees, report."""
    return _Swarm(cfg, queue, lanes, bus, data_dir, max_agents, deadline, usages, enrich).run()


# ── burn week ─────────────────────────────────────────────────────────────────────────────────────────────────

def _inside(path: str, root: str) -> bool:
    p, r = Path(_key(path)), Path(_key(root))
    return p == r or r in p.parents


def _week_end(cfg: dict, usage) -> float | None:
    week = cfg.get("week") or {}
    try:
        return float(usage.week_bounds(week.get("reset", "mon 09:00"), week.get("timezone"))[1]) if usage else None
    except Exception:
        return None


def burn_week(cfg: dict, *, roots=None, plan_path=None, max_agents=None, hours=None, ideas=None, dry_run=False,
              data_dir=None, bus=None, lanes=None) -> dict:
    """discover -> ideas (opt-in) -> usage -> pace -> queue -> lanes -> run_swarm -> BURN.md.
    `ideas`: None follows cfg["ideas"]["enabled"], a bool overrides it, and a dict is used as already-mined ideas.
    The swarm stops after `hours`, else at the latest reset among the lanes being burned (each lane stops at its
    own), else at the configured weekly reset. dry_run returns the plan and the queue without creating worktrees
    or running lanes."""
    cfg = cfg or {}
    data_dir = _key(data_dir or cfg.get("data_dir") or "~/.relay-burn")
    max_agents = int(max_agents or cfg.get("max_agents") or 12)
    roots = [str(Path(r).expanduser()) for r in (roots or cfg.get("roots") or ["."])]
    projects = _need("discover").discover_projects(roots, max_depth=int(cfg.get("max_depth", 3)),
                                                   limit=int(cfg.get("max_projects", 12)),
                                                   include=cfg.get("include"), exclude=cfg.get("exclude"),
                                                   skip=[*(cfg.get("skip") or []), data_dir])
    projects = [p for p in projects if not _inside(p.path, data_dir)]      # never our own worktrees
    mined = ideas if isinstance(ideas, dict) else {}
    on = (cfg.get("ideas") or {}).get("enabled", False) if ideas is None else bool(ideas)
    mod = _mod("ideas") if on and not mined else None
    if mod:
        mined = mod.mine({**cfg, "ideas": {**(cfg.get("ideas") or {}), "enabled": True}}) or {}
    if bus is None and not dry_run:
        bus = Bus(log_dir=cfg.get("log_dir", ".relay"))
    usage = _mod("usage")
    usages = usage.weekly_usage(cfg, ledger=str(bus.tel.path) if bus else ".relay/events.jsonl") if usage else []
    queue = build_queue(projects, mined, plan_path, per_project=int(cfg.get("per_project", 3)),
                        limit=cfg.get("max_tasks"))
    lanes = list(lanes) if lanes is not None else _need("lanes").build_lanes(cfg)
    deadline = time.time() + float(hours) * 3600 if hours else None
    if deadline is None and not any(_usage_for(l, usages) for l in lanes):
        deadline = _week_end(cfg, usage)                   # no lane has a known subscription: the configured week
    seen = {"projects": [{"name": p.name, "path": p.path, "test_cmd": p.test_cmd, "todos": len(p.todos or [])}
                         for p in projects], "ideas": sum(len(v) for v in mined.values())}
    if dry_run:
        pace = _mod("pace") if usages else None
        plans = pace.plan_pace(usages, lanes, max_agents=max_agents,
                               target_pct=float((cfg.get("week") or {}).get("target_pct", 0.97))) if pace else []
        return {"dry_run": True, "data_dir": data_dir, "deadline": _stop(lanes, usages, deadline),
                "max_agents": max_agents, **seen,
                "usages": [to_dict(u) for u in usages], "pace": [_as_dict(p) for p in plans],
                "schedule": pace.schedule_note(plans) if pace and plans else "",
                "lanes": [{"name": l.name, "kind": l.kind, "subscription": l.subscription, "as": l.flavor,
                           "max_agents": l.max_agents, "available": ok, "why": why}
                          for l in lanes for ok, why in [_available(l)]],
                "queue": [{"task_id": t.id, "project": t.project.name, "text": t.text, "source": t.source,
                           "priority": t.priority} for t in queue]}
    monid = _mod("monid")
    try:
        enrich = monid.enricher(cfg) if monid else None
    except Exception:
        enrich = None
    result = run_swarm(cfg, queue, lanes, bus, data_dir, max_agents, deadline, usages, enrich)
    result.update(seen)
    try:
        write_report(result, result["report"])
    except OSError as e:
        result["report_error"] = redact(str(e))
    return result


# ── BURN.md ───────────────────────────────────────────────────────────────────────────────────────────────────

_PUSHY = re.compile(r"\b(git(\s+-[cC]\s+\S+)*\s+push|gh\s+pr\s+create|gh\s+repo\s+sync)\b[^\n]*", re.I)
ICON = {"done": "✓", "blocked": "!", "failed": "✗", "limited": "⏸"}


def _tok(n) -> str:
    n = _num(n, int)
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return str(n)


def _dur(s) -> str:
    s = int(_num(s))
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def _cell(x) -> str:
    return str("" if x is None else x).replace("|", "\\|").replace("\n", " ")


def _when(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%a %b %d %H:%M") if ts else "?"
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def _usage_line(u: dict, after: dict) -> str:
    """`- claude-max: 62% → 71% used of 400.0M tokens · resets Mon Oct 12 09:00` (before → after this run)."""
    def pct(x: dict) -> str:
        return "?" if x.get("pct") is None else f"{x['pct']:.0%}"
    unit, limit = u.get("unit", ""), u.get("limit")
    size = "" if limit is None else (f" of {_tok(limit)} tokens" if unit == "tokens" else
                                     f" of ${_num(limit):,.2f}" if unit == "usd" else f" of {limit:g} {unit}")
    return f"- {u.get('name')}: {pct(u)} → {pct(after)} used{size} · resets {_when(u.get('resets_at'))}"


def _render(r: dict) -> str:
    tasks, queued = r.get("tasks") or [], r.get("queued") or []
    lines = [f"# Burn week · {_when(r.get('started'))}", "",
             f"**{r.get('done', 0)} done · {r.get('blocked', 0)} blocked · {r.get('failed', 0)} failed · "
             f"{r.get('limited', 0)} limited · {len(queued)} still queued** · {_tok(r.get('tokens'))} tokens · "
             f"${_num(r.get('usd')):,.2f} · {_dur(r.get('seconds'))} · stopped: {r.get('reason', '?')}", ""]
    if r.get("projects") is not None:
        lines += [f"{len(r['projects'])} repos · {r.get('ideas', 0)} ideas from local chat history · "
                  f"up to {r.get('max_agents', '?')} agents at once", ""]
    lines += ["## Lanes", "", "| lane | runs as | subscription | tasks | tokens | usd | state |",
              "|---|---|---|---:|---:|---:|---|"]
    for l in r.get("lanes") or []:
        state = (f"skipped: {l.get('why')}" if l.get("available") is False else
                 f"limited until {_when(l['limited_until'])}" if l.get("limited_until") else
                 f"stopped at its reset ({_when(l.get('resets_at'))})" if l.get("past_reset") else
                 f"ok · resets {_when(l['resets_at'])}" if l.get("resets_at") else "ok")
        lines.append("| " + " | ".join(_cell(x) for x in (l.get("name"), l.get("as"), l.get("subscription"),
                                                          l.get("tasks", 0), _tok(l.get("tokens")),
                                                          f"${_num(l.get('usd')):,.2f}", state)) + " |")
    if r.get("usages"):
        now = {u["name"]: u for u in r.get("usages_now") or []}
        lines += ["", "## Capacity", ""] + [_usage_line(u, now.get(u["name"], u)) for u in r["usages"]]
    lines += ["", "## Tasks", ""]
    for t in tasks:
        tests = {True: "passed", False: "FAILING", None: "not run"}.get(t.get("tests_ok"), "?")
        where = f"branch `{t.get('branch')}`" if t.get("branch") else "no branch"
        what = (f"commit `{t['commit']}` · {t.get('diffstat') or 'no diff'}" if t.get("commit")
                else "no changes committed")
        lines += [f"### {ICON.get(t.get('status'), '?')} {t.get('status')} · {t.get('project')} — {t.get('text')}",
                  f"- {where} · {what}", f"- tests: {tests}",
                  f"- lane `{t.get('lane')}` · {_tok(t.get('tokens'))} tokens · ${_num(t.get('usd')):,.2f} · "
                  f"{_dur(t.get('seconds'))} · from {t.get('source')}"
                  + (f" · {t['attempts']} attempts" if _num(t.get("attempts"), int) > 1 else "")]
        if t.get("summary"):
            lines.append(f"- {' '.join(str(t['summary']).split())}")
        if t.get("tests_ok") is False and t.get("tests_tail"):
            tail = str(t["tests_tail"]).replace("```", "'''").strip().splitlines()[-8:]
            lines += ["", "  ```", *[f"  {x}" for x in tail], "  ```"]
        lines.append("")
    need = [t for t in tasks if t.get("status") in ("blocked", "failed", "limited")]
    if need:
        lines += ["## Needs you", ""] + [
            f"- **{t.get('project')}** — {t.get('text')}: {' '.join(str(t.get('summary') or t.get('status')).split())}"
            + (f" (`{t['branch']}`)" if t.get("commit") else "") for t in need] + [""]
    if queued:
        lines += ["## Still queued", ""] + [f"- {q['project']} — {q['text']} ({q['source']})" for q in queued] + [""]
    lines += ["## Review and merge (local only)", "",
              "Every task is at most one commit on its own local branch in its repo. Your checkouts were not touched;",
              "nothing is merged until you merge it.", ""]
    by_repo: dict[str, list] = defaultdict(list)
    for t in tasks:
        if t.get("branch"):
            by_repo[t.get("repo") or t.get("project")].append(t)
    for repo, ts in by_repo.items():
        cmds = [(f"cd {shlex.quote(str(repo))}", "")]
        for t in ts:
            b = t["branch"]
            if t.get("commit"):
                cmds += [(f"git log -p HEAD..{b}", f"{t.get('status')}: {str(t.get('text'))[:60]}"),
                         (f"git merge {b}", "take it"), (f"git branch -D {b}", "or drop it")]
            else:
                cmds += [(f"git branch -D {b}", f"no changes ({t.get('status')}): nothing to review")]
        w = max(len(c) for c, _ in cmds) + 2
        lines += [f"**{ts[0].get('project')}**", "", "```sh"] + [(c.ljust(w) + f"# {n}").rstrip() if n else c
                                                                  for c, n in cmds] + ["```", ""]
    if not by_repo:
        lines += ["No branches this run.", ""]
    lines += ["List every burn branch in a repo: `git -C <repo> branch --list 'relay/burn/*'`", ""]
    return _PUSHY.sub("[remote step removed: branches stay local]", "\n".join(lines))


def write_report(result: dict, path: str) -> str:
    """Write BURN.md: totals, a lane table, one entry per task (branch, commit, diffstat, tests), what needs a
    human, and how to review and merge locally. Everything goes through redact(), and the report never tells
    anyone to publish a branch anywhere. Returns the path."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(redact(_render(result)))
    return str(p)
