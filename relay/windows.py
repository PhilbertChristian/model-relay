"""5-hour window priming: open each plan's usage window at a time you pick, so its resets land when you want them.

Claude and ChatGPT (Codex) plans meter use in rolling windows that open with your first request and reset
`window_hours` later (default 5, the key capacity.py's rolling_window subscriptions use). Prime at 05:00 and the
first reset is at 10:00; stagger several plans an hour apart and resets arrive all morning.

    "windows": {"prime_at": "05:00", "stagger_minutes": 60, "align": "hour",
                "days": ["mon", "tue", "wed", "thu", "fri"], "timezone": "America/Los_Angeles",
                "plans": [{"name": "claude-a", "kind": "claude_code", "env": {"CLAUDE_CONFIG_DIR": "~/.claude-a"}},
                          {"name": "codex", "kind": "codex"}]}

Per plan: prime_at (instead of its stagger slot), days, align, env, and window_hours / root (else those of the
subscription named like the plan, or its `subscription`). Timezone falls back as in schedule.py.

Alignment [UNVERIFIED, inferred from the reset times the CLIs report]: with "align": "hour" (default) a window
opens at the top of the hour (UTC) of its first request, so a prime at 05:01 resets at 10:00, and a stagger under
60 minutes can land two resets on the same hour. "exact": the window opens at the request itself.

A prime is the cheapest request each CLI makes, from an empty temp dir, with no tools and no permission bypass:
    claude -p "Reply with OK." --model haiku --max-turns 1 --output-format json --tools "" --strict-mcp-config
           --no-session-persistence
    codex exec --sandbox read-only "Reply with OK."      (the temp dir gets `git init`: codex exec wants a repo)
API-key variables are dropped from its environment so the plan's login answers, not API billing. A plan that
already looks active (an unexpired prime in the ledger, or transcript use recent enough to sit in a window that
is still open) is skipped with a note; --force primes anyway. Every prime logs a `window_primed` row.
"""
from __future__ import annotations

import importlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from datetime import date, datetime, timedelta, timezone

from . import schedule
from .contracts import redact
from .telemetry import Telemetry
from .ui import c

PROMPT = "Reply with OK."
LEDGER = ".relay/events.jsonl"
API_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY")


def _minutes(hm) -> int:
    h, m = (str(hm).strip() + ":0").split(":")[:2]
    return int(h) * 60 + int(m)


def agent(plan: dict) -> str:
    """The CLI a plan primes with: "codex" or "claude"."""
    return "codex" if str(plan.get("kind", "")).startswith("codex") else "claude"


def plans(cfg: dict) -> list[dict]:
    """windows.plans with every default filled in."""
    w = cfg.get("windows") or {}
    subs = {s.get("name"): s for s in cfg.get("subscriptions") or []}
    where = schedule.tz_name(cfg, w)
    out = []
    for i, p in enumerate(w.get("plans") or []):
        sub = subs.get(p.get("subscription", p.get("name")), {})
        out.append({**p, "name": p.get("name") or p.get("kind") or f"plan{i + 1}", "kind": p.get("kind", "claude_code"),
                    "env": dict(p.get("env") or {}), "slot": i, "timezone": where,
                    "days": [str(d)[:3].lower() for d in p.get("days") or w.get("days") or schedule.DAYS[:5]],
                    "window_hours": float(p.get("window_hours") or sub.get("window_hours")
                                          or w.get("window_hours") or 5),
                    "align": p.get("align") or w.get("align") or "hour", "root": p.get("root") or sub.get("root")})
    return out


def prime_minute(cfg: dict, plan: dict) -> int:
    """Minutes after midnight (may run past 24:00) at which `plan` primes: its own prime_at, else its stagger slot."""
    w = cfg.get("windows") or {}
    if plan.get("prime_at"):
        return _minutes(plan["prime_at"])
    return _minutes(w.get("prime_at", "05:00")) + plan["slot"] * int(w.get("stagger_minutes", 60))


