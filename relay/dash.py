"""Terminal dashboard for burn-week: fold bus / JSONL rows into a DashState, draw it as one frame.

    attach(bus)                   live view in the alternate screen while the swarm runs (TTY only)
    tail(".relay/events.jsonl")   follow a ledger like `tail -f`; once=True prints a single frame
    render(state, 120, 34)        pure: same state and size -> same text
"""
from __future__ import annotations

import atexit
import json
import re
import shutil
import signal
import sys
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from . import ui
from .contracts import redact

if TYPE_CHECKING:  # pragma: no cover
    from .bus import Bus

# 256-color palette. Lane flavor -> (text color, badge background).
FLAVORS = {"claude": (209, 173), "codex": (114, 71), "agent37": (141, 98), "relay": (75, 33),
           "orca": (80, 37), "monid": (221, 178)}
ALIASES = (("claude", "claude"), ("codex", "codex"), ("chatgpt", "codex"), ("agent37", "agent37"),
           ("openai", "relay"), ("relay", "relay"), ("orca", "orca"), ("monid", "monid"))
GREY = (250, 242)
FIRE = (124, 160, 196, 202, 208, 214, 220, 226)
FRAME, DIM, TEXT, BRIGHT, TRACK = "38;5;239", "38;5;244", "38;5;252", "1;38;5;231", 236
GOOD, WARN, BAD, LIMIT = "38;5;114", "38;5;221", "38;5;203", "38;5;213"
STATUS = {"done": ("✓", GOOD), "blocked": ("◆", WARN), "failed": ("✗", BAD), "limited": ("⊘", LIMIT)}
SPIN, EIGHTHS, SPARK = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏", " ▏▎▍▌▋▊▉█", " ▁▂▃▄▅▆▇█"
FLICKER = ((5, 8, 6), (6, 7, 8), (7, 8, 5), (8, 6, 7), (6, 8, 7), (7, 5, 8))   # header flame, 4 frames a second
STEPS = (1, 1.5, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 30, 45, 60)   # seconds per chart column: whole run while it fits
LIVE_GAP = 300                              # tail ticks the clock while the ledger moved this recently
UNIT = {"tokens": 0, "usd": 1}                # burn[lane] column; samples keep the same pair at 3 + column
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


# --------------------------------------------------------------------------- state
@dataclass
class Slot:
    """One agent slot: the task it runs now, or ran last."""
    agent: int
    task_id: str = ""
    project: str = ""
    text: str = ""
    lane: str = ""
    branch: str = ""
    note: str = ""
    status: str = "idle"                    # running | done | blocked | failed | limited | idle
    tokens: int = 0
    usd: float = 0.0
    started: float = 0.0
    last: float = 0.0                       # ts of the last report: the next delta spreads back to here
    seconds: float | None = None
    samples: deque = field(default_factory=lambda: deque(maxlen=48))


@dataclass
class Meter:
    """One subscription: the latest usage snapshot plus what this swarm burned since."""
    name: str
    unit: str = "tokens"
    used: float = 0.0                       # latest snapshot (swarm_start usage or pace)
    limit: float | None = None
    resets_at: float = 0.0
    lane: str = ""                          # Usage.lane: fallback for the lane -> subscription map
    base: float = 0.0                       # usage before this swarm burned anything
    snap: float = 0.0                       # swarm burn already included in `used`
    agents: dict = field(default_factory=dict)   # lane -> recommended agents (pace)
    target_rate: float = 0.0                # units/hour to land at target_pct by reset (pace)


