"""Subscription capacity: how much paid-for capacity will expire unused, and when you are not using it.

Three kinds of subscription:

  agent37_budget   Agent37 instance budget. Read live from GET /v1/instances/{id}/budget when
                   AGENT37_API_KEY is set, else from `monthly_usd` + the local ledger. Resets each UTC month.
  api_budget       Any metered API you cap yourself (OpenAI, OpenRouter...): `monthly_usd`, `reset_day`.
                   Spend comes from relay's own ledger.
  rolling_window   Coding plans with usage windows (Claude Max/Pro, ChatGPT Pro/Codex): `window_hours`,
                   `windows_per_week`, `weekly_reset`. These plans have no usage API, so you describe them;
                   relay counts every window that falls inside your idle hours as capacity you'd otherwise waste.

Idle hours ("downtime") come from `idle` in burner.json:
  {"timezone": "America/Los_Angeles", "weekday": "23:00-07:00", "weekend": "all"}
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# --------------------------------------------------------------------------- idle windows
def _tz(cfg: dict):
    name = cfg.get("timezone") or os.environ.get("TZ") or "UTC"
    try:
        return ZoneInfo(name) if ZoneInfo else timezone.utc
    except Exception:
        return timezone.utc


def _span(spec: str) -> tuple[int, int] | None:
    """'23:00-07:00' -> minutes (1380, 420); 'all' -> (0, 1440); 'none' -> None."""
    spec = (spec or "none").strip().lower()
    if spec == "none":
        return None
    if spec == "all":
        return 0, 1440
    a, b = spec.split("-")
    to_min = lambda s: int(s.split(":")[0]) * 60 + int(s.split(":")[1])  # noqa: E731
    return to_min(a), to_min(b) if b != "24:00" else 1440


def idle_intervals(cfg: dict, now: datetime | None = None, days: int = 8) -> list[tuple[datetime, datetime]]:
    """Merged idle intervals from yesterday through `days` ahead, in the configured timezone."""
    tz = _tz(cfg)
    now = (now or datetime.now(tz)).astimezone(tz)
    raw = []
    for d in range(-1, days):
        day = (now + timedelta(days=d)).replace(hour=0, minute=0, second=0, microsecond=0)
        key = DAYS[day.weekday()]
        spec = cfg.get(key) or (cfg.get("weekend") if day.weekday() >= 5 else cfg.get("weekday"))
        span = _span(spec or "none")
        if not span:
            continue
        a, b = span
        start = day + timedelta(minutes=a)
        end = day + timedelta(minutes=b) if b > a else day + timedelta(days=1, minutes=b)
        raw.append((start, end))
        if b < a and day.weekday() < 5:
            # an overnight span also covers this morning (Monday's 00:00-07:00 after an all-day Sunday)
            raw.append((day, day + timedelta(minutes=b)))
    raw.sort()
    merged: list[tuple[datetime, datetime]] = []
    for s, e in raw:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(e, merged[-1][1]))
        else:
            merged.append((s, e))
    return [(s, e) for s, e in merged if e > now]


def current_or_next_window(cfg: dict, now: datetime | None = None) -> tuple[datetime, datetime, bool]:
    tz = _tz(cfg)
    now = (now or datetime.now(tz)).astimezone(tz)
    wins = idle_intervals(cfg, now)
    if not wins:
        return now, now, False
    s, e = wins[0]
    return s, e, s <= now < e


# --------------------------------------------------------------------------- subscriptions
@dataclass
class Capacity:
    name: str
    kind: str
    providers: list[str]
    unit: str                    # "usd" or "windows"
    remaining: float
    resets_at: datetime
    tonight: float               # allowance for the current/next idle window
    expiring: float              # what will likely go unused at reset at your normal pace
    source: str                  # "live", "config", "ledger"
    note: str = ""

    @property
    def hours_to_reset(self) -> float:
        return max(0.0, (self.resets_at - datetime.now(self.resets_at.tzinfo)).total_seconds() / 3600)

    @property
    def urgency(self) -> tuple[float, float]:
        """Burn order: the subscription most likely to go to waste first, then the one that resets soonest."""
        return (self.expiring / self.remaining if self.remaining else 0.0, -self.hours_to_reset)


def _ledger_spend(providers: list[str], since: datetime, ledger: Path) -> float:
    if not ledger.exists():
        return 0.0
    total, t0 = 0.0, since.timestamp()
    for line in ledger.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("event") == "call" and r.get("ts", 0) >= t0 and r.get("provider") in providers:
            total += float(r.get("usd") or 0)
    return total


def _month_bounds(now: datetime, reset_day: int = 1) -> tuple[datetime, datetime]:
    start = now.replace(day=min(reset_day, 28), hour=0, minute=0, second=0, microsecond=0)
    if start > now:
        start = (start - timedelta(days=28)).replace(day=min(reset_day, 28))
    nxt = (start + timedelta(days=32)).replace(day=min(reset_day, 28))
    return start, nxt


def _agent37_live(instance: str) -> dict | None:
    key = os.environ.get("AGENT37_API_KEY")
    if not (key and instance):
        return None
    req = urllib.request.Request(f"https://api.agent37.com/v1/instances/{instance}/budget")
    req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _nights_until(cfg_idle: dict, until: datetime, now: datetime) -> int:
    return max(1, sum(1 for s, _ in idle_intervals(cfg_idle, now, days=40) if s < until))


def assess(cfg: dict, ledger: str = ".relay/events.jsonl", now: datetime | None = None) -> list[Capacity]:
    idle = cfg.get("idle", {"weekday": "23:00-07:00", "weekend": "all"})
    tz = _tz(idle)
    now = (now or datetime.now(tz)).astimezone(tz)
    w_start, w_end, _ = current_or_next_window(idle, now)
    window_h = max(0.0, (w_end - max(w_start, now)).total_seconds() / 3600)
    out: list[Capacity] = []
    for sub in cfg.get("subscriptions", []):
        kind, name = sub["kind"], sub["name"]
        providers = sub.get("providers", [name])
        reserve = float(sub.get("reserve_pct", 30)) / 100   # keep this much for your own daytime use
        if kind in ("agent37_budget", "api_budget"):
            utc_now = now.astimezone(timezone.utc)
            m_start, m_end = _month_bounds(utc_now, int(sub.get("reset_day", 1)))
            source, note = "config", ""
            live = _agent37_live(os.path.expandvars(sub.get("instance", ""))) if kind == "agent37_budget" else None
            if live:
                remaining = (live.get("monthly_remaining_micros", 0) + live.get("credit_remaining_micros", 0)) / 1e6
                source = "live"
            else:
                spent = _ledger_spend(providers, m_start, Path(ledger))
                remaining = max(0.0, float(sub.get("monthly_usd", 0)) - spent)
                source = "ledger" if spent else "config"
                note = f"${spent:.2f} spent this period"
            nights = _nights_until(idle, m_end.astimezone(tz), now)
            burnable = remaining * (1 - reserve)
            # at your normal daytime pace, how much of this will still be unused at reset?
            pace = float(sub.get("daytime_usd_per_day", 0))
            days_left = max(0.0, (m_end - utc_now).total_seconds() / 86400)
            expiring = max(0.0, remaining - pace * days_left)
            tonight = burnable / nights                      # spread what you won't use over the idle nights left
            out.append(Capacity(name, kind, providers, "usd", remaining, m_end.astimezone(tz),
                                round(tonight, 4), round(expiring, 4), source, note))
        elif kind == "rolling_window":
            wh = float(sub.get("window_hours", 5))
            per_week = float(sub.get("windows_per_week", 20))
            day, hm = (sub.get("weekly_reset", "mon 00:00").split() + ["00:00"])[:2]
            reset = now.replace(hour=int(hm[:2]), minute=int(hm[3:5]), second=0, microsecond=0)
            reset += timedelta(days=(DAYS.index(day[:3].lower()) - now.weekday()) % 7)
            if reset <= now:
                reset += timedelta(days=7)
            used = float(sub.get("windows_used_this_week", 0))
            remaining = max(0.0, per_week - used)
            idle_h_before_reset = sum(
                (min(e, reset) - max(s, now)).total_seconds() / 3600
                for s, e in idle_intervals(idle, now, days=8) if s < reset)
            idle_windows = int(idle_h_before_reset // wh)
            tonight = min(remaining, max(1, int(window_h // wh)) if window_h else 0)
            expiring = min(remaining * (1 - reserve), idle_windows)
            out.append(Capacity(name, kind, providers, "windows", remaining, reset, float(tonight),
                                float(expiring), "config",
                                f"{idle_windows} x {wh:g}h windows fall in your idle time before reset"))
    out.sort(key=lambda c: c.urgency, reverse=True)
    return out


def report(caps: list[Capacity], cfg: dict, now: datetime | None = None) -> str:
    idle = cfg.get("idle", {})
    s, e, active = current_or_next_window(idle, now)
    when = "now" if active else s.strftime("%a %H:%M")
    lines = [f"downtime window: {when} -> {e.strftime('%a %H:%M')} ({_tz(idle)})", "",
             f"{'subscription':<16}{'kind':<16}{'left':>10}{'resets':>14}{'tonight':>10}{'expiring':>10}  burn order"]
    for i, c in enumerate(caps, 1):
        fmt = (lambda v: f"${v:,.2f}") if c.unit == "usd" else (lambda v: f"{v:g} win")
        lines.append(f"{c.name:<16}{c.kind:<16}{fmt(c.remaining):>10}{c.resets_at.strftime('%a %d %H:%M'):>14}"
                     f"{fmt(c.tonight):>10}{fmt(c.expiring):>10}  #{i} ({c.source}{'; ' + c.note if c.note else ''})")
    return "\n".join(lines)
