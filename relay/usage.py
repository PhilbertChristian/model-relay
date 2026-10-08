"""Subscription usage for burn-week: how much of each subscription this window has used, and when it resets.

  claude_code     tokens in Claude Code transcripts (~/.claude/projects/**/*.jsonl) since the weekly reset
  codex           tokens in Codex sessions (~/.codex/sessions/**/*.jsonl) since the weekly reset
  agent37_budget  Agent37 instance budget, live via relay.capacity; else monthly_usd minus the ledger
  api_budget      monthly_usd minus this month's spend by the subscription's lanes in .relay/events.jsonl
  monid_credits   relay.monid.credits(), else the configured `credits`
  demo            {"used", "limit", "unit", "resets_in_hours"} as given

Weekly kinds reset at week.reset (or the subscription's own "reset") in week.timezone; monthly kinds on
reset_day (default 1) at 00:00 UTC. Transcripts are read locally for their usage counters only: no message
text is kept, returned or sent anywhere.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from .contracts import Usage, is_secret_file, redact
from .ui import c

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
LANES = {"claude_code": "claude", "codex": "codex", "agent37_budget": "agent37", "api_budget": "relay",
         "monid_credits": "monid", "demo": "mock"}
TOKEN_KEYS = ("input", "output", "cache_read", "cache_creation")
_CLAUDE_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
_RESET_RE = re.compile(r"^\s*([a-z]{3})[a-z]*\.?\s*(?:(\d{1,2})(?::(\d\d))?\s*([ap]m)?)?\s*$", re.I)


# --------------------------------------------------------------------------- windows
def _now(now=None) -> float:
    return time.time() if now is None else now.timestamp() if isinstance(now, datetime) else float(now)


def _zone(name: str | tzinfo | None = None) -> tzinfo:
    """IANA zone by name, else $TZ, else UTC."""
    if isinstance(name, tzinfo):
        return name
    for n in (name, os.environ.get("TZ"), "UTC"):
        if n:
            try:
                return ZoneInfo(n.lstrip(":"))
            except Exception:
                continue
    return timezone.utc


def _parse_reset(reset: str) -> tuple[int, int, int]:
    """'mon 09:00' / 'Monday 9am' / 'sun 21:30' / 'fri' -> (weekday, hour, minute)."""
    m = _RESET_RE.match(reset or "")
    if not m or m[1].lower() not in DAYS:
        raise ValueError(f"weekly reset {reset!r}: expected e.g. 'mon 09:00'")
    h, mi = int(m[2] or 0), int(m[3] or 0)
    if m[4]:
        h = h % 12 + (12 if m[4].lower() == "pm" else 0)
    if h > 23 or mi > 59:
        raise ValueError(f"weekly reset {reset!r}: bad time of day")
    return DAYS.index(m[1].lower()), h, mi


def week_bounds(reset: str = "mon 09:00", tz: str | None = None, now: float | None = None) -> tuple[float, float]:
    """(start, end) epoch seconds of the weekly window holding `now`. The reset is wall-clock time in `tz`, so a
    week across a DST change lasts 167 or 169 hours; the reset instant itself opens the new window."""
    day, hh, mm = _parse_reset(reset)
    z, now = _zone(tz), _now(now)

    def at(d) -> float:
        return datetime(d.year, d.month, d.day, hh, mm, tzinfo=z).timestamp()

    d = datetime.fromtimestamp(now, z).date()
    d -= timedelta(days=(d.weekday() - day) % 7)
    if at(d) > now:
        d -= timedelta(days=7)
    return at(d), at(d + timedelta(days=7))


def _month_bounds(now: float, day: int = 1) -> tuple[float, float]:
    """(start, end) of the monthly window holding `now`, resetting on `day` (1-28) at 00:00 UTC."""
    d, day = datetime.fromtimestamp(now, timezone.utc), max(1, min(int(day or 1), 28))
    y, m = (d.year, d.month) if d.day >= day else (d.year - (d.month == 1), (d.month - 2) % 12 + 1)
    return (datetime(y, m, day, tzinfo=timezone.utc).timestamp(),
            datetime(y + (m == 12), m % 12 + 1, day, tzinfo=timezone.utc).timestamp())


# --------------------------------------------------------------------------- transcripts
def _ts(v, default: float = 0.0) -> float:
    """Epoch seconds from an ISO-8601 string or epoch s/ms; `default` when unreadable."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v / 1000 if v > 1e11 else float(v)
    if not isinstance(v, str) or not v.strip():
        return default
    s = re.sub(r"\.(\d{1,6})\d*", lambda m: "." + m[1].ljust(6, "0"), v.strip().replace("Z", "+00:00"))
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return default
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def _int(v) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def _num(v) -> float | None:
    return None if v is None or v == "" else float(v)