@dataclass
class DashState:
    """Everything the dashboard draws, folded from bus rows by apply(); render() reads nothing else."""
    run: str = ""
    hint: str = "waiting for the swarm to start"
    started: float = 0.0
    now: float = 0.0                        # clock: the latest row ts, or tick() while live
    last_ts: float = 0.0
    resets_at: float = 0.0
    max_agents: int = 0
    queue: int = 0
    queued: int = 0
    launched: int = 0
    pending: dict = field(default_factory=dict)    # task_id -> (project, text) queued, not started yet
    target_pct: float = 0.97
    lanes: dict = field(default_factory=dict)      # lane name -> {kind, subscription, flavor}
    meters: dict = field(default_factory=dict)     # subscription name -> Meter, in display order
    slots: dict = field(default_factory=dict)      # slot number -> Slot
    branches: dict = field(default_factory=dict)   # task_id -> branch
    finished: set = field(default_factory=set)     # task ids that ended: later rows for them are stale
    limited: dict = field(default_factory=dict)    # lane -> (until, reason)
    burn: dict = field(default_factory=dict)       # lane -> [tokens, usd] burned by this swarm
    counts: dict = field(default_factory=lambda: dict.fromkeys(STATUS, 0))
    tokens: int = 0
    usd: float = 0.0
    landed: deque = field(default_factory=lambda: deque(maxlen=32))
    samples: deque = field(default_factory=lambda: deque(maxlen=4096))   # (t0, t1, lane, tokens, usd)
    end: dict | None = None
    rows: int = 0

    def apply(self, row: dict) -> None:
        """Fold one bus / JSONL row in. Unknown events and malformed fields are ignored."""
        if not isinstance(row, dict):
            return
        ev, ts = row.get("event"), _f(row.get("ts"))
        if ev == "swarm_start":
            self.__dict__.update({**DashState().__dict__, "hint": self.hint})   # a new run replaces the last
        self.rows += 1
        self.now, self.last_ts = max(self.now, ts), max(self.last_ts, ts)
        fn = getattr(self, f"_on_{ev}", None) if isinstance(ev, str) else None
        if fn:
            try:
                fn(row, ts or self.now)
            except (TypeError, ValueError, AttributeError, KeyError):
                pass                        # one malformed row must not take the dashboard down

    def tick(self, now: float) -> None:
        """Advance the clock without an event (spinners, elapsed times) while the run is live."""
        self.now = max(self.now, float(now))

    # ---- event handlers
    def _on_swarm_start(self, r: dict, ts: float) -> None:
        self.run, self.started = _clean(r.get("run"), 40), ts
        self.max_agents, self.queue = _i(r.get("max_agents")), _i(r.get("queue"))
        self.resets_at, self.target_pct = _f(r.get("resets_at")), _f(r.get("target_pct"), 0.97) or 0.97
        for ln in r.get("lanes") or []:
            if isinstance(ln, dict) and ln.get("name"):
                self.lanes[_name(ln["name"])] = {"kind": _name(ln.get("kind")), "subscription": _name(ln.get("subscription")),
                                                 "flavor": _name(ln.get("as") or ln.get("kind") or ln["name"])}
        for u in r.get("usages") or []:
            if isinstance(u, dict) and u.get("name"):
                m = self._meter(_name(u["name"]))
                m.unit, m.used, m.limit = _name(u.get("unit")) or "tokens", _f(u.get("used")), _f(u.get("limit")) or None
                m.base, m.lane = m.used, _name(u.get("lane"))
                m.resets_at = _f(u.get("resets_at")) or (ts + _f(u.get("hours_left")) * 3600 if _f(u.get("hours_left")) else 0.0)
        self.resets_at = self.resets_at or min((m.resets_at for m in self.meters.values() if m.resets_at), default=0.0)
        self.slots = {k: Slot(k) for k in range(1, min(self.max_agents, 64) + 1)}

    def _on_pace(self, r: dict, ts: float) -> None:
        lane = _name(r.get("lane"))
        sub = _name(r.get("subscription")) or self.sub_of(lane)
        if not sub:
            return
        if lane:
            info = self.lanes.setdefault(lane, {"kind": "", "subscription": "", "flavor": lane})
            info["subscription"] = info["subscription"] or sub
        new, m = sub not in self.meters, self._meter(sub)
        m.unit = _name(r.get("unit")) or m.unit
        if r.get("used") is not None:
            b = self.burned(m)
            m.used, m.snap = _f(r.get("used")), b
            m.base = m.used - b if new else m.base
        if r.get("limit") is not None:
            m.limit = _f(r.get("limit")) or None
        if lane:
            m.agents[lane] = _i(r.get("agents"))
        m.target_rate = _f(r.get("target_rate"), m.target_rate)
        if not m.resets_at and _f(r.get("hours_left")):
            m.resets_at = ts + _f(r.get("hours_left")) * 3600
        self.resets_at = self.resets_at or m.resets_at
        self.target_pct = _f(r.get("target_pct"), self.target_pct) or self.target_pct

    def _on_task_queued(self, r: dict, ts: float) -> None:
        self.queued += 1
        if len(self.pending) < 500:
            self.pending[_clean(r.get("task_id"), 80) or str(self.queued)] = (_clean(r.get("project"), 60), _clean(r.get("text"), 200))

    def _on_agent_start(self, r: dict, ts: float) -> None:
        k, tid = _i(r.get("agent")), _clean(r.get("task_id"), 80)
        s = self.slots[k] = Slot(k, tid, _clean(r.get("project"), 60), _clean(r.get("text"), 300), _name(r.get("lane")),
                                 _clean(r.get("branch"), 120) or _branch(tid), status="running", started=ts, last=ts)
        self.branches[tid] = s.branch
        self.pending.pop(tid, None)
        self.finished.discard(tid)          # a retry of a task that ended (say, limited) counts again
        self.launched += 1

    def _on_agent_progress(self, r: dict, ts: float) -> None:
        s = self._slot(r, ts)
        if s:
            self._spend(s, r, ts)
            s.note = _clean(r.get("note"), 120) or s.note

    def _on_agent_end(self, r: dict, ts: float) -> None:
        s = self._slot(r, ts)
        if not s or s.status != "running":
            return                          # a duplicate end row
        self.finished.add(s.task_id)
        self._spend(s, r, ts)
        st = str(r.get("status") or "done")
        s.status = st if st in STATUS else "failed"
        s.seconds = _f(r.get("seconds"), ts - s.started)
        self.counts[s.status] += 1
        tests = r.get("tests_ok")
        self.landed.append({"status": s.status, "branch": s.branch, "project": s.project, "lane": s.lane,
                            "summary": re.sub(r"^blocked\s*:\s*", "", _clean(r.get("summary"), 200), flags=re.I),
                            "commit": _clean(r.get("commit"), 12),
                            "diff": _diffstat(r), "tests_ok": tests if isinstance(tests, bool) else None})

    def _on_lane_limited(self, r: dict, ts: float) -> None:
        if r.get("lane"):
            self.limited[_name(r["lane"])] = (_f(r.get("until")), _clean(r.get("reason"), 120))

    def _on_swarm_end(self, r: dict, ts: float) -> None:
        self.run = self.run or _clean(r.get("run"), 40)
        self.end = {"reason": _clean(r.get("reason"), 80), "report": _clean(r.get("report"), 160),
                    "seconds": _f(r.get("seconds"), ts - (self.started or ts))}
        for k in self.counts:
            self.counts[k] = _i(r.get(k), self.counts[k])
        self.tokens = _i(r.get("tokens"), self.tokens)            # swarm_end's totals are authoritative (BURN.md, web)
        self.usd = _f(r.get("usd"), self.usd)

    # ---- helpers
    def _meter(self, name: str) -> Meter:
        return self.meters.setdefault(name, Meter(name))

    def _slot(self, r: dict, ts: float) -> Slot | None:
        """The slot a progress/end row belongs to (made up when we missed its agent_start); None for an ended task."""
        k, tid = _i(r.get("agent")), _clean(r.get("task_id"), 80)
        if tid in self.finished:
            return None                     # agent_end already carried this task's final cumulative tokens
        s = self.slots.get(k)
        if s is None or (tid and s.task_id != tid):
            s = self.slots[k] = Slot(k, tid, _clean(r.get("project"), 60), "", _name(r.get("lane")),
                                     self.branches.get(tid) or _branch(tid), status="running", started=ts, last=ts)
        return s

    def _spend(self, s: Slot, r: dict, ts: float) -> None:
        """Cumulative task tokens -> a delta, spread over the time since the agent's previous report."""
        s.lane = s.lane or _name(r.get("lane"))
        tok, usd = max(s.tokens, _i(r.get("tokens"))), max(s.usd, _f(r.get("usd")))
        if tok > s.tokens or usd > s.usd:
            sample = (s.last if 0 < s.last < ts else ts, ts, s.lane, tok - s.tokens, usd - s.usd)
            self.samples.append(sample)
            s.samples.append(sample)
            b = self.burn.setdefault(s.lane, [0, 0.0])
            b[0] += sample[3]
            b[1] += sample[4]
            self.tokens += sample[3]
            self.usd += sample[4]
        s.tokens, s.usd, s.last = tok, usd, max(s.last, ts)

    def sub_of(self, lane: str) -> str | None:
        info = self.lanes.get(lane) or {}
        if info.get("subscription") in self.meters:
            return info["subscription"]
        keys = {lane, info.get("kind"), info.get("flavor")} - {"", None}
        return next((m.name for m in self.meters.values() if m.lane in keys), None)

    def flavor(self, lane: str) -> str:
        info = self.lanes.get(lane) or {}
        f = info.get("flavor") or info.get("kind") or lane
        return f if f in FLAVORS else _guess(f"{lane} {f}")

    def meter_flavor(self, m: Meter) -> str:
        lane = next((n for n, i in self.lanes.items() if i.get("subscription") == m.name), None)
        return self.flavor(lane) if lane else m.lane if m.lane in FLAVORS else _guess(f"{m.name} {m.lane}")

    def burned(self, m: Meter) -> float:
        i = UNIT.get(m.unit)
        return 0.0 if i is None else sum(v[i] for lane, v in self.burn.items() if self.sub_of(lane) == m.name)

    def used(self, m: Meter) -> float:
        b = self.burned(m)
        return max(m.used + b - m.snap, m.base + b)     # right whether or not pace rows count our own burn

    @property
    def waiting(self) -> int:
        """Tasks still queued: swarm_start's queue size (or task_queued rows) minus launches."""
        return max(0, (self.queue or self.queued) - self.launched)

    def snapshot(self) -> dict:
        """JSON-friendly state without the sample logs (relay.web serves this as /api/state, keyed like its own fold)."""
        nxt, slots = self.next_reset(), [s for s in self.slots.values() if s.task_id]
        lanes = {n: {**i, "tokens": self.burn.get(n, [0, 0.0])[0], "usd": self.burn.get(n, [0, 0.0])[1]} for n, i in self.lanes.items()}
        meters = {m.name: {"name": m.name, "unit": m.unit, "used": self.used(m), "limit": m.limit, "resets_at": m.resets_at,
                           "pct": min(1.0, self.used(m) / m.limit) if m.limit else None, "burned": self.burned(m),
                           "agents": sum(m.agents.values()), "target_rate": m.target_rate} for m in self.meters.values()}
        agents = {str(s.agent): {"agent": s.agent, "task_id": s.task_id, "project": s.project, "text": s.text, "lane": s.lane,
                                 "branch": s.branch, "status": s.status, "tokens": s.tokens, "usd": s.usd, "note": s.note,
                                 "since": s.started, "seconds": s.seconds} for s in slots}
        running = sum(s.status == "running" for s in slots)
        return {"run": self.run, "started": self.started, "now": self.now, "ended": self.now if self.end else None,
                "resets_at": self.resets_at, "max_agents": self.max_agents, "queued": self.waiting, "rows": self.rows,
                "target_pct": self.target_pct, "lanes": lanes, "meters": meters, "agents": agents, "landed": list(self.landed)[::-1],
                "limited": {k: {"until": u, "reason": why} for k, (u, why) in self.limited.items()}, "end": self.end,
                "totals": {"tokens": self.tokens, "usd": self.usd, "running": running, "queued": self.waiting, **self.counts},
                "next_reset": {"plan": nxt[0], "at": nxt[1]} if nxt else None}

    def next_reset(self) -> tuple[str, float] | None:
        """(plan, resets_at) of the soonest future reset among the plans being burned (all plans if none mapped)."""
        burning = {self.sub_of(n) for n in set(self.lanes) | set(self.burn)} - {None}
        ms = [m for m in self.meters.values() if m.resets_at > self.now and (m.name in burning or not burning)]
        m = min(ms, key=lambda m: m.resets_at, default=None)
        return (m.name, m.resets_at) if m else ("", self.resets_at) if self.resets_at > self.now else None

    def lane_limit(self, lane: str) -> tuple[float, str] | None:
        lim = self.limited.get(lane)
        return lim if lim and (lim[0] > self.now or not lim[0]) else None

    def rate(self, m: Meter, window: float = 60.0) -> float:
        """Units per hour this swarm burned on `m` over the last `window` seconds."""
        i = UNIT.get(m.unit)
        lanes = {n for n in self.burn if self.sub_of(n) == m.name}
        if i is None or not lanes:
            return 0.0
        end = self.now
        start, tot = max(end - window, self.started or end - window), 0.0
        for s in reversed(self.samples):               # appended in time order: stop well before the window
            if s[1] < start - window:
                break
            if s[2] in lanes:
                tot += s[3 + i] * _overlap(s[0], s[1], start, end)
        return tot / max(end - start, 1.0) * 3600


