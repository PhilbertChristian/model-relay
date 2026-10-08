"""End-of-week autoburn, per plan: when a subscription's own reset is near and your pace leaves capacity above
your reserve, burn it while you're off.

    "autoburn": {"enabled": false, "start_hours_before_reset": 36, "min_leftover_pct": 0.10,
                 "allow_work_hours": false, "min_hours": 1}

Every subscription keeps its own clock: relay.usage gives each Usage its own resets_at (a Claude seat on Thu 14:00,
another on Sat 09:00, Codex on Mon 07:00, API budgets on the 1st). For each one, autoburn projects use at its reset
from its pace (used ÷ elapsed share of its period: a week as long as usage.week_bounds says, or a calendar month
for monthly budgets) and calls the plan ready when:
  - its reset is at most start_hours_before_reset away
  - its projected leftover above its reserve (schedule.reserve_pct) is at least min_leftover_pct of its limit
  - an enabled lane burns it, and lane_limited rows in the ledger don't still hold all of them
  - it isn't pay-as-you-go (api_budget without "expires": spending it is new money, capacity.py's rule)
  - it wasn't autoburned in this idle window already (<data_dir>/autoburn.json), so a burn that stops early is
    never re-fired in a loop
  - at least min_hours remain before its deadline: its reset, or your next work start if that comes first
It fires when autoburn.enabled, you're outside work hours (schedule.py; unless allow_work_hours), no autoburn is
running (<data_dir>/autoburn.lock) and some plan is ready. burn_week then runs only the ready plans' lanes, most
urgent first (leftover ÷ hours to deadline), aiming at the strictest 1 - reserve among them; the swarm stops each
lane at its own reset and the whole burn at the latest deadline.
Rows: `autoburn_check` on every check, `autoburn_fire` when it fires. Hourly job: `relay burn auto --install`.
"""
from __future__ import annotations

import importlib
import json
import os
import shlex
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from . import schedule
from .bus import Bus
from .telemetry import Telemetry
from .ui import c

LEDGER = ".relay/events.jsonl"


@dataclass
class PlanCheck:
    """One subscription's verdict, on its own clock."""
    name: str
    resets_at: float                                    # epoch seconds: this subscription's own reset
    hours_left: float                                   # until that reset
    leftover: float                                     # projected share of its limit left above the reserve at reset
    fire: bool                                          # burned by this check
    reason: str
    ready: bool = False                                 # passes its own checks; fires unless a global gate holds
    used: float = 0.0
    limit: float | None = None
    unit: str = "tokens"
    projected_used: float | None = None
    target_pct: float = 1 - schedule.DEFAULT_RESERVE
    lanes: list[str] = field(default_factory=list)      # enabled lanes that burn it and aren't held by a limit
    deadline: float = 0.0                               # its reset, or your next work start if that comes first
    urgency: float = 0.0                                # leftover ÷ hours to deadline: highest burns first


@dataclass
class Decision:
    fire: bool
    reason: str
    hours_left: float                                   # until the soonest reset among ready plans (else any plan)
    plans: list[PlanCheck] = field(default_factory=list)   # one per subscription, most urgent first
    lanes: list[str] = field(default_factory=list)      # lanes of the ready plans, most urgent first
    burn_hours: float = 0.0                             # until the latest deadline among the ready plans
    target_pct: float = 1 - schedule.DEFAULT_RESERVE    # the strictest 1 - reserve among them
    result: dict | None = None                          # burn_week's result, once fired


def _span(h: float) -> str:
    return "now" if h <= 0 else f"{h * 60:.0f}m" if h < 1 else f"{h:.0f}h" if h < 100 else f"{h / 24:.0f}d"


def _lane(spec: dict) -> str:
    return spec.get("name") or spec.get("kind") or "lane"


def _lanes(cfg: dict, name: str) -> list[str]:
    """Enabled lanes that burn subscription `name`."""
    return [_lane(s) for s in cfg.get("lanes") or []
            if s.get("enabled") is not False and (s.get("subscription") or _lane(s)) == name]