def _files(root: str, since: float) -> list[tuple[float, Path]]:
    """(mtime, path) of *.jsonl under root, oldest first; files untouched since `since` are skipped unread."""
    base, out = Path(os.path.expanduser(root)), []
    if base.is_dir():
        for f in base.rglob("*.jsonl"):
            try:
                mt = f.stat().st_mtime
            except OSError:
                continue
            if mt >= since and f.is_file() and not is_secret_file(str(f)):
                out.append((mt, f))
    return sorted(out)


def _rows(path: Path, needles: tuple[str, ...]):
    """JSON objects from the lines that contain a needle; unreadable lines are skipped."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if any(n in line for n in needles):
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(r, dict):
                        yield r
    except OSError:
        return


def _totals(rows) -> dict:
    """[model, session, input, output, cache_read, cache_creation] rows -> the usage dict."""
    out = dict.fromkeys(TOKEN_KEYS, 0)
    by_model: dict[str, int] = defaultdict(int)
    sessions = set()
    for model, session, *vals in rows:
        if any(vals):
            for k, v in zip(TOKEN_KEYS, vals):
                out[k] += v
            by_model[model] += sum(vals)
            sessions.add(session)
    return {**out, "total": sum(out.values()), "by_model": dict(by_model), "sessions": len(sessions)}


def claude_code_tokens(since: float, root: str = "~/.claude/projects") -> dict:
    """Tokens in Claude Code assistant rows newer than `since`. A response is repeated by streaming (one row per
    content block) and by resumed sessions, so rows dedupe by message.id, else requestId, keeping each field's max."""
    seen: dict[str, list] = {}
    for mtime, f in _files(root, since):
        for i, r in enumerate(_rows(f, ('"usage"',))):
            msg = r.get("message")
            if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                continue
            if r.get("type", "assistant") != "assistant" and msg.get("role") != "assistant":
                continue
            if _ts(r.get("timestamp"), mtime) < since:
                continue
            vals = [_int(msg["usage"].get(k)) for k in _CLAUDE_FIELDS]
            key = str(msg.get("id") or r.get("requestId") or f"{f}:{i}")
            if key in seen:
                seen[key][2:] = map(max, seen[key][2:], vals)
            else:
                seen[key] = [str(msg.get("model") or "unknown"), str(r.get("sessionId") or f.stem), *vals]
    return _totals(seen.values())


def _codex_usage(u) -> tuple[int, int, int] | None:
    """(input incl. cached, cached, output) from a Codex/OpenAI usage object."""
    if not isinstance(u, dict) or not ("input_tokens" in u or "output_tokens" in u):
        return None
    cached = u.get("cached_input_tokens") or u.get("cache_read_input_tokens")
    return _int(u.get("input_tokens")), _int(cached), _int(u.get("output_tokens"))


def codex_tokens(since: float, root: str = "~/.codex/sessions") -> dict:
    """Tokens in Codex sessions newer than `since`, from token_count events in any schema seen so far
    (info.total_token_usage, info.last_token_usage, flat counts, a plain usage object). Cumulative totals are
    differenced per session (across files, for resumed sessions), so repeated events add nothing and a session
    spanning the reset splits correctly."""
    rows, seen, prevs = [], set(), {}
    for mtime, f in _files(root, since):
        session, model = f.stem, "unknown"
        for r in _rows(f, ("token_count", '"usage"', "session_meta", "turn_context")):
            p = r["payload"] if isinstance(r.get("payload"), dict) else r
            kind = p.get("type") or r.get("type")
            if r.get("type") == "session_meta" and p.get("id"):
                session = str(p["id"])
            if isinstance(p.get("model"), str):
                model = p["model"]
            info = p["info"] if isinstance(p.get("info"), dict) else p
            total = _codex_usage(info.get("total_token_usage"))
            last = (_codex_usage(info.get("last_token_usage")) or _codex_usage(p.get("usage"))
                    or (_codex_usage(p) if kind == "token_count" else None))
            ts = _ts(r.get("timestamp") or p.get("timestamp"), mtime)
            if total:
                prev = prevs.get(session)
                grew = prev is not None and all(t >= q for t, q in zip(total, prev))
                delta = tuple(t - q for t, q in zip(total, prev)) if grew else total
                prevs[session], key = total, (session, total)
            elif last:
                delta, key = last, (session, ts, last)
            else:
                continue
            if ts >= since and key not in seen:
                i, cached, o = delta
                rows.append([model, session, i - cached if i >= cached else i, o, cached, 0])
            seen.add(key)
    return _totals(rows)


