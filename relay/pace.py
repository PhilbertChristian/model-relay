"""Pacing for burn-week: how many agents each lane should run so its subscription lands near target_pct right
before the reset (use it or lose it), plus the damped controller that corrects the count while the swarm runs.

  target_rate  (target_pct x limit - used) / hours to reset           units/hour still needed
  agents       ceil(target_rate / agent_rate), capped by the lane's max_agents; when the lanes together want more
               than the global max_agents, it is split across them in proportion to what each wants
  agent_rate   lane config "agent_rate" (units/hour one busy agent burns), else DEFAULT_RATE for the unit
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime

from .contracts import PacePlan, Usage, redact
from .ui import c
from .usage import human, span

# Rough units/hour one busy agent burns. Tokens include cache reads, as Claude Code / Codex usage counts them.
DEFAULT_RATE = {"tokens": 10_000_000, "usd": 1.0, "credits": 20.0}
MIN_HOURS = 0.25      # the last quarter hour still gets a finite (large) target rate
BAND = 0.10           # adjust() holds while the observed rate is within ±10% of target
MAX_STEP = 2          # adjust() moves at most this many agents per call


@dataclass
class LanePace(PacePlan):
    """A PacePlan plus the subscription's used/limit and why a lane is idle; to_dict() of it is the `pace` event."""
    used: float = 0.0
    limit: float | None = None
    note: str = ""

    @property
    def pct(self) -> float | None:
        return None if not self.limit else min(1.0, self.used / self.limit)


def _lane(spec) -> dict:
    """A lane config dict, or a live Lane object (which also knows limited_until)."""
    if isinstance(spec, dict):
        name = spec.get("name") or spec.get("kind") or "lane"
        return {**spec, "name": name, "subscription": spec.get("subscription") or name}
    return {**getattr(spec, "cfg", {}), "name": spec.name, "subscription": spec.subscription,
            "max_agents": spec.max_agents, "limited_until": spec.limited_until}