def reset_at(prime_at: datetime, window_hours: float = 5, align: str = "hour") -> datetime:
    """When a window first used at `prime_at` resets: "hour" opens it at the top of that hour (UTC), "exact" at it."""
    u = prime_at.astimezone(timezone.utc)
    if align != "exact":
        u = u.replace(minute=0, second=0, microsecond=0)
    return (u + timedelta(hours=window_hours)).astimezone(prime_at.tzinfo)


def _day(day, where) -> date:
    if isinstance(day, str):
        return date.fromisoformat(day)
    if day is None or isinstance(day, (int, float, datetime)):
        return schedule.at(day, where).date()
    return day


def plan_primes(cfg: dict, day=None) -> list[tuple[dict, datetime, datetime]]:
    """[(plan, prime_at, reset_at)] for `day` (date, datetime, ISO date or epoch; default today), in prime order."""
    where = schedule.tz(cfg, cfg.get("windows"))
    d = _day(day, where)
    out = []
    for p in plans(cfg):
        if schedule.DAYS[d.weekday()] in p["days"]:
            t = datetime(d.year, d.month, d.day, tzinfo=where) + timedelta(minutes=prime_minute(cfg, p))
            out.append((p, t, reset_at(t, p["window_hours"], p["align"])))
    return sorted(out, key=lambda x: x[1])


# --------------------------------------------------------------------------- priming
def _env(plan: dict) -> dict:
    """Your environment minus API keys (the plan's login must answer, not API billing), plus the plan's env."""
    env = {k: v for k, v in os.environ.items() if k not in API_KEYS}
    env.update({k: os.path.expanduser(os.path.expandvars(str(v))) for k, v in plan["env"].items()})
    return env


def prime_cmd(plan: dict) -> list[str]:
    if agent(plan) == "codex":
        return ["codex", "exec", "--sandbox", "read-only", PROMPT]
    return ["claude", "-p", PROMPT, "--model", "haiku", "--max-turns", "1", "--output-format", "json",
            "--tools", "", "--strict-mcp-config", "--no-session-persistence"]


def _hm(ts: float, where) -> str:
    return datetime.fromtimestamp(ts, where).strftime("%H:%M")


