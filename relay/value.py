"""What your AI usage is worth at API rates, and how much of it Relay rescued from expiring.

Ported from Max the Golden Token Retrieval (pricing table + Claude Code log reader), same rules:
local-first, read-only, and conservative. Every number comes from a log; nothing is estimated
without saying so, and derived numbers err *against* the flattering direction.

  used      API-rate value of every token you used this month:
              Claude Code logs (~/.claude/projects/**/*.jsonl, deduped by message/request id)
            + Relay's own model calls (.relay/events.jsonl, priced when they were made)
  rescued   API-rate value of night-shift work that drew on capacity that expires: plan windows
            (Claude / ChatGPT) and monthly credit, capped per night at that night's allowance.
            Pay-as-you-go API budgets are NOT rescue (that is new money) unless marked "expires": true.
            "used" is value at API rates, not a bill: flat-rate plans cost the same either way.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# API list prices, USD per million tokens: (prefix, input, output, cache_read). Cache writes bill 1.25x input.
# Longest prefix wins, after normalizing provider-prefixed ids ("anthropic/claude-opus-5-5" -> "claude-opus-5-5").
# Unknown models are never guessed: they are reported as unpriced and counted at $0 (the conservative direction).
RATES_AS_OF = "2026-09-25"
API_PRICES = [
    ("claude-fable-5-1", 10.0, 50.0, 0.25),
    ("claude-mythos-5-1", 10.0, 50.0, 0.25),
    ("claude-fable-5", 10.0, 50.0, 1.00),
    ("claude-mythos", 10.0, 50.0, 1.00),
    ("claude-opus-5-5", 4.0, 20.0, 0.20),
    ("claude-opus-5", 5.0, 25.0, 0.50),
    ("claude-opus-4-1", 15.0, 75.0, 1.50),
    ("claude-opus-4", 5.0, 25.0, 0.50),
    ("claude-sonnet-5", 2.0, 10.0, 0.20),
    ("claude-sonnet-4", 3.0, 15.0, 0.30),
    ("claude-haiku-4-5", 1.0, 5.0, 0.10),
]
CACHE_WRITE_MULT = 1.25
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
_OVERRIDE = Path.home() / ".config" / "relay" / "pricing.json"
_TABLE: list | None = None


def _table() -> list[tuple[str, float, float, float]]:
    """Built-in rows plus ~/.config/relay/pricing.json ({prefix: {input, output, cache_read?}}), loaded once."""
    global _TABLE
    if _TABLE is None:
        rows = {r[0]: r for r in API_PRICES}
        if _OVERRIDE.is_file():
            try:
                for k, v in json.loads(_OVERRIDE.read_text()).items():
                    rows[k] = (k, float(v["input"]), float(v["output"]), float(v.get("cache_read", float(v["input"]) * 0.1)))
            except (ValueError, KeyError, TypeError) as e:
                import sys
                print(f"relay: ignoring {_OVERRIDE}: {e}", file=sys.stderr)
        _TABLE = sorted(rows.values(), key=lambda r: -len(r[0]))   # longest prefix first
    return _TABLE


def normalize(model: str) -> str:
    m = (model or "").strip().lower()
    for pre in ("anthropic/", "openai/", "us.anthropic.", "eu.anthropic.", "anthropic.", "agent37/"):
        if m.startswith(pre):
            m = m[len(pre):]
    return m.split("[")[0].split("@")[0]


def lookup(model: str) -> tuple[float, float, float] | None:
    m = normalize(model)
    for prefix, i, o, cr in _table():
        if m.startswith(prefix):
            return i, o, cr
    return None


def price(model: str, usage: dict) -> float | None:
    rates = lookup(model)
    if rates is None:
        return None
    i, o, cr = rates
    return (int(usage.get("input_tokens") or 0) * i
            + int(usage.get("output_tokens") or 0) * o
            + int(usage.get("cache_creation_input_tokens") or 0) * i * CACHE_WRITE_MULT
            + int(usage.get("cache_read_input_tokens") or 0) * cr) / 1e6


# ------------------------------------------------------------------ Claude Code logs (from Max)
def claude_log_dirs(override: str | None = None) -> list[Path]:
    if override:
        return [Path(p).expanduser() for p in override.split(",") if Path(p).expanduser().is_dir()]
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    cands = [Path(p.strip()) / "projects" for p in env.split(",")] if env else [
        Path.home() / ".config" / "claude" / "projects", Path.home() / ".claude" / "projects"]
    return [d for d in cands if d.is_dir()]


def iter_claude_events(dirs: list[Path]):
    """Yield (utc_datetime, usage, model) per API response, deduped by (message id, request id)."""
    seen = set()
    for d in dirs:
        for f in d.rglob("*.jsonl"):
            try:
                fh = open(f, errors="replace")
            except OSError:
                continue
            with fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = rec.get("message") or {}
                    usage, ts = msg.get("usage"), rec.get("timestamp")
                    if not isinstance(usage, dict) or not ts:
                        continue
                    key = (msg.get("id"), rec.get("requestId"))
                    if key != (None, None):
                        if key in seen:
                            continue
                        seen.add(key)
                    try:
                        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    yield (when if when.tzinfo else when.replace(tzinfo=timezone.utc)), usage, msg.get("model") or "unknown"


# ------------------------------------------------------------------ the month
@dataclass
class Month:
    period: str
    used_usd: float = 0.0
    rescued_usd: float = 0.0
    by_model: dict = field(default_factory=lambda: defaultdict(lambda: {"tokens": 0, "usd": 0.0}))
    by_source: dict = field(default_factory=lambda: defaultdict(lambda: {"tokens": 0, "usd": 0.0}))
    unpriced: set = field(default_factory=set)
    nights: int = 0
    tasks_done: int = 0
    tasks_blocked: int = 0
    claude_dirs: int = 0

    @property
    def rescue_rate(self) -> float:
        return self.rescued_usd / self.used_usd if self.used_usd else 0.0


def _in_period(dt: datetime, period: str) -> bool:
    return dt.astimezone(timezone.utc).strftime("%Y-%m") == period


def month(period: str | None = None, ledgers: list[str] | None = None, claude_logs: str | None = None,
          include_claude: bool = True) -> Month:
    period = period or datetime.now(timezone.utc).strftime("%Y-%m")
    m = Month(period)

    if include_claude:
        dirs = claude_log_dirs(claude_logs)
        m.claude_dirs = len(dirs)
        for when, usage, model in iter_claude_events(dirs):
            if not _in_period(when, period):
                continue
            usd = price(model, usage)
            toks = sum(int(usage.get(k) or 0) for k in TOKEN_FIELDS)
            if usd is None:
                if model != "<synthetic>":
                    m.unpriced.add(model)  # counted as $0: never guess a price
                usd = 0.0
            m.used_usd += usd
            m.by_model[model]["tokens"] += toks
            m.by_model[model]["usd"] += usd
            m.by_source["Claude Code"]["tokens"] += toks
            m.by_source["Claude Code"]["usd"] += usd

    # Relay ledgers: calls are already priced at API list rates (or the provider-reported cost)
    shifts: dict[str, dict] = {}
    for path in ledgers or [".relay/events.jsonl"]:
        p = Path(path).expanduser()
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not _in_period(datetime.fromtimestamp(r.get("ts", 0), timezone.utc), period):
                continue
            sess, ev = r.get("session"), r.get("event")
            if ev == "shift_start":
                shifts[sess] = {"caps": r.get("capacity") or [], "spend": defaultdict(float)}
            elif ev == "call":
                usd = float(r.get("usd") or 0)
                toks = int(r.get("input_tokens") or 0) + int(r.get("output_tokens") or 0)
                src = "Relay night shifts" if sess in shifts else "Relay"
                m.used_usd += usd
                m.by_model[r.get("model") or "unknown"]["tokens"] += toks
                m.by_model[r.get("model") or "unknown"]["usd"] += usd
                m.by_source[src]["tokens"] += toks
                m.by_source[src]["usd"] += usd
                if sess in shifts:
                    shifts[sess]["spend"][r.get("provider") or "?"] += usd
            elif ev == "task" and sess in shifts:
                m.tasks_done += r.get("status") == "done"
                m.tasks_blocked += r.get("status") == "blocked"

    for s in shifts.values():
        m.nights += 1
        for cap in s["caps"]:
            spent = sum(s["spend"].get(p, 0.0) for p in cap.get("providers", []))
            if cap.get("unit") == "usd":
                if cap.get("kind") == "api_budget" and not cap.get("expires"):
                    continue   # pay-as-you-go: spending it overnight is new money, not rescued credit
                m.rescued_usd += min(spent, float(cap.get("tonight") or 0))   # never more than was expiring
            else:
                m.rescued_usd += spent   # plan windows inside your downtime: unused otherwise by definition
    return m


def money(v: float) -> str:
    return f"${v:,.2f}"


def tokens(n: int) -> str:
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return str(n)


def render(m: Month, color: bool = True) -> str:
    c = (lambda code, s: f"\033[{code}m{s}\033[0m") if color else (lambda code, s: s)
    title = datetime.strptime(m.period, "%Y-%m").strftime("%B %Y")
    bar_w = 28
    filled = int(round(bar_w * min(1.0, m.rescue_rate)))
    lines = [
        c("1;36", f"  RELAY SAVINGS · {title}"),
        "",
        "  Used at API rates      " + c("1", f"{money(m.used_usd):>12}"),
        "  Rescued before reset   " + c("1;32", f"{money(m.rescued_usd):>12}"),
        f"  {c('32', '█' * filled)}{c('2', '░' * (bar_w - filled))}  {m.rescue_rate:.1%} rescued",
        "",
        c("2", "  by source"),
    ]
    for k, v in sorted(m.by_source.items(), key=lambda kv: -kv[1]["usd"]):
        lines.append(f"    {k:<20}{tokens(v['tokens']):>8}  {money(v['usd']):>11}")
    lines += ["", c("2", "  by model")]
    for k, v in sorted(m.by_model.items(), key=lambda kv: -kv[1]["usd"])[:6]:
        lines.append(f"    {k:<20}{tokens(v['tokens']):>8}  {money(v['usd']):>11}")
    lines += ["", f"  {m.nights} night shifts · {m.tasks_done} tasks shipped · {m.tasks_blocked} need you"]
    if m.unpriced:
        lines.append(c("33", f"  unpriced (counted as $0): {', '.join(sorted(m.unpriced))}"))
    lines.append(c("2", f"  value at API list rates as of {RATES_AS_OF}, not a bill · rescued = expiring plan capacity only"))
    return "\n".join(lines)