# --------------------------------------------------------------------------- ledger
def _ledger_usd(path: str, lanes: tuple, since: float, providers: tuple = ()) -> float:
    """USD the given lanes spent since `since` per the event ledger. agent_progress/agent_end usd is cumulative
    per task attempt; relay `call` rows for the subscription's providers in the same session describe the same
    spend, so each session counts the larger of the two."""
    agents: dict[str, float] = defaultdict(float)
    calls: dict[str, float] = defaultdict(float)
    running: dict[tuple, float] = {}
    for r in _rows(Path(os.path.expanduser(path)), ('"usd"',)):
        if _ts(r.get("ts")) < since:
            continue
        s, ev = str(r.get("session")), r.get("event")
        try:
            usd = max(0.0, float(r.get("usd") or 0))
        except (TypeError, ValueError):
            continue
        if ev == "call" and r.get("provider") in providers:
            calls[s] += usd
        elif ev in ("agent_progress", "agent_end") and r.get("lane") in lanes:
            k = (s, str(r.get("task_id")), str(r.get("agent")))
            if ev == "agent_end":
                agents[s] += max(running.pop(k, 0.0), usd)
            else:
                running[k] = max(running.get(k, 0.0), usd)
    for (s, *_), usd in running.items():
        agents[s] += usd
    return round(sum(max(agents[s], calls[s]) for s in set(agents) | set(calls)), 6)


# --------------------------------------------------------------------------- subscriptions
def _default_root(claude: bool) -> str:
    env = os.environ.get("CLAUDE_CONFIG_DIR" if claude else "CODEX_HOME")
    if env:
        return os.path.join(env, "projects" if claude else "sessions")
    return "~/.claude/projects" if claude else "~/.codex/sessions"


def _monid_credits() -> Usage | None:
    try:
        from .monid import credits
        u = credits()
    except Exception:
        return None
    return u if isinstance(u, Usage) else None


def _usage(cfg: dict, sub: dict, ledger: str, now: float) -> Usage:
    kind = sub.get("kind", "")
    name, lane = str(sub.get("name") or kind), sub.get("lane") or LANES.get(kind, kind)
    if kind == "demo":
        return Usage(name, lane, sub.get("unit", "tokens"), float(sub.get("used") or 0), _num(sub.get("limit")),
                     now + float(sub.get("resets_in_hours") or 0) * 3600, "demo", sub.get("note", ""))
    week = cfg.get("week") or {}
    w0, w1 = week_bounds(sub.get("reset") or week.get("reset") or "mon 09:00",
                         sub.get("timezone") or week.get("timezone"), now)
    if kind in ("claude_code", "codex"):
        claude = kind == "claude_code"
        root, limit = sub.get("root") or _default_root(claude), _num(sub.get("weekly_tokens"))
        if not Path(os.path.expanduser(root)).is_dir():
            return Usage(name, lane, "tokens", 0.0, limit, w1, "config", f"no transcripts at {root}")
        t = (claude_code_tokens if claude else codex_tokens)(w0, root)
        top = max(t["by_model"], key=t["by_model"].get, default="")
        n = t["sessions"]
        note = f"{n} session{'' if n == 1 else 's'} this week" + (f", mostly {top}" if top else "")
        return Usage(name, lane, "tokens", float(t["total"]), limit, w1, "transcripts", redact(note))
    m0, m1 = _month_bounds(now, sub.get("reset_day", 1))
    if kind == "agent37_budget":
        from .capacity import _agent37_live
        inst = os.path.expandvars(str(sub.get("instance") or ""))
        live = _agent37_live("" if "$" in inst else inst)
        if live:
            left = (_int(live.get("monthly_remaining_micros")) + _int(live.get("credit_remaining_micros"))) / 1e6
            limit = max(float(sub.get("monthly_usd") or 0), left)
            return Usage(name, lane, "usd", limit - left, limit, m1, "live", f"${left:,.2f} left on the instance")
    if kind in ("agent37_budget", "api_budget"):
        lanes = tuple({lane} | {ln.get("name") or ln.get("kind") for ln in cfg.get("lanes") or []
                                if ln.get("subscription") == name})
        spent = _ledger_usd(ledger, lanes, m0, tuple(sub.get("providers") or [name]))
        return Usage(name, lane, "usd", spent, float(sub.get("monthly_usd") or 0), m1, "ledger",
                     f"${spent:,.2f} spent this month")
    if kind == "monid_credits":
        resets, u = (m1 if "reset_day" in sub else w1), _monid_credits()
        if u is not None:
            return replace(u, name=name, lane=lane, resets_at=u.resets_at or resets)
        return Usage(name, lane, "credits", float(sub.get("used") or 0), _num(sub.get("credits")), resets, "config")
    return Usage(name, lane, sub.get("unit", "tokens"), float(sub.get("used") or 0), _num(sub.get("limit")), w1,
                 "config", f"unknown kind {kind!r}")