def active(plan: dict, now=None, ledger: str = LEDGER) -> str:
    """Why `plan`'s window already looks open, or "": an unexpired prime in the ledger, or transcript use (via
    relay.usage) since the earliest moment whose window could still be open."""
    t, where = schedule.at(now).timestamp(), schedule.zone(plan["timezone"])
    for r in reversed(schedule.ledger_rows(ledger)):
        if r.get("event") == "window_primed" and r.get("plan") == plan["name"] and r.get("ok") \
                and float(r.get("reset_at") or 0) > t:
            return f"primed {_hm(r['prime_at'], where)}, resets {_hm(r['reset_at'], where)}"
    since = t - plan["window_hours"] * 3600
    if plan["align"] != "exact":
        since = (since // 3600 + 1) * 3600          # use before this sat in a window that has reset by now
    codex = agent(plan) == "codex"
    home = _env(plan).get("CODEX_HOME" if codex else "CLAUDE_CONFIG_DIR") or ("~/.codex" if codex else "~/.claude")
    try:
        usage = importlib.import_module("relay.usage")
        count = usage.codex_tokens if codex else usage.claude_code_tokens
        root = plan.get("root") or os.path.join(home, "sessions" if codex else "projects")
        n = int((count(since, root=root) or {}).get("total") or 0)
    except Exception:
        return ""
    return f"{n:,} tokens used since {_hm(since, where)}" if n > 0 else ""


def _attempt(plan: dict, cmd: list[str], timeout: float) -> tuple[bool, str]:
    env = _env(plan)
    exe = shutil.which(cmd[0], path=env.get("PATH"))
    if not exe:
        return False, f"{cmd[0]} not found on PATH"
    with tempfile.TemporaryDirectory(prefix="relay-prime-") as d:
        if cmd[0] == "codex":
            try:
                subprocess.run(["git", "init", "-q", d], env=env, capture_output=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                pass                                  # codex will say what it's missing
        try:
            r = subprocess.run([exe, *cmd[1:]], cwd=d, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, f"no answer in {timeout:g}s"
        except OSError as e:
            return False, str(e)
    out, err = (r.stdout or "").strip(), (r.stderr or "").strip()
    ok, note = r.returncode == 0, ((out or err).splitlines() or [""])[-1]
    if cmd[0] == "claude":
        for text in (out, (out.splitlines() or [""])[-1]):
            try:
                j = json.loads(text)
            except ValueError:
                continue
            if isinstance(j, dict):
                ok, note = ok and not j.get("is_error"), str(j.get("result") or j.get("subtype") or note)
                break
    if r.returncode:
        note = f"exit {r.returncode}: {(err or out)[-240:]}"
    return ok, redact(" ".join(note.split()))[:240]


def prime(plan: dict, dry_run: bool = False, *, now=None, tel=None, ledger: str = LEDGER, force: bool = False,
          timeout: float = 120) -> dict:
    """Open `plan`'s window now with the cheapest request. Returns {plan, kind, prime_at, reset_at, ok, note,
    skipped} and logs it as `window_primed` (a dry run logs nothing). ok is None when nothing was sent."""
    t, where = schedule.at(now).timestamp(), schedule.zone(plan["timezone"])
    row = {"plan": plan["name"], "kind": plan["kind"], "prime_at": t, "ok": None, "note": "", "skipped": False,
           "reset_at": reset_at(datetime.fromtimestamp(t, where), plan["window_hours"], plan["align"]).timestamp()}
    why = "" if force else active(plan, t, ledger)
    if why:
        row.update(reset_at=None, skipped=True, note=f"skipped, already active: {why}")
    elif dry_run:
        row["note"] = "dry run: would run " + shlex.join(prime_cmd(plan))
    else:
        row["ok"], row["note"] = _attempt(plan, prime_cmd(plan), timeout)
    if not dry_run:
        schedule.emit(tel or Telemetry(os.path.dirname(ledger) or "."), "window_primed", **row)
    return row


# --------------------------------------------------------------------------- report, jobs, CLI
def _status(r: dict | None, prime_at: datetime, now: datetime, where) -> str:
    if r is None:
        return c("2", "due") if prime_at > now else c("33", "not primed")
    if r.get("skipped"):
        return c("33", r.get("note") or "skipped")
    if r.get("ok"):
        return c("32", f"✓ primed {_hm(r['prime_at'], where)}")
    return c("31", f"✗ {r.get('note') or 'failed'}")


def report(cfg: dict, now=None, ledger: str = LEDGER) -> str:
    """Today's primes and resets, one row per plan, with what happened to each so far."""
    w = cfg.get("windows") or {}
    where = schedule.tz(cfg, w)
    t = schedule.at(now, where)
    today = plan_primes(cfg, t)
    seen: dict[str, dict] = {}
    for r in schedule.ledger_rows(ledger):
        if r.get("event") == "window_primed" and \
                datetime.fromtimestamp(float(r.get("prime_at") or r.get("ts") or 0), where).date() == t.date():
            seen[r.get("plan")] = r
    hourly = all(p["align"] != "exact" for p, _, _ in today)
    lines = [c("1", f"5-hour windows · {t:%a %b %d} · {where.key}")
             + c("2", "  (a window opens on the hour of its first use)" if today and hourly else "")]
    if not today:
        days = sorted({d for p in plans(cfg) for d in p["days"]}, key=schedule.DAYS.index)
        return "\n".join(lines + [c("2", f"  no primes today; plans prime on {', '.join(days) or 'no days'}")])
    lines.append(c("2", f"  {'plan':<14}{'kind':<13}{'prime':<7}{'reset':<7}status"))
    for p, a, z in today:
        lines.append(f"  {p['name'][:13]:<14}{p['kind'][:12]:<13}{a:%H:%M}  {z:%H:%M}  "
                     + _status(seen.get(p["name"]), a, t, where))
    return "\n".join(lines)


def job(cfg: dict, plan: dict, cfg_path: str, cwd: str = ".") -> schedule.Job:
    """`plan`'s scheduled prime. launchd/cron run `windows prime`; Orca gates its own turn (which is the prime,
    under Orca's account for that agent) on `windows due`."""
    m = prime_minute(cfg, plan)
    cal = [((schedule.DAYS.index(d) + m // 1440) % 7, m % 1440 // 60, m % 60)
           for d in plan["days"] if d in schedule.DAYS]
    name, tail = schedule.label("windows", plan["name"]), ["--plan", plan["name"], "-b", cfg_path]
    return schedule.Job(name, ["windows", "prime", *tail], calendar=cal, timezone=plan["timezone"], cwd=cwd,
                        log=schedule.log_path(cfg, name), precheck=["windows", "due", *tail], provider=agent(plan),
                        prompt=PROMPT)


def _line(r: dict, where) -> str:
    tag = c("2", "·") if r["ok"] is None else c("32", "✓") if r["ok"] else c("31", "✗")
    when = f" → resets {_hm(r['reset_at'], where)}" if r.get("reset_at") and r["ok"] is not False else ""
    return f"{tag} {r['plan']}{when}  " + c("2", r["note"])


def add_cli(subparsers):
    """`relay windows plan|prime|due|install|uninstall|status [--plan NAME] [--dry-run] [--force] [--via ...]`."""
    p = subparsers.add_parser("windows", help="open each plan's 5-hour window on schedule so resets land when you want")
    p.add_argument("action", nargs="?", default="plan",
                   choices=["plan", "prime", "due", "install", "uninstall", "status"],
                   help="plan: today's primes and resets · prime: open the windows now · due: exit 0 when the plan "
                        "needs a prime (Orca's precheck) · install|uninstall|status: the scheduled primes")
    p.add_argument("--plan", help="only this plan (a name from windows.plans)")
    p.add_argument("--dry-run", action="store_true", help="show the commands; prime and install nothing")
    p.add_argument("--force", action="store_true", help="prime even if the window already looks open")
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
    ledger = os.path.join(cwd, ".relay", "events.jsonl")
    ps = [p for p in plans(cfg) if args.plan in (None, p["name"])]
    if not ps:
        print(c("1;31", f"✗ no plan {args.plan!r} in windows.plans") if args.plan
              else c("33", "no windows.plans configured"))
        return 2
    if args.action == "plan":
        print(report(cfg, ledger=ledger))
        return 0
    where = schedule.tz(cfg, cfg.get("windows"))
    if args.action == "due":
        whys = [(p["name"], active(p, ledger=ledger)) for p in ps]
        for n, why in whys:
            print(f"{n}: " + (f"not due, already active ({why})" if why else "due"))
        return 1 if any(why for _, why in whys) else 0
    if args.action == "prime":
        rows = [prime(p, args.dry_run, ledger=ledger, force=args.force) for p in ps]
        for r in rows:
            print(_line(r, where))
        return 1 if any(r["ok"] is False for r in rows) else 0
    rc = 0
    for p in ps:
        j = job(cfg, p, path, cwd)
        if args.action == "status":
            print(schedule.status_line(schedule.status(j.label, args.via)))
            continue
        m = prime_minute(cfg, p)
        days = ",".join(p["days"])
        print(c("1", p["name"]) + c("2", f"  primes {m % 1440 // 60:02d}:{m % 60:02d} {days} ({where.key})"))
        ok, msg = (schedule.install(j, args.via, args.dry_run) if args.action == "install"
                   else schedule.uninstall(j.label, args.via, args.dry_run))
        print((c("32", "✓ ") if ok else c("31", "✗ ")) + msg)
        rc |= not ok
    return rc