def _monthly(sub: dict) -> bool:
    k = sub.get("kind")
    return k in ("agent37_budget", "api_budget") or (k == "monid_credits" and "reset_day" in sub)


def _elapsed(cfg: dict, sub: dict, end: float, t: float) -> float:
    """Share of the subscription's current period gone by `t` (0.02-1). The period ends at its own reset and lasts
    a week (as long as usage.week_bounds says, DST included) or, for monthly budgets, a calendar month."""
    if _monthly(sub):
        d = datetime.fromtimestamp(end, timezone.utc)
        start = d.replace(year=d.year - (d.month == 1), month=(d.month - 2) % 12 + 1, day=min(d.day, 28)).timestamp()
    else:
        week = cfg.get("week") or {}
        try:
            w0, w1 = importlib.import_module("relay.usage").week_bounds(
                sub.get("reset") or week.get("reset") or "mon 09:00", sub.get("timezone") or week.get("timezone"), t)
            start = end - (w1 - w0)
        except Exception:
            start = end - 7 * 86400
    return min(1.0, max(0.02, (t - start) / max(1.0, end - start)))


# --------------------------------------------------------------------------- lock, state, limits
def running(cfg: dict) -> int | None:
    """PID of the autoburn holding <data_dir>/autoburn.lock (0: one is writing it now); None when nothing runs."""
    path = schedule.data_dir(cfg) / "autoburn.lock"
    try:
        raw, age = path.read_text(), time.time() - path.stat().st_mtime
    except OSError:
        return None
    try:
        pid = int(json.loads(raw)["pid"])
    except (ValueError, KeyError, TypeError):
        return 0 if age < 60 else None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)                                 # signal 0: only asks whether the process exists
    except ProcessLookupError:
        return None                                     # stale lock: its process is gone
    except PermissionError:
        pass
    except OSError:
        return None
    return pid


def _lock(cfg: dict):
    path = schedule.data_dir(cfg) / "autoburn.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if running(cfg) is not None:
                return None
            path.unlink(missing_ok=True)                # stale
            continue
        with os.fdopen(fd, "w") as f:
            json.dump({"pid": os.getpid(), "ts": time.time()}, f)
        return path
    return None


def _state(cfg: dict) -> dict:
    try:
        s = json.loads((schedule.data_dir(cfg) / "autoburn.json").read_text())
    except (OSError, ValueError):
        return {}
    return s if isinstance(s, dict) else {}


def _held(ledger: str, t: float) -> dict[str, float]:
    """{lane: until} for lanes a lane_limited row still holds at `t`."""
    out: dict[str, float] = {}
    for r in schedule.ledger_rows(ledger):
        if r.get("event") == "lane_limited":
            try:
                until = float(r.get("until") or 0)
            except (TypeError, ValueError):
                continue
            if until > t:
                out[str(r.get("lane"))] = max(until, out.get(str(r.get("lane")), 0.0))
    return out