def _clock(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%a %H:%M")
    except (OverflowError, OSError, ValueError):
        return "further notice"


def _split(want: list[int], total: int) -> list[int]:
    """Share `total` agents in proportion to `want`: each one goes to the lane missing the largest share of its
    ask (ties: the bigger ask, then config order), so every lane gets one before any lane gets a second."""
    got = [0] * len(want)
    for _ in range(min(total, sum(want))):
        i = max((j for j, w in enumerate(want) if got[j] < w), key=lambda j: ((want[j] - got[j]) / want[j], want[j]))
        got[i] += 1
    return got


def plan_pace(usages: list[Usage], lanes_cfg: list[dict], max_agents: int = 12, target_pct: float = 0.97,
              now: float | None = None) -> list[PacePlan]:
    """One plan per enabled lane whose subscription is in `usages` (lanes_cfg: config dicts or Lane objects).
    Exhausted, at-target, limited and past-reset lanes get 0 agents; a subscription with no limit gets the lane's
    max_agents. Lanes sharing a subscription share its rate, earlier lanes filling up first."""
    now = time.time() if now is None else float(now)
    goal = min(1.0, max(0.0, float(target_pct)))
    by_name = {u.name: u for u in usages}
    plans: list[LanePace] = []
    left: dict[str, float] = {}            # per subscription: units/hour not yet given to a lane
    last: dict[str, LanePace] = {}         # per subscription: the last lane that took some
    for spec in lanes_cfg:
        ln = _lane(spec)
        u = by_name.get(ln["subscription"])
        if u is None or ln.get("enabled") is False:
            continue
        hours = (u.resets_at - now) / 3600
        p = LanePace(ln["name"], u.name, u.unit, u.remaining or 0.0, round(max(0.0, hours), 2), 0.0, 0, u.used, u.limit)
        plans.append(p)
        cap = max(0, int(ln.get("max_agents", 4)))
        need = None if u.limit is None else max(0.0, goal * u.limit - u.used)
        if hours <= 0:
            p.note = "reset passed"
        elif float(ln.get("limited_until") or 0) > now:
            p.note = "limited until " + _clock(float(ln["limited_until"]))
        elif need is None:
            p.agents, p.note = cap, "no limit set"
        elif need <= 0:
            p.note = "no budget" if not u.limit else "exhausted" if u.used >= u.limit else "at target"
        elif left.setdefault(u.name, need / max(hours, MIN_HOURS)) <= 0:
            p.note = "covered by other lanes"
        else:
            rate = float(ln.get("agent_rate") or DEFAULT_RATE.get(u.unit, 1.0))
            p.agents = min(cap, max(1, math.ceil(left[u.name] / rate - 1e-9)))
            p.target_rate = min(left[u.name], p.agents * rate)
            left[u.name] -= p.target_rate
            last[u.name] = p
    for name, p in last.items():
        p.target_rate += left[name]        # what its lanes can't carry still has to be burned by reset
    want = [p.agents for p in plans]
    total = 12 if max_agents is None else max(0, int(max_agents))
    if sum(want) > total:
        for p, n in zip(plans, _split(want, total)):
            p.agents = n
    for p in plans:
        p.target_rate = round(p.target_rate, 4)
    return plans


def adjust(current: int, observed_rate: float, target_rate: float, lo: int = 1, hi: int = 12) -> int:
    """Next agent count for a lane, from a damped proportional controller. It holds while the observed rate is
    within ±10% of target; otherwise it steps half way to current x target/observed, rounded so it never passes
    that point, by at most ±2, so it settles without oscillating. With no rate observed yet it holds (or starts
    one agent); with nothing left to burn it steps down. The result is clamped to [lo, hi], and hi wins."""
    def clamp(n: int) -> int:
        return max(0, min(hi, max(lo, n)))

    current = int(current)
    if target_rate <= 0:
        return clamp(current - MAX_STEP)
    if observed_rate <= 0 or current <= 0:
        return clamp(max(current, 1))
    ratio = target_rate / observed_rate
    if not abs(ratio - 1) > BAND:                            # inside the band (or NaN): hold
        return clamp(current)
    half = max(-MAX_STEP, min(MAX_STEP, (current * ratio - current) / 2))
    return clamp(current + int(half + (0.5 if half > 0 else -0.5)))   # rounded half away from zero


def _left(p: PacePlan) -> str:
    """'38% left' when the plan knows its limit, else the units left."""
    lim = getattr(p, "limit", 0)
    if lim is None:
        return "no limit"
    return f"{p.remaining / lim:.0%} left" if lim else f"{human(p.remaining, p.unit)} left"


def schedule_note(plans: list[PacePlan]) -> str:
    """One line, e.g. 'claude-max: 38% left, 61h to reset → 6 agents · codex: … → 0 agents (limited until …)'."""
    groups: dict[str, list[PacePlan]] = {}
    for p in plans:
        groups.setdefault(p.subscription, []).append(p)
    parts = []
    for sub, ps in groups.items():
        n = sum(q.agents for q in ps)
        why = "" if n else next((q.note for q in ps if getattr(q, "note", "")), "")
        parts.append(f"{sub}: {_left(ps[0])}, {span(ps[0].hours_left)} to reset → {n} agent{'' if n == 1 else 's'}"
                     + (f" ({why})" if why else ""))
    return redact(" · ".join(parts) or "no lane burns a known subscription")


def report(plans: list[PacePlan]) -> str:
    """Colored table: what's left per lane, time to reset, the rate still needed and the agents to run."""
    if not plans:
        return "no lane burns a known subscription"
    lines = [c("1", f"{'lane':<12}{'subscription':<14}{'left':>16}{'reset':>7}{'need/h':>10}{'agents':>8}")]
    for p in plans:
        lim = getattr(p, "limit", 0)
        left = "no limit" if lim is None else human(p.remaining, p.unit) + (f" {p.remaining / lim:4.0%}" if lim else "")
        row = (f"{p.lane[:11]:<12}{p.subscription[:13]:<14}{left[:16]:>16}{span(p.hours_left):>7}"
               f"{human(p.target_rate, p.unit) + '/h':>10}")
        idle = getattr(p, "note", "") or "idle"
        lines.append(row + (c("1;32", f"{p.agents:>8}") + "  " + c("32", "●" * p.agents) if p.agents
                            else c("2", f"{0:>8}  {idle}")))
    total = sum(p.agents for p in plans)
    lines.append(c("1", f"{'total':<12}{'':<14}{'':>16}{'':>7}{'':>10}{total:>8}"))
    return redact("\n".join(lines))