# --------------------------------------------------------------------------- small pure helpers
def _f(v, d: float = 0.0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return d
    return x if x == x and abs(x) != float("inf") else d


def _i(v, d: int = 0) -> int:
    return int(_f(v, d))


def _name(v) -> str:
    return _clean(v, 40)


def _branch(tid: str) -> str:
    return f"relay/burn/{tid}" if tid else ""


def _guess(text: str) -> str:
    text = text.lower()
    return next((f for key, f in ALIASES if key in text), "")


def _clean(v, n: int = 200) -> str:
    """Untrusted text -> one redacted printable line: no escapes, controls or zero-width marks."""
    s = unicodedata.normalize("NFC", redact(str(v or "")))
    s = "".join(" " if unicodedata.category(ch) in ("Cc", "Zl", "Zp") else ch for ch in s
                if unicodedata.category(ch) not in ("Mn", "Me", "Cf", "Cs", "Cn"))
    return " ".join(s.split())[:n]


def _diffstat(r: dict) -> tuple[int | None, int, int]:
    """agent_end -> (files, insertions, deletions) from its fields or `git diff --shortstat` / '+N -N' text."""
    s = str(r.get("diffstat") or "")

    def num(key: str, *pats: str) -> int | None:
        if r.get(key) is not None:
            return _i(r[key])
        m = next((m for p in pats if (m := re.search(p, s))), None)
        return int(m[1]) if m else None
    return (num("files", r"(\d+) files? changed"), num("insertions", r"(\d+) insertions?\(\+\)", r"(?<![\w-])\+(\d+)") or 0,
            num("deletions", r"(\d+) deletions?\(-\)", r"(?<![\w+])[-−](\d+)") or 0)


def _overlap(t0: float, t1: float, a: float, b: float) -> float:
    """Fraction of [t0, t1] inside [a, b] (a point sample counts whole when inside)."""
    if t1 <= t0:
        return 1.0 if a <= t1 <= b else 0.0
    return max(0.0, min(t1, b) - max(t0, a)) / (t1 - t0)


def _series(samples, end: float, step: float, n: int, idx: int = 3) -> list[float]:
    """Spread samples over n buckets of `step` seconds ending at `end` -> units per second."""
    start, vals = end - step * n, [0.0] * n
    for s in samples:
        t0, t1, v = s[0], s[1], s[idx]
        if t1 < start or t0 > end or not v:
            continue
        if t1 <= t0:
            vals[min(n - 1, int((t1 - start) // step))] += v
            continue
        a, b, rate = max(t0, start), min(t1, end), v / (t1 - t0)
        i = min(n - 1, int((a - start) // step))
        while a < b and i < n:
            edge = min(b, start + (i + 1) * step)
            vals[i] += rate * (edge - a)
            a, i = edge, i + 1
    return [v / step for v in vals]


def _si(v: float, unit: str = "tokens", trim: bool = False) -> str:
    """1240000 -> '1.24M', 84200 -> '84.2k'; unit 'usd' -> '$3.42'. trim: '3.60M' -> '3.6M' (static figures)."""
    v, s = _f(v), ""
    if unit == "usd":
        s = f"${v:,.2f}" if abs(v) < 1000 else f"${v:,.0f}"
    for suffix, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)) if not s else ():
        if abs(v) >= div * 0.9995:
            x = v / div
            s = f"{x:.{2 if abs(x) < 9.995 else 1 if abs(x) < 99.95 else 0}f}{suffix}"
            break
    s = s or f"{v:.0f}"
    if trim and "." in s:
        num, suffix = re.match(r"(.*\d)(\D*)$", s).groups()
        s = num.rstrip("0").rstrip(".") + suffix
    return s


def _clock(sec: float) -> str:
    s = max(0, int(sec))
    h, s = divmod(s, 3600)
    return f"{h}:{s // 60:02d}:{s % 60:02d}" if h else f"{s // 60}:{s % 60:02d}"


def _countdown(sec: float) -> str:
    s = max(0, int(sec))
    d, s = divmod(s, 86400)
    return (f"{d}d " if d else "") + f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"


def _span(sec: float) -> str:
    s = max(0, int(sec))
    return f"{s // 86400}d" if s >= 100 * 3600 else f"{s // 3600}h" if s >= 3600 else f"{s // 60}m"


def _resets(ts: float, now: float) -> str:
    """'Thu 14:00 (31h)': a plan's next reset and the time left until it ('Nov 01 (24d)' when far off)."""
    return time.strftime("%a %H:%M" if ts - now < 6.5 * 86400 else "%b %d", time.localtime(ts)) + f" ({_span(ts - now)})"


def _hm(ts: float, now: float) -> str:
    return time.strftime("%H:%M" if abs(ts - now) < 86400 else "%a %H:%M", time.localtime(ts))


# --------------------------------------------------------------------------- text layout
def _cw(ch: str) -> int:
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def _w(s: str) -> int:
    return len(s) if s.isascii() else sum(_cw(ch) for ch in s)


def _sw(segs) -> int:
    return sum(_w(t) for t, _ in segs)


def _clip(s: str, n: int) -> str:
    if _w(s) <= n:
        return s
    out, used = [], 0
    for ch in s:
        if used + _cw(ch) > n - 1:
            break
        out.append(ch)
        used += _cw(ch)
    return "".join(out) + "…" if n > 0 else ""


def _fit(segs, n: int) -> list:
    """Clip styled segments [(text, sgr)] to n cells, with an ellipsis where text was cut."""
    out, used = [], 0
    for text, code in segs:
        if used + _w(text) > n:
            if n - used > 0:
                out.append((_clip(text, n - used), code))
            break
        out.append((text, code))
        used += _w(text)
    return out


def _pad(segs, n: int, right: bool = False) -> list:
    segs = _fit(segs, n)
    pad = [(" " * (n - _sw(segs)), "")]
    return pad + segs if right else segs + pad


def _spread(left, right, n: int) -> list:
    """Left segments, then right segments flush right; the right side is dropped when it doesn't fit."""
    gap = n - _sw(left) - _sw(right)
    return list(left) + [(" " * gap, "")] + list(right) if gap >= 1 else list(left)


def _colored() -> bool:
    return ui.c("0", "x") != "x"


def _line(segs, n: int, bg: int | None = None) -> str:
    """Segments -> exactly n cells of text, colored through relay.ui.c (so NO_COLOR and pipes stay plain)."""
    out = []
    for text, code in _pad(segs, n):
        if bg is not None:
            code = f"{code};48;5;{bg}" if code else f"48;5;{bg}"
        out.append(ui.c(code, text) if code and text else text)
    return "".join(out)


def _merge(cells) -> list:
    out: list = []
    for ch, code in cells:
        if out and out[-1][1] == code:
            out[-1] = (out[-1][0] + ch, code)
        else:
            out.append((ch, code))
    return out


def _box(width: int, title, rows, right=()) -> list[str]:
    """Rounded frame: title (and an optional right-hand label) in the top border, rows padded inside."""
    r = [(" ", "")] + list(right) + [(" ─╮", FRAME)] if right else [("─╮", FRAME)]
    t = [("╭─ ", FRAME)] + list(title) + [(" ", "")]
    if _sw(t) + _sw(r) > width:
        r = [("─╮", FRAME)]
    t = _fit(t, width - _sw(r))
    top = t + [("─" * (width - _sw(t) - _sw(r)), FRAME)] + r
    side = [("│", FRAME)]
    body = [_line(side + [(" ", "")] + _pad(row, width - 4) + [(" ", "")] + side, width) for row in rows]
    return [_line(top, width)] + body + [_line([("╰" + "─" * (width - 2) + "╯", FRAME)], width)]


# --------------------------------------------------------------------------- widgets
def _bar(frac: float, n: int, fg: int, target: float | None) -> list:
    """n-cell meter with 1/8-cell resolution and a target marker; plain █ ░ │ without color."""
    color, e = _colored(), round(min(max(frac, 0.0), 1.0) * n * 8)
    full, part = divmod(e, 8)
    mark = min(n - 1, int(target * n)) if target else -1
    cells = []
    for i in range(n):
        if i == mark:
            cells.append(("┃", f"1;38;5;231;48;5;{fg if i < full else TRACK}") if color else ("│", ""))
        elif i < full:
            cells.append(("█", f"38;5;{fg}"))
        elif i == full and part:
            cells.append((EIGHTHS[part], f"38;5;{fg};48;5;{TRACK}" if color else ""))
        else:
            cells.append((" ", f"48;5;{TRACK}") if color else ("░", DIM))
    return _merge(cells)


def _spark(vals, peak: float, code: str) -> list:
    return [("".join(SPARK[max(1, min(8, round(v / peak * 8)))] if v > 0 and peak else " " for v in vals), code)]


def _area(vals, rows: int) -> list[list]:
    """Multi-row area chart, colored by height like a flame (dark red at the bottom, yellow on top)."""
    peak = max(vals, default=0) or 1.0
    out = []
    for r in range(rows - 1, -1, -1):
        cells = []
        for v in vals:
            e = max(1, round(v / peak * rows * 8)) if v > 0 else 0
            k = min(8, max(0, e - r * 8))
            level = (r * 8 + k - 1) * len(FIRE) // (rows * 8) if rows > 1 else k - 1
            cells.append((SPARK[k], f"38;5;{FIRE[max(0, min(len(FIRE) - 1, level))]}" if k else ""))
        out.append(_merge(cells))
    return out


def _badge(st: DashState, lane: str, dim: bool = False) -> list:
    fg, bg = FLAVORS.get(st.flavor(lane), GREY)
    if not _colored():
        return [(lane, "")]
    return [(f" {_clip(lane, 7)} ", f"1;38;5;16;48;5;{240 if dim else bg}")]


def _step(st: DashState, cols: int) -> float:
    span = st.now - (st.started or (st.samples[0][0] if st.samples else st.now))
    return next((s for s in STEPS if s * cols >= span), STEPS[-1])


def _chart_end(st: DashState, step: float) -> float:
    """Right edge of the chart: the last whole step with data (a half-reported bucket would dip)."""
    last = max((s[1] for s in st.samples), default=st.now)
    return last if st.end is not None else max(last // step * step, st.started + step)


# --------------------------------------------------------------------------- sections
def _header(st: DashState, W: int) -> str:
    """Flame + title, LIVE/DONE, run id; on the right the soonest reset among burned plans and the clock."""
    k = int(st.now * 4)
    if st.end is None and st.started:
        flame = [(SPARK[h], f"38;5;{FIRE[h - 1]}") for h in FLICKER[k % len(FLICKER)]]
        live = [("●", "1;38;5;196" if k % 4 < 2 else "38;5;88"), (" LIVE", "1;38;5;196")]
    else:
        flame = [(SPARK[h], f"38;5;{FIRE[h - 1]}") for h in (4, 6, 8)]
        live = [("■ DONE", "1;" + GOOD)] if st.end else [("○ waiting", DIM)]
    title = "relay burn week"
    left = ([(" ", "")] + flame + [("  ", "")] + [(ch, f"1;38;5;{FIRE[7 - i * 4 // len(title)]}") for i, ch in enumerate(title)]
            + [("   ", "")] + live)
    if st.run:
        left += [("   run ", DIM), (st.run, TEXT)]
    right, nxt = [], st.next_reset()
    if nxt:
        right += [(nxt[0], f"1;38;5;{FLAVORS.get(st.meter_flavor(st.meters[nxt[0]]), GREY)[0]}") if nxt[0] else ("", ""),
                  (" resets in " if nxt[0] else "resets in ", DIM), (_countdown(nxt[1] - st.now), BRIGHT)]
    if st.now:
        right += [("   ◷ ", DIM), (time.strftime("%H:%M:%S", time.localtime(st.now)), TEXT)]
    if _sw(left) + _sw(right) + 2 > W and st.run:
        left = left[:-2]
    if _sw(left) + _sw(right) + 2 > W:
        right = right[:3]
    return _line(_spread(left, right + [(" ", "")], W), W)


def _meters(st: DashState, inner: int) -> list:
    """One row per plan: meter with target marker, %, its own reset clock, amount, burned this run, pace."""
    ms = list(st.meters.values())
    if not ms:
        return []
    nw = min(14, max(_w(m.name) for m in ms))
    want = {"pct": 6, "resets": 22, "amt": 11, "burn": 13, "agents": 9, "pace": 15}
    for step in (None, "agents", "medium", "burn", "pace", "amt", "short"):
        if step in ("medium", "short"):
            want["resets"] = 17 if step == "medium" else 6       # 'resets Thu 14:00 (31h)' > '↻ Thu 14:00 (31h)' > '↻ 31h'
        want.pop(step, None)
        bar = inner - nw - sum(want.values()) - 2 * len(want)          # 1 after the name, 1 before the %, 2 elsewhere
        if bar >= max(16, inner // 5):
            break
    rows = []
    for m in ms:
        fg = FLAVORS.get(st.meter_flavor(m), GREY)[0]
        lanes = sorted(n for n in set(st.burn) | set(st.lanes) | set(m.agents) if st.sub_of(n) == m.name)
        lim = next((st.lane_limit(n) for n in lanes if st.lane_limit(n)), None)
        used = st.used(m)
        pct = min(1.0, used / m.limit) if m.limit else None
        cells = {"pct": [(f"{pct * 100:5.1f}%", BRIGHT) if pct is not None else ("    ?%", DIM)]}
        if m.resets_at > st.now:
            label = {22: "resets ", 17: "↻ "}.get(want["resets"], "↻ ")
            when = _resets(m.resets_at, st.now) if want["resets"] > 6 else _span(m.resets_at - st.now)
            cells["resets"] = [(label, DIM), (when, TEXT)]
        elif m.resets_at:
            cells["resets"] = [("resets now", WARN)]
        unit = " cr" if m.unit == "credits" else ""
        cells["amt"] = [(_si(used, m.unit), TEXT), (f"/{_si(m.limit, m.unit, trim=True)}{unit}" if m.limit else f"{unit} used", DIM)]
        b = st.burned(m)
        cells["burn"] = [(f"+{_si(b, m.unit)}", f"1;38;5;{fg}"), (" burned", DIM)] if b else []
        n = sum(m.agents.values())
        cells["agents"] = [(f"{n} agent{'s' if n != 1 else ''}", TEXT)] if m.agents and not lim else []
        if lim:
            cells["pace"] = [("⊘ limited", "1;" + LIMIT)] + ([(" " + _hm(lim[0], st.now), TEXT)] if lim[0] else [])
        elif m.target_rate:
            ok = st.rate(m) >= m.target_rate * 0.95
            cells["pace"] = [("need ", DIM), (f"{_si(m.target_rate, m.unit, trim=True)}/h ", TEXT), ("▲" if ok else "▼", GOOD if ok else WARN)]
        row = [(_clip(m.name, nw).ljust(nw), f"1;38;5;{fg}"), (" ", "")]
        row += _bar(pct or 0.0, bar, 242 if lim else fg, st.target_pct if m.limit else None)
        for k, w in want.items():
            row += [(" " if k == "pct" else "  ", "")] + _pad(cells.get(k, []), w, right=k in ("amt", "burn", "agents"))
        rows.append(row)
    return rows


COLS = (("#", 2, ">"), ("lane", 9, "<"), ("project", 12, "<"), ("task", 0, "<"), ("activity", 8, "<"),
        ("tokens", 7, ">"), ("time", 7, ">"), ("status", 9, "<"))


def _columns(inner: int) -> list[tuple[str, int, str]]:
    cols = [(n, 16 if n == "project" and inner > 136 else w, a) for n, w, a in COLS]
    for drop in (None, "activity", "time", "project", "tokens"):
        cols = [c for c in cols if c[0] != drop]
        flex = inner - sum(w for _, w, _ in cols) - (len(cols) - 1)
        if flex >= 18:
            break
    return [(n, w or max(1, flex), a) for n, w, a in cols]


def _agents(st: DashState, inner: int, step: float, end: float) -> tuple[list, list]:
    cols = _columns(inner)
    names = {n for n, _, _ in cols}
    head = _cells(cols, {n: [(n, DIM)] for n in names})
    series = {k: _series(s.samples, end, step, 8) for k, s in st.slots.items()} if "activity" in names else {}
    peak = max((max(v) for v in series.values()), default=0.0)
    waiting, rows = st.waiting, []
    for k in sorted(st.slots):
        s = st.slots[k]
        if not s.task_id and s.status == "idle":
            rows.append(_cells(cols, {"#": [(str(k), DIM)], "lane": [("·", DIM)],
                                      "task": [("waiting for a task" if waiting else "idle", DIM)]}))
            continue
        fg = FLAVORS.get(st.flavor(s.lane), GREY)[0]
        live = s.status == "running" and st.end is None
        task_w = next(w for n, w, _ in cols if n == "task")
        text = [(s.text or s.task_id, TEXT if live else DIM)]
        if live and s.note and task_w >= 48:
            nw = min(_w(s.note) + 3, task_w // 3)
            text = _fit(text, task_w - nw) + [(" · ", FRAME), (_clip(s.note, nw - 3), DIM)]
        glyph, code = STATUS.get(s.status, ("■", DIM))
        status = ([(SPIN[(int(st.now * 8) + k) % len(SPIN)], f"1;38;5;{fg}"), (" working", TEXT)] if live
                  else [(glyph, "1;" + code), (" " + (s.status if s.status in STATUS else "stopped"), code)])
        took = s.seconds if s.seconds is not None else st.now - s.started
        rows.append(_cells(cols, {"#": [(str(k), DIM)], "lane": _badge(st, s.lane, dim=not live), "project": [(s.project, TEXT)],
                                  "task": text, "activity": _spark(series.get(k, []), peak, f"38;5;{fg}" if live else DIM),
                                  "tokens": [(_si(s.tokens), BRIGHT if live else TEXT)], "time": [(_clock(took), TEXT if live else DIM)],
                                  "status": status}))
    return head, rows


def _cells(cols, cells: dict) -> list:
    out: list = []
    for i, (name, w, align) in enumerate(cols):
        out += _pad(cells.get(name, []), w, right=align == ">") + ([(" ", "")] if i < len(cols) - 1 else [])
    return out


def _landed(st: DashState, inner: int, n: int) -> list:
    """Newest completions first (branch, diffstat, tests), then what is up next when there is room."""
    rows = []
    for e in list(st.landed)[::-1][:n]:
        glyph, code = STATUS.get(e["status"], ("·", DIM))
        prefix, _, name = e["branch"].rpartition("/")
        left = [(glyph, "1;" + code), (" ", "")] + ([(prefix + "/", DIM)] if prefix and inner >= 56 else []) + [(name or e["project"], TEXT)]
        files, ins, dels = e["diff"]
        t = e["tests_ok"]
        tests = [("tests ", DIM), ("✓", "1;" + GOOD)] if t else [("tests ", DIM), ("✗", "1;" + BAD)] if t is False else [("no tests", DIM)]
        if e["status"] == "done" or t is False or not e["summary"]:
            right = ([(f"{files}f ", DIM)] if files is not None and inner >= 48 else []) + [
                (f"+{ins}", GOOD if ins else DIM), (" ", ""), (f"−{dels}", BAD if dels else DIM), ("  ", "")] + tests
            row = _spread(left, right, inner)
            row = row if len(row) > len(left) else _spread(left, tests, inner)
        else:
            row = left + [("  ", ""), (e["summary"], code)]
        rows.append(row)
    if not rows:
        rows.append([("◌ ", DIM), ("nothing landed yet", DIM)])
    if len(rows) + 2 <= n and st.pending:
        rows += [[], [("up next", DIM)]]
        rows += [[("◌ ", DIM), (p, TEXT), ("  ", ""), (t, DIM)] for p, t in list(st.pending.values())[:n - len(rows)]]
    return rows[:n]


def _footer(st: DashState, W: int) -> str:
    """Totals bar: tokens, $, done / blocked / failed / limited, queue, and elapsed time or how the run ended."""
    c, waiting = st.counts, st.waiting

    def left(wide: bool) -> list:
        segs = [(" Σ ", "1;38;5;214"), (_si(st.tokens), BRIGHT), (" tokens  " if wide else " ", DIM), (_si(st.usd, "usd"), BRIGHT),
                ("    " if wide else "  ", "")]
        for k, (glyph, code) in STATUS.items():
            segs += [(glyph, "1;" + code if c[k] else DIM), (f" {c[k]}", BRIGHT if c[k] else DIM), (f" {k}   " if wide else "  ", DIM)]
        return segs + ([("◌ ", DIM), (str(waiting), TEXT), (" queued" if wide else "", DIM)] if waiting else [])
    if st.end:
        right = [("■ ", "1;" + GOOD), (st.end["reason"] or "finished", TEXT)] + (
            [("  ", ""), (st.end["report"], "1;38;5;75")] if st.end["report"] else [])
    else:
        right = [("elapsed ", DIM), (_clock(st.now - st.started), TEXT)] if st.started else []
    right += [(" ", "")]
    segs = next((lf + [(" " * (W - _sw(lf) - _sw(right)), "")] + right for lf in (left(True), left(False))
                 if _sw(lf) + _sw(right) < W), left(False))
    return _line(segs, W, bg=235 if _colored() else None)


def _bottom(st: DashState, W: int, B: int, step: float, end: float) -> list[str]:
    """Recently landed feed + burn-rate chart, side by side when wide, stacked in one box when narrow."""
    done = st.counts["done"]
    lt = [("recently landed", BRIGHT)]
    lr = [(f"{done} landed", GOOD)] if done else []
    if W >= 96:
        lw = W * 52 // 100
        cw = W - lw
        vals = _series(st.samples, end, step, cw - 4)
        rows = B - 1 if B >= 4 else B
        chart = _area(vals, rows) + ([_axis(cw - 4, step)] if B >= 4 else [])
        left = _box(lw, lt, (_landed(st, lw - 4, B) + [[]] * B)[:B], lr)
        right = _box(cw, [("burn rate", BRIGHT)], chart, _rate_label(vals, step))
        return [a + b for a, b in zip(left, right)]
    vals = _series(st.samples, end, step, W - 4)
    body = _area(vals, 1) + _landed(st, W - 4, B - 1)
    return _box(W, lt, (body + [[]] * B)[:B], _rate_label(vals, step))


def _rate_label(vals, step: float) -> list:
    if not any(vals):
        return [("tokens/s", DIM)]
    k = max(1, int(10 // step))
    now = sum(vals[-k:]) / k
    return [(_si(now), "1;38;5;214"), (" tok/s", DIM), ("  peak ", DIM), (_si(max(vals)), TEXT)]


def _axis(n: int, step: float) -> list:
    m, s = divmod(int(step * n), 60)
    ago = f"-{m}m{s:02d}s" if m and s else f"-{m}m" if m else f"-{s}s"
    return [(ago, DIM), ("╌" * max(0, n - len(ago) - 4), FRAME), (" now", DIM)]


def _mini(st: DashState, W: int, H: int) -> str:
    """Tiny terminals: plain lines, most important first."""
    running = sum(s.status == "running" for s in st.slots.values())
    c = st.counts
    lines = [[("relay burn week", "1;38;5;214")],
             [(f"{running} running · ✓{c['done']} ◆{c['blocked']} ✗{c['failed']} ⊘{c['limited']} · {_si(st.tokens)} tok", TEXT)]]
    for m in st.meters.values():
        pct = f"{st.used(m) / m.limit:.0%}" if m.limit else "?"
        lines.append([(f"{pct:>4} ", BRIGHT), (m.name, f"38;5;{FLAVORS.get(st.meter_flavor(m), GREY)[0]}")])
    for k in sorted(st.slots):
        s = st.slots[k]
        if s.status == "running":
            lines.append([(f"{k} ", DIM), (s.lane + " ", f"38;5;{FLAVORS.get(st.flavor(s.lane), GREY)[0]}"), (s.text, TEXT)])
    return "\n".join(_line(segs, W) for segs in lines[:H])


def render(state: DashState, width: int = 120, height: int = 34) -> str:
    """Draw one frame. Pure: reads only `state`. Every line fits `width` cells; at most `height` lines."""
    st, W, H = state, max(1, int(width)), max(1, int(height))
    if W < 48 or H < 14:
        return _mini(st, W, H)
    head, foot = _header(st, W), _footer(st, W)
    if not (st.started or st.slots or st.meters):
        box = _box(W, [("agents", BRIGHT)], [[("○ ", DIM), (st.hint, TEXT)]])
        return "\n".join(([head] + box)[:H - 1] + [""] * (H - 2 - len(box)) + [foot])
    chart_w = W - W * 52 // 100 - 4 if W >= 96 else W - 4
    step = _step(st, chart_w)
    end = _chart_end(st, step)
    meters = _meters(st, W - 4)[:max(1, H - 9)]
    head_row, slots = _agents(st, W - 4, step, end)
    avail = H - 2 - (len(meters) + 2 if meters else 0) - 3          # header, footer, meter box, agent box frame
    idle = [k for k in sorted(st.slots) if st.slots[k].status == "idle" and not st.slots[k].task_id]
    if len(slots) > avail - 5 and len(idle) > 1:                    # fold idle slots before losing the bottom boxes
        slots = [r for k, r in zip(sorted(st.slots), slots) if k not in idle] + [[("·", DIM), (f"  {len(idle)} idle slots", DIM)]]
    B = min(16, avail - len(slots) - 2)
    if B < 3:
        B, keep = 0, max(1, avail)
        if len(slots) > keep:
            slots = slots[:keep - 1] + [[(f"… {len(slots) - keep + 1} more", DIM)]]
    running, waiting = sum(s.status == "running" for s in st.slots.values()), st.waiting
    out = [head]
    if meters:
        out += _box(W, [("burn meters", BRIGHT)], meters, [("┃", "1;38;5;231"), (f" target {st.target_pct:.0%}", DIM)])
    out += _box(W, [("agents", BRIGHT), ("  ", ""), (f"{running}/{st.max_agents or len(st.slots)}", "1;38;5;214"),
                    (" running", DIM)], [head_row] + slots, [(f"{waiting} queued", DIM)] if waiting else ())
    if B:
        out += _bottom(st, W, B, step, end)
    out = out[:H - 1]
    return "\n".join(out + [""] * (H - 1 - len(out)) + [foot])        # footer pinned to the bottom row


# --------------------------------------------------------------------------- terminal plumbing
def _isatty(f) -> bool:
    try:
        return bool(f.isatty())
    except (AttributeError, ValueError, OSError):
        return False


class _Screen:
    """Alternate screen, hidden cursor, in-place repaints of changed lines; SIGWINCH repaints; close() restores."""

    def __init__(self, out):
        self.out, self.size_was, self.prev, self.hooked, self.active = out, None, None, False, False
        self.lines: list[str] = []
        self.frames, self.resized, self.halt = 0, False, False

    @staticmethod
    def size() -> tuple[int, int]:
        s = shutil.get_terminal_size((120, 34))
        return max(1, s.columns), max(1, s.lines)

    def open(self) -> None:
        self.active = True
        self._write("\033[?1049h\033[?25l\033[?7l\033[2J")          # alt screen, no cursor, no auto-wrap
        try:
            self.prev, self.hooked = signal.signal(signal.SIGWINCH, self._winch), True
        except (AttributeError, ValueError, OSError):      # no SIGWINCH (Windows) or not the main thread
            pass

    def _winch(self, signum, frame) -> None:
        self.resized = True                 # just a flag: a handler that takes a lock can deadlock the main thread
        if callable(self.prev):
            self.prev(signum, frame)

    def sleep(self, seconds: float) -> None:
        """Wait up to `seconds`, waking early (within 50 ms) on a resize or halt."""
        end = time.monotonic() + max(seconds, 0.02)
        while not (self.resized or self.halt) and time.monotonic() < end:
            time.sleep(min(0.05, max(0.0, end - time.monotonic())))

    def paint(self, state: DashState) -> None:
        w, h = self.size()
        lines = render(state, w, h).split("\n")
        resized, self.frames, self.resized = self.resized or (w, h) != self.size_was, self.frames + 1, False
        full = resized or self.frames % 25 == 0              # now and then repaint all: stray output gets wiped
        buf = [f"\033[{i};1H{line}" + ("\033[K" if _w(_ANSI.sub("", line)) < w else "")
               for i, line in enumerate(lines, 1) if full or i > len(self.lines) or self.lines[i - 1] != line]
        if (full or len(lines) < len(self.lines)) and len(lines) < h:
            buf.append(f"\033[{len(lines) + 1};1H\033[J")
        self.size_was, self.lines = (w, h), lines
        if buf:                                             # synchronized update: no tearing where supported
            self._write("\033[?2026h" + ("\033[2J" if resized else "") + "".join(buf) + "\033[?2026l")

    def close(self, final: str | None = None) -> None:
        if not self.active:
            return
        self.active = False
        if self.hooked:
            try:
                signal.signal(signal.SIGWINCH, self.prev if self.prev is not None else signal.SIG_DFL)
            except (ValueError, OSError):
                pass
        self._write("\033[0m\033[?7h\033[?25h\033[?1049l" + (final + "\n" if final else ""))

    def _write(self, s: str) -> None:
        try:
            self.out.write(s)
            self.out.flush()
        except (OSError, ValueError):
            pass


def attach(bus: Bus, refresh: float = 0.2, force: bool = False):
    """Draw the bus live in the alternate screen from a daemon thread (only on a TTY, or with force).
    Returns stop(): restores the terminal and leaves the final frame on screen. A no-op without a TTY."""
    if not (force or _isatty(sys.stdout)):
        return lambda *a, **k: None
    state, inbox = DashState(), deque()
    unsubscribe = bus.subscribe(inbox.append)        # subscribe first, then replay: nothing falls in between
    history = list(getattr(bus, "rows", None) or [])
    seen = {id(r): r for r in history[-256:]}        # rows emitted while we subscribed arrive twice (refs keep ids unique)
    for r in history:
        state.apply(r)
    screen, lock, stopped = _Screen(sys.stdout), threading.Lock(), []

    def drain() -> None:
        while inbox:
            r = inbox.popleft()
            if id(r) not in seen:
                state.apply(r)

    def loop() -> None:
        while not screen.halt:
            try:
                drain()
                if state.end is None:
                    state.tick(time.time())
                screen.paint(state)
            except Exception:                        # a drawing bug must never take the swarm's terminal down
                pass
            screen.sleep(refresh)

    def stop(keep: bool = True) -> None:
        with lock:
            if stopped:
                return
            stopped.append(True)
        screen.halt = True
        if thread is not threading.current_thread():
            thread.join(timeout=2)
        unsubscribe()
        drain()
        screen.close(render(state, *screen.size()) if keep else None)
        atexit.unregister(stop)

    thread = threading.Thread(target=loop, name="relay-dash", daemon=True)
    screen.open()
    atexit.register(stop)
    thread.start()
    return stop


class _Follow:
    """Incremental JSONL reader: a missing file, a half-written last line or a truncated file are all fine."""

    def __init__(self, path: str):
        self.path, self.pos, self.buf, self.ino = Path(path).expanduser(), 0, b"", None

    def read(self, final: bool = False) -> list[dict]:
        try:
            st = self.path.stat()
            if st.st_size < self.pos or (self.ino is not None and st.st_ino != self.ino):
                self.pos, self.buf = 0, b""                  # truncated or replaced: start over
            self.ino = st.st_ino
            with self.path.open("rb") as f:
                f.seek(self.pos)
                data = f.read()
                self.pos = f.tell()
        except OSError:
            return []
        lines = (self.buf + data).split(b"\n")
        self.buf = b"" if final else lines.pop()
        rows = []
        for ln in lines:
            try:
                r = json.loads(ln.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(r, dict):
                rows.append(r)
        return rows


def tail(events_path: str, refresh: float = 0.2, once: bool = False) -> None:
    """Follow a JSONL ledger like `tail -f`, redrawing in place on a TTY (a frame per change otherwise).
    once=True folds the whole file, prints one frame and returns. Ctrl-C stops following."""
    state, feed = DashState(hint=f"waiting for events in {events_path}"), _Follow(events_path)
    if once:
        for r in feed.read(final=True):
            state.apply(r)
        print(render(state, *_Screen.size()))
        return
    screen = _Screen(sys.stdout) if _isatty(sys.stdout) else None
    try:
        if screen:
            screen.open()
        while True:
            rows = feed.read()
            for r in rows:
                state.apply(r)
            if screen:
                if state.end is None and state.rows and 0 <= time.time() - state.last_ts < LIVE_GAP:
                    state.tick(time.time())
                screen.paint(state)
                screen.sleep(refresh)
            else:
                if rows:
                    print(render(state, *_Screen.size()) + "\n", flush=True)
                time.sleep(max(refresh, 1.0))
    except KeyboardInterrupt:
        pass
    finally:
        if screen:
            screen.close(render(state, *screen.size()))