# --------------------------------------------------------------------------- decide and run
def decide(cfg: dict, usages: list, now=None, *, ledger: str = LEDGER) -> Decision:
    """Which plans to burn now, each judged on its own reset. Reads only the lock, the state file and the ledger."""
    a = cfg.get("autoburn") or {}
    t, where = schedule.at(now).timestamp(), schedule.tz(cfg)
    when = lambda ts: datetime.fromtimestamp(ts, where).strftime("%a %H:%M")      # noqa: E731
    start_h = float(a.get("start_hours_before_reset", 36))
    min_left, min_h = float(a.get("min_leftover_pct", 0.10)), float(a.get("min_hours", 1))
    anytime = bool(a.get("allow_work_hours"))
    working = not anytime and schedule.in_work_hours(cfg, t)
    work_at = None if anytime else schedule.next_work_start(cfg, t).timestamp()
    subs = {s.get("name"): s for s in cfg.get("subscriptions") or []}
    fired, held = _state(cfg).get("fired") or {}, _held(ledger, t)
    plans = []
    for u in usages:
        sc, end, lanes = subs.get(u.name, {}), float(u.resets_at), _lanes(cfg, u.name)
        free = [n for n in lanes if n not in held]
        deadline = min(end, work_at) if work_at and work_at > t else end
        p = PlanCheck(u.name, end, round((end - t) / 3600, 2), 0.0, False, "", used=u.used, limit=u.limit,
                      unit=u.unit, target_pct=schedule.target_pct(cfg, u.name), lanes=free, deadline=deadline)
        if u.limit:
            proj = min(float(u.limit), u.used / _elapsed(cfg, sc, end, t))
            p.projected_used, p.leftover = round(proj, 2), round(max(0.0, p.target_pct - proj / u.limit), 4)
        note, paygo = f" ({u.note})" if u.note else "", sc.get("kind") == "api_budget" and not sc.get("expires")
        done_until, short = float(fired.get(u.name) or 0), not working and deadline - t < min_h * 3600
        early = p.hours_left > start_h
        p.reason = ("no known limit" + note if not u.limit
                    else "reset unknown" + note if end <= t
                    else "pay-as-you-go: spending it is new money" if paygo
                    else "no enabled lane burns it" if not lanes
                    else f"lanes limited until {when(min(held[n] for n in lanes))}" if not free
                    else f"resets in {_span(p.hours_left)}; autoburn starts {start_h:g}h before" if early
                    else f"on pace to use it ({p.leftover:.0%} above reserve)" if p.leftover < min_left
                    else f"autoburned this window, until {when(done_until)}" if done_until > t
                    else f"only {(deadline - t) / 3600:.1f}h before work starts" if short
                    else "")
        if not p.reason:
            p.ready, p.urgency = True, round(p.leftover / max(0.25, (deadline - t) / 3600), 6)
            p.reason = f"{p.leftover:.0%} of its limit would expire above the {1 - p.target_pct:.0%} reserve"
        plans.append(p)
    plans.sort(key=lambda p: (not p.ready, -p.urgency, p.hours_left if p.hours_left > 0 else float("inf")))
    ready, pid = [p for p in plans if p.ready], running(cfg)
    soon = min((p for p in plans if p.hours_left > 0), key=lambda p: p.hours_left, default=None)
    free_at = schedule.next_free_time(cfg, t) if working else None
    hold = ("autoburn is off: set autoburn.enabled" if not a.get("enabled")
            else "an autoburn is already running" + (f" (pid {pid})" if pid else "") if pid is not None
            else ("nothing to burn yet" + (f": soonest reset is {soon.name} in {_span(soon.hours_left)}" if soon
                                           else ": no subscription measured")) if not ready
            else f"{len(ready)} plan{'s' * (len(ready) > 1)} ready; waiting for your work day to end "
                 f"({when(free_at.timestamp()) if free_at else 'not this week'}) or set autoburn.allow_work_hours"
            if working else "")
    for p in ready:
        p.fire = not hold
    return Decision(not hold, hold or "burning " + ", ".join(f"{p.name} until {when(p.deadline)}" for p in ready),
                    round(min((p.hours_left for p in ready), default=soon.hours_left if soon else 0.0), 2), plans,
                    list(dict.fromkeys(n for p in ready for n in p.lanes)),
                    round(max(0.0, max((p.deadline for p in ready), default=t) - t) / 3600, 2),
                    min((p.target_pct for p in ready), default=schedule.target_pct(cfg)))


def burn_config(cfg: dict, d: Decision) -> dict:
    """What burn_week runs: only the ready plans' lanes, most urgent first, and week.target_pct = 1 - reserve."""
    order, specs = {n: i for i, n in enumerate(d.lanes)}, cfg.get("lanes") or []
    keep = sorted((s for s in specs if _lane(s) in order), key=lambda s: order[_lane(s)])
    return {**cfg, "week": {**(cfg.get("week") or {}), "target_pct": d.target_pct},
            "lanes": keep + [{**s, "enabled": False} for s in specs if _lane(s) not in order]}


