"""Your schedule, your reserve, and the jobs that run relay on them.

Schedule: when you work comes from the `idle` block capacity.py already reads; work hours are whatever isn't idle.
Nine to six on weekdays:
    "idle": {"timezone": "America/Los_Angeles", "weekday": "18:00-09:00", "weekend": "all"}
Without `idle`, capacity.py's default applies (idle weekdays 23:00-07:00 and all weekend). Timezone: the feature's
own `timezone`, else idle.timezone, else week.timezone, else $TZ, else this machine's zone.

Reserve: the share of a subscription you keep for yourself: subscriptions[].reserve_pct, else the top-level
`reserve_pct`, else 0.15. A fraction (0.15) or a percent (15), as capacity.py reads reserve_pct. A burn aims for
target_pct = 1 - reserve.

Jobs run a relay command on a weekly calendar or an interval. Only an explicit install command installs one:
    launchd  ~/Library/LaunchAgents/<label>.plist, then `launchctl bootstrap gui/<uid>` (default on macOS)
    orca     `orca automations create`: Orca runs the job's precheck command (exit 0 = go), then one short agent
             turn. Binary: $ORCA_BIN, else /Applications/Orca.app/Contents/Resources/bin/orca
    cron     prints crontab lines for you to add; relay never edits your crontab (default elsewhere)
launchd and cron read the machine's clock, so calendars are converted to it at install time (re-install after a
DST change if the config's timezone isn't this machine's); Orca gets the timezone itself.
"""
from __future__ import annotations

import json
import os
import platform
import plistlib
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import capacity
from .contracts import redact, slugify
from .ui import c

DAYS = capacity.DAYS
DEFAULT_IDLE = {"weekday": "23:00-07:00", "weekend": "all"}     # capacity.py's default downtime
DEFAULT_RESERVE = 0.15
VIAS = ("launchd", "orca", "cron")
ORCA_BIN = "/Applications/Orca.app/Contents/Resources/bin/orca"
ROOT = str(Path(__file__).resolve().parent.parent)              # PYTHONPATH for a scheduled `python -m relay`


# --------------------------------------------------------------------------- time
def tz_name(cfg: dict, *sections: dict | None) -> str:
    """First `timezone` among sections, idle, week; else $TZ; else this machine's zone; else UTC."""
    for s in (*sections, cfg.get("idle"), cfg.get("week")):
        if (s or {}).get("timezone"):
            return s["timezone"]
    link = os.path.realpath("/etc/localtime")
    return os.environ.get("TZ", "").lstrip(":") or (link.split("zoneinfo/", 1)[1] if "zoneinfo/" in link else "UTC")


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def tz(cfg: dict, *sections: dict | None) -> ZoneInfo:
    return zone(tz_name(cfg, *sections))


def at(now=None, where=None) -> datetime:
    """`now` (epoch seconds, datetime or None) as an aware datetime in `where` (default UTC)."""
    where = where or timezone.utc
    if now is None:
        return datetime.now(where)
    if isinstance(now, datetime):
        return (now if now.tzinfo else now.replace(tzinfo=where)).astimezone(where)
    return datetime.fromtimestamp(float(now), where)


def idle(cfg: dict) -> dict:
    """Your downtime in capacity.py's `idle` format, timezone resolved."""
    return {**(cfg.get("idle") or DEFAULT_IDLE), "timezone": tz_name(cfg)}


def _idle_now(cfg: dict, now):
    t = at(now, tz(cfg))
    return capacity.idle_intervals(idle(cfg), t), t


def in_work_hours(cfg: dict, now=None) -> bool:
    """True when `now` is outside your idle hours."""
    wins, t = _idle_now(cfg, now)
    return not (wins and wins[0][0] <= t)


def next_free_time(cfg: dict, now=None) -> datetime | None:
    """When you're next off: `now` if you're idle, else the start of your next idle window (None: none ahead)."""
    wins, t = _idle_now(cfg, now)
    return max(wins[0][0], t) if wins else None


def next_work_start(cfg: dict, now=None) -> datetime:
    """When you next start work: `now` if you're working, else the end of the idle window you're in."""
    wins, t = _idle_now(cfg, now)
    return wins[0][1] if wins and wins[0][0] <= t else t


# --------------------------------------------------------------------------- reserve
def reserve_pct(cfg: dict, subscription: str | None = None) -> float:
    """Share of `subscription` kept for your own use: its reserve_pct, else the top-level one, else 0.15."""
    sub = next((s for s in cfg.get("subscriptions") or [] if s.get("name") == subscription), {})
    v = sub.get("reserve_pct", cfg.get("reserve_pct"))
    if v is None:
        return DEFAULT_RESERVE
    v = float(v)
    return min(1.0, max(0.0, v / 100 if v > 1 else v))