def weekly_usage(cfg: dict, ledger: str = ".relay/events.jsonl", now: float | None = None) -> list[Usage]:
    """One Usage per cfg["subscriptions"] entry. One that can't be read comes back already reset (resets_at=now)
    with the error in its note, so pacing never launches on it and the others still show."""
    now, out = _now(now), []
    for sub in cfg.get("subscriptions") or []:
        try:
            out.append(_usage(cfg, sub, ledger, now))
        except Exception as e:
            kind = sub.get("kind", "")
            out.append(Usage(str(sub.get("name") or kind or "?"), sub.get("lane") or LANES.get(kind, kind or "?"),
                             sub.get("unit", "tokens"), 0.0, None, now, "config", redact(f"error: {e}")[:200]))
    return out


# --------------------------------------------------------------------------- display
def human(v: float | None, unit: str = "tokens") -> str:
    """400.0M (tokens), $12.40 (usd), 1,000 (credits); '—' when unknown."""
    if v is None:
        return "—"
    if unit == "usd":
        return f"${v:,.2f}"
    if unit == "tokens":
        for s, d in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
            if abs(v) >= d:
                return f"{v / d:.1f}{s}"
    return f"{v:,.0f}"


def span(hours: float) -> str:
    """45m, 61h, 12d; 'now' once past."""
    if hours <= 0:
        return "now"
    return f"{hours * 60:.0f}m" if hours < 1 else f"{hours:.0f}h" if hours < 100 else f"{hours / 24:.0f}d"


def bar(pct: float | None, width: int = 16) -> str:
    """█░ bar colored by how full it is: yellow (lots left to burn), cyan, green near target, red at the limit."""
    if pct is None:
        return c("2", "·" * width)
    n = round(width * min(1.0, max(0.0, pct)))
    color = "31" if pct >= 1 else "32" if pct >= 0.9 else "36" if pct >= 0.5 else "33"
    return c(color, "█" * n) + c("2", "░" * (width - n))


def report(usages: list[Usage], now: float | None = None) -> str:
    """Compact colored table: used / limit with a % bar, time to reset, and where each number came from."""
    if not usages:
        return "no subscriptions configured"
    now = _now(now)
    lines = [c("1", f"{'subscription':<14}{'lane':<9}{'used':>9} / {'limit':<9}  {'':<16}{'%':>5}{'reset':>7}  source")]
    for u in usages:
        pct = u.pct
        shown = "—" if pct is None else f"{pct:.0%}"
        lines.append(f"{u.name[:13]:<14}{u.lane[:8]:<9}{human(u.used, u.unit):>9} / {human(u.limit, u.unit):<9}  "
                     f"{bar(pct)}{shown:>5}{span((u.resets_at - now) / 3600):>7}  "
                     + c("2", u.source + (f" · {u.note}" if u.note else "")))
    return redact("\n".join(lines))