def run(cfg: dict, now=None, dry_run: bool = False, *, ledger: str = LEDGER, tel=None) -> Decision:
    """Measure every subscription (relay.usage.weekly_usage), decide, log `autoburn_check`; when it fires, log
    `autoburn_fire` and run relay.swarm.burn_week under the lock. dry_run asks burn_week for its plan only."""
    t = schedule.at(now).timestamp()
    usages = importlib.import_module("relay.usage").weekly_usage(cfg, ledger=ledger, now=t)
    d = decide(cfg, usages, t, ledger=ledger)
    tel = tel or Telemetry(os.path.dirname(ledger) or ".")
    schedule.emit(tel, "autoburn_check", fire=d.fire, reason=d.reason, hours_left=d.hours_left,
                  burn_hours=d.burn_hours, lanes=d.lanes, target_pct=d.target_pct,
                  plans=[asdict(p) for p in d.plans], dry_run=dry_run)
    if not d.fire:
        return d
    swarm, bcfg = importlib.import_module("relay.swarm"), burn_config(cfg, d)
    if dry_run:
        d.result = swarm.burn_week(bcfg, hours=d.burn_hours, dry_run=True, bus=Bus(tel))
        return d
    lock = _lock(cfg)
    if lock is None:
        d.fire, d.reason = False, "another autoburn started first"
        for p in d.plans:
            p.fire = False
        return d
    try:
        firing = [p for p in d.plans if p.fire]
        state = _state(cfg)
        state["fired"] = {**(state.get("fired") or {}), **{p.name: p.deadline for p in firing}}
        state["last"] = {"ts": t, "plans": [p.name for p in firing], "lanes": d.lanes, "hours": d.burn_hours}
        (schedule.data_dir(cfg) / "autoburn.json").write_text(json.dumps(state, indent=1))
        schedule.emit(tel, "autoburn_fire", reason=d.reason, lanes=d.lanes, hours=d.burn_hours,
                      target_pct=d.target_pct, plans=[{"name": p.name, "resets_at": p.resets_at, "deadline": p.deadline,
                                                       "leftover": p.leftover, "lanes": p.lanes} for p in firing])
        d.result = swarm.burn_week(bcfg, hours=d.burn_hours, bus=Bus(tel))
    finally:
        lock.unlink(missing_ok=True)
    return d


# --------------------------------------------------------------------------- report, job, CLI
def report(d: Decision, where=None) -> str:
    """The decision, then one row per plan, most urgent first."""
    when = lambda ts: datetime.fromtimestamp(ts, where).strftime("%a %H:%M")      # noqa: E731
    lines = [(c("1;32", "autoburn · FIRE") if d.fire else c("1", "autoburn · waiting")) + f"  {d.reason}",
             c("2", f"  {'plan':<14}{'used':>6}{'at reset':>10}{'target':>8}{'spare':>7}{'resets':>8}  status")]
    for p in d.plans:
        pct = (lambda v: "—" if v is None or not p.limit else f"{v / p.limit:.0%}")    # noqa: E731
        status = (c("1;32", f"burn until {when(p.deadline)}") + c("2", f" · {p.reason}") if p.fire
                  else c("33", "ready") + c("2", f" · {p.reason}") if p.ready else c("2", p.reason))
        lines.append(f"  {p.name[:13]:<14}{pct(p.used):>6}{pct(p.projected_used):>10}{p.target_pct:>8.0%}"
                     f"{p.leftover:>7.0%}{_span(p.hours_left):>8}  {status}")
    if d.fire:
        lines.append(c("2", f"  lanes {', '.join(d.lanes)} · aiming at {d.target_pct:.0%} · done within "
                            f"{d.burn_hours:.1f}h"))
    return "\n".join(lines)


def _summary(r: dict) -> str:
    if r.get("dry_run"):
        return f"dry run: {len(r.get('queue') or [])} tasks queued" + (f" · {r['schedule']}" if r.get("schedule")
                                                                        else "")
    return " · ".join(f"{k} {r[k]}" for k in ("done", "blocked", "failed", "limited", "reason", "report")
                      if r.get(k) not in (None, ""))