def target_pct(cfg: dict, subscription: str | None = None) -> float:
    return round(1 - reserve_pct(cfg, subscription), 4)


# --------------------------------------------------------------------------- config and ledger
def data_dir(cfg: dict) -> Path:
    return Path(os.path.expanduser(cfg.get("data_dir") or "~/.relay-burn"))


def log_path(cfg: dict, name: str) -> str:
    return str(data_dir(cfg) / "logs" / f"{name}.log")


def load_cfg(path: str | None = None) -> tuple[dict, str]:
    """(config, absolute path) for -b/--burner, default ./burner-week.json; the default model ladder if it has none."""
    from .config import DEFAULT
    p = Path(path or "burner-week.json").expanduser().resolve()
    cfg = json.loads(p.read_text())
    return ({**DEFAULT, **cfg} if "models" not in cfg else cfg), str(p)


def scrub(v):
    """Every string inside a (nested) value, redacted."""
    if isinstance(v, str):
        return redact(v)
    if isinstance(v, dict):
        return {k: scrub(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [scrub(x) for x in v]
    return v


def emit(tel, event: str, **data) -> dict:
    """Log a row through a Telemetry (or Bus) with every string in it redacted."""
    return tel.emit(event, **scrub(data))


def ledger_rows(path: str) -> list[dict]:
    """Rows of a JSONL event ledger; unreadable lines are skipped."""
    try:
        lines = Path(path).read_text(errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


# --------------------------------------------------------------------------- jobs
@dataclass
class Job:
    """A relay command on a schedule. launchd and cron run `argv`; Orca runs `precheck` (exit 0 = go), then one
    agent turn (`provider`, `prompt`)."""
    label: str                                                      # com.relay.<feature>[.<name>]
    argv: list[str]                                                 # arguments after `python -m relay`
    calendar: list[tuple[int, int, int]] = field(default_factory=list)   # (weekday mon=0, hour, minute) in timezone
    interval: int = 3600                                            # seconds, when there is no calendar
    timezone: str = "UTC"
    cwd: str = "."                                                  # relay runs here: its .relay/ ledger is here
    log: str | None = None
    precheck: list[str] | None = None                               # Orca only; default `argv`
    provider: str = "claude"                                        # Orca only
    prompt: str = "Reply with OK."                                  # Orca only: kept as cheap as possible


def label(*parts: str) -> str:
    return ".".join(["com.relay", *(slugify(p) for p in parts if p)])


def default_via() -> str:
    return "launchd" if platform.system() == "Darwin" else "cron"


def command(argv: list[str]) -> list[str]:
    return [sys.executable, "-m", "relay", *argv]


def local_calendar(job: Job, local=None, now=None) -> list[tuple[int, int, int]]:
    """job.calendar on this machine's clock (or `local`), at this week's UTC offsets."""
    where = zone(job.timezone)
    today = at(now, where).date()
    out = set()
    for wd, h, m in job.calendar:
        d = today + timedelta(days=(wd - today.weekday()) % 7)
        t = datetime(d.year, d.month, d.day, h, m, tzinfo=where)
        t = t.astimezone(local) if local else t.astimezone()
        out.add((t.weekday(), t.hour, t.minute))
    return sorted(out)


def _env() -> dict:
    return {"PATH": os.environ.get("PATH") or "/usr/bin:/bin", "PYTHONPATH": ROOT}


def plist(job: Job, local=None, now=None) -> str:
    """The launchd agent for `job` (pure)."""
    d = {"Label": job.label, "ProgramArguments": command(job.argv), "WorkingDirectory": os.path.abspath(job.cwd),
         "EnvironmentVariables": _env(), "RunAtLoad": False}
    if job.calendar:
        d["StartCalendarInterval"] = [{"Weekday": (wd + 1) % 7, "Hour": h, "Minute": m}
                                      for wd, h, m in local_calendar(job, local, now)]
    else:
        d["StartInterval"] = int(job.interval)
    if job.log:
        d["StandardOutPath"] = d["StandardErrorPath"] = job.log
    return plistlib.dumps(d, sort_keys=False).decode()


def _every(seconds: int) -> str:
    m = max(1, int(seconds) // 60)
    return f"*/{m} * * * *" if m < 60 else "0 * * * *" if m == 60 else f"0 */{m // 60} * * *"


def _shell(job: Job, argv: list[str]) -> str:
    env = _env()
    return (f"cd {shlex.quote(os.path.abspath(job.cwd))} && PATH={shlex.quote(env['PATH'])} "
            f"PYTHONPATH={shlex.quote(env['PYTHONPATH'])} exec {shlex.join(command(argv))}")


def cron_lines(job: Job, local=None, now=None) -> list[str]:
    """crontab lines for `job` (pure), tagged `# <label>`."""
    cmd = (_shell(job, job.argv) + (f" >> {shlex.quote(job.log)} 2>&1" if job.log else "")).replace("%", r"\%")
    if not job.calendar:
        return [f"{_every(job.interval)} {cmd}  # {job.label}"]
    times: dict[tuple[int, int], list[int]] = {}
    for wd, h, m in local_calendar(job, local, now):
        times.setdefault((h, m), []).append((wd + 1) % 7)
    return [f"{m} {h} * * {','.join(map(str, sorted(ds)))} {cmd}  # {job.label}"
            for (h, m), ds in sorted(times.items())]


def orca_bin() -> str:
    return os.environ.get("ORCA_BIN") or ORCA_BIN


def _trigger(job: Job) -> str:
    if not job.calendar:
        return "hourly" if int(job.interval) == 3600 else _every(job.interval)
    times = sorted({(h, m) for _, h, m in job.calendar})
    if len(times) > 1:
        raise ValueError(f"{job.label}: an Orca automation runs at one time of day, not {len(times)}")
    (h, m), days = times[0], sorted({(wd + 1) % 7 for wd, _, _ in job.calendar})
    return f"{m} {h} * * {'*' if len(days) == 7 else ','.join(map(str, days))}"


def orca_argv(job: Job) -> list[str]:
    """`orca automations create` for `job` (pure). Orca keeps the timezone, so the calendar isn't converted."""
    pre = shlex.join(["/bin/sh", "-c", _shell(job, job.precheck or job.argv)])
    return [orca_bin(), "automations", "create", "--name", job.label, "--trigger", _trigger(job),
            "--timezone", job.timezone, "--provider", job.provider, "--prompt", job.prompt, "--precheck", pre,
            "--repo", f"path:{os.path.abspath(job.cwd)}", "--enabled", "--json"]


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(argv, 127, "", str(e))


def _said(r: subprocess.CompletedProcess) -> str:
    return redact((r.stderr or r.stdout or "").strip())[-300:]


def _orca_ids(name: str) -> list[str]:
    """Ids of the Orca automations named `name` (read-only `automations list --json`)."""
    try:
        data = json.loads(_run([orca_bin(), "automations", "list", "--json"]).stdout or "[]")
    except ValueError:
        return []
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [])
    return [str(a["id"]) for a in data if isinstance(a, dict) and a.get("name") == name and a.get("id")]


def agents_dir(home=None) -> Path:
    return Path(home or Path.home()) / "Library" / "LaunchAgents"


def install(job: Job, via: str | None = None, dry_run: bool = False, home=None, local=None) -> tuple[bool, str]:
    """Install `job`; (ok, what happened). launchd writes ~/Library/LaunchAgents and runs launchctl, orca runs
    `orca automations create` (replacing one of the same name), cron only prints. dry_run changes nothing."""
    via = via or default_via()
    if via == "cron":
        return True, "add with `crontab -e` (relay never edits your crontab):\n" + "\n".join(cron_lines(job, local))
    if via == "orca":
        argv = orca_argv(job)
        if dry_run:
            return True, f"would replace any Orca automation named {job.label}, then run:\n  {shlex.join(argv)}"
        for i in _orca_ids(job.label):
            _run([orca_bin(), "automations", "remove", "--id", i, "--json"])
        r = _run(argv)
        return r.returncode == 0, (f"Orca automation {job.label} created" if r.returncode == 0
                                   else f"orca automations create failed: {_said(r)}")
    path, domain = agents_dir(home) / f"{job.label}.plist", f"gui/{os.getuid()}"
    text = plist(job, local)
    if dry_run:
        return True, f"would write {path}:\n{text}then run: launchctl bootstrap {domain} {path}"
    path.parent.mkdir(parents=True, exist_ok=True)
    if job.log:
        Path(job.log).parent.mkdir(parents=True, exist_ok=True)
    _run(["launchctl", "bootout", f"{domain}/{job.label}"])        # an older copy; fails harmlessly when absent
    path.write_text(text)
    r = _run(["launchctl", "bootstrap", domain, str(path)])
    return r.returncode == 0, (f"installed {job.label} ({path})" if r.returncode == 0
                               else f"launchctl bootstrap failed: {_said(r)}")


def uninstall(name: str, via: str | None = None, dry_run: bool = False, home=None) -> tuple[bool, str]:
    """Remove the job labelled `name`. dry_run changes nothing."""
    via = via or default_via()
    if via == "cron":
        return True, f"remove the crontab line ending in `# {name}` with `crontab -e`"
    if via == "orca":
        cmds = [[orca_bin(), "automations", "remove", "--id", i, "--json"] for i in _orca_ids(name)]
        if not cmds or dry_run:
            return True, ("would run:\n" + "\n".join("  " + shlex.join(x) for x in cmds)) if cmds \
                else f"no Orca automation named {name}"
        bad = [r for r in map(_run, cmds) if r.returncode]
        return not bad, (f"removed Orca automation {name}" if not bad
                         else f"orca automations remove failed: {_said(bad[0])}")
    path, domain = agents_dir(home) / f"{name}.plist", f"gui/{os.getuid()}"
    if dry_run:
        return True, f"would run: launchctl bootout {domain}/{name}, then delete {path}"
    _run(["launchctl", "bootout", f"{domain}/{name}"])
    if not path.exists():
        return True, f"{name} was not installed"
    path.unlink()
    return True, f"uninstalled {name}"


def status(name: str, via: str | None = None, home=None) -> dict:
    """{label, via, installed, ...}: read-only."""
    via = via or default_via()
    if via == "orca":
        ids = _orca_ids(name)
        return {"label": name, "via": via, "installed": bool(ids), "ids": ids}
    if via == "cron":
        lines = _run(["crontab", "-l"]).stdout.splitlines()
        return {"label": name, "via": via, "installed": any(l.rstrip().endswith(f"# {name}") for l in lines)}
    path = agents_dir(home) / f"{name}.plist"
    loaded = path.exists() and _run(["launchctl", "print", f"gui/{os.getuid()}/{name}"]).returncode == 0
    return {"label": name, "via": via, "installed": path.exists(), "loaded": loaded, "path": str(path)}


def status_line(st: dict) -> str:
    on = st["installed"]
    loaded = "" if st["via"] != "launchd" or not on else " · loaded" if st.get("loaded") else c("33", " · not loaded")
    return f"{st['label']:<36}{st['via']:<9}" + (c("32", "installed") if on else c("2", "not installed")) + loaded


# --------------------------------------------------------------------------- report and CLI
def report(cfg: dict, now=None, home=None) -> str:
    """Where you are in your week: working or off, the reserve per subscription, relay's launchd jobs."""
    where = tz(cfg)
    t = at(now, where)
    free, back = next_free_time(cfg, t), next_work_start(cfg, t)
    hm = lambda d: d.strftime("%a %H:%M") if d else "not this week"     # noqa: E731
    lines = [c("1", f"schedule · {where.key} · {t:%a %b %d %H:%M}"),
             "  " + (c("33", f"working · off from {hm(free)}") if in_work_hours(cfg, t)
                     else c("32", f"off · work starts {hm(back)}")),
             c("1", "reserve") + c("2", "  kept for your own use; burns aim for 1 - reserve")]
    for s in cfg.get("subscriptions") or []:
        n = s.get("name", "?")
        lines.append(f"  {n[:15]:<16}{reserve_pct(cfg, n):>5.0%}  → target {target_pct(cfg, n):.0%}")
    jobs = sorted(p.stem for p in agents_dir(home).glob("com.relay.*.plist"))
    lines.append(c("1", "launchd jobs"))
    lines += [f"  {j}" for j in jobs] or [c("2", "  none (relay windows install · relay burn auto --install)")]
    return "\n".join(lines)


def add_cli(subparsers):
    """`relay schedule [-b FILE]`: your work hours, reserve and installed jobs."""
    p = subparsers.add_parser("schedule", help="your work hours, reserve per subscription, relay's scheduled jobs")
    p.add_argument("-b", "--burner", help="burn-week config (default: ./burner-week.json)")
    p.set_defaults(func=run_cli)
    return p


def run_cli(args) -> int:
    try:
        cfg, _ = load_cfg(getattr(args, "burner", None))
    except (OSError, ValueError) as e:
        print(c("1;31", f"✗ config: {e}"))
        return 2
    print(report(cfg))
    return 0