def job(cfg: dict, cfg_path: str, cwd: str = ".") -> schedule.Job:
    """Hourly `relay burn auto --check`. Under Orca, `--check --detach` is the precheck, then one short turn."""
    name = schedule.label("autoburn")
    kinds = {s.get("kind") for s in cfg.get("lanes") or [] if s.get("enabled") is not False}
    return schedule.Job(name, ["burn", "auto", "--check", "-b", cfg_path], interval=3600,
                        timezone=schedule.tz_name(cfg), cwd=cwd, log=schedule.log_path(cfg, name),
                        precheck=["burn", "auto", "--check", "--detach", "-b", cfg_path],
                        provider="codex" if "codex" in kinds and "claude" not in kinds else "claude",
                        prompt="Relay autoburn started a burn in the background. Reply with OK.")


def add_cli(subparsers):
    """`relay burn auto [--check] [--dry-run] [--detach] [--install|--uninstall|--status] [--via ...] [-b FILE]`;
    add it to the `burn` subparsers. Without --check it only shows the decision."""
    p = subparsers.add_parser("auto", help="end-of-week autoburn: per plan, burn what would expire while you're off")
    p.add_argument("--check", action="store_true", help="decide and fire if due (what the hourly job runs)")
    p.add_argument("--dry-run", action="store_true", help="show the decision and burn plan; launch or install nothing")
    p.add_argument("--detach", action="store_true", help="fire in the background; exit 0 only if a burn started "
                                                         "(Orca's precheck)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--install", action="store_true", help="run --check every hour")
    g.add_argument("--uninstall", action="store_true", help="remove the hourly job")
    g.add_argument("--status", action="store_true", help="is the hourly job installed?")
    p.add_argument("--via", choices=schedule.VIAS, help="scheduler (default: launchd on macOS, else cron)")
    p.add_argument("-b", "--burner", help="burn-week config (default: ./burner-week.json)")
    p.set_defaults(func=run_cli)
    return p


def run_cli(args) -> int:
    try:
        cfg, path = schedule.load_cfg(getattr(args, "burner", None))
    except (OSError, ValueError) as e:
        print(c("1;31", f"✗ config: {e}"))
        return 2
    cwd = os.path.abspath(getattr(args, "cwd", None) or ".")
    ledger, where = os.path.join(cwd, ".relay", "events.jsonl"), schedule.tz(cfg)
    if args.install or args.uninstall or args.status:
        j = job(cfg, path, cwd)
        if args.status:
            print(schedule.status_line(schedule.status(j.label, args.via)))
            return 0
        if args.install and not (cfg.get("autoburn") or {}).get("enabled"):
            print(c("33", "! autoburn.enabled is false: the job will check every hour and never fire"))
        ok, msg = (schedule.install(j, args.via, args.dry_run) if args.install
                   else schedule.uninstall(j.label, args.via, args.dry_run))
        print((c("32", "✓ ") if ok else c("31", "✗ ")) + msg)
        return 0 if ok else 1
    if args.check and not args.detach:
        d = run(cfg, dry_run=args.dry_run, ledger=ledger)
        print(report(d, where))
        if d.result:
            print(c("2", "  " + _summary(d.result)))
        return 0
    d = decide(cfg, importlib.import_module("relay.usage").weekly_usage(cfg, ledger=ledger), ledger=ledger)
    print(report(d, where))
    if not args.detach:
        return 0
    if not d.fire:
        return 1
    argv = schedule.command(["burn", "auto", "--check", "-b", path])
    if args.dry_run:
        print("would start in the background: " + shlex.join(argv))
        return 0
    log = schedule.log_path(cfg, schedule.label("autoburn"))
    os.makedirs(os.path.dirname(log), exist_ok=True)
    with open(log, "a") as f:
        child = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT,
                                 start_new_session=True, env={**os.environ, "PYTHONPATH": schedule.ROOT})
    print(c("32", f"✓ burn started in the background (pid {child.pid}); log {log}"))
    return 0
