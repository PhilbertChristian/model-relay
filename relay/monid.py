"""Monid research enrichment: a short brief per task before an agent starts it. Opt-in via MONID_API_KEY.

Only a redacted query built from the project name and the task text (<= 1000 chars) leaves the machine:
never transcripts, file contents, paths or .env. The API shape is provisional and mirrors BurnerAgent's
src/monid.ts. A 429/402 or a limit-ish body pauses every research call for an hour; nothing is retried.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from .contracts import SwarmTask, Usage, redact

DEFAULT_BASE_URL = "https://api.monid.ai"          # override with MONID_BASE_URL
RESEARCH_PATH = "/v1/research"                     # POST {"query": str} -> {"notes": [str]}
CREDITS_PATH = "/v1/credits"                       # GET -> {"used", "limit", "remaining", "resets_at"} (provisional)
TOOLS_PATH = "/v1/tools"                           # GET -> {"tools": [{"name": str}]} or [str] (provisional)
QUERY_CHARS, NOTE_LIMIT, NOTE_CHARS = 1000, 3, 500
LIMIT_BACKOFF = 3600.0                             # seconds to stay off Monid after a limit
_LIMIT_RE = re.compile(r"rate[\s_-]*limit|usage[\s_-]*limit|too many requests|payment required"
                       r"|insufficient[\s_-]*(quota|funds|balance|credits?)", re.I)
_BEARER_RE = re.compile(r"Bearer\s+\S+", re.I)

limited_until = 0.0                                # epoch seconds; research() makes no call before this


def _opts(cfg: dict | None) -> dict:
    m = (cfg or {}).get("monid")
    return m if isinstance(m, dict) else {} if m is None else {"enabled": bool(m)}


def _clean(text) -> str:
    """One line, contracts.redact()ed, bearer tokens masked as well (like monid.ts)."""
    return redact(_BEARER_RE.sub("[redacted]", " ".join(str(text).split())))


def _num(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def available(cfg: dict | None = None) -> tuple[bool, str]:
    """(ok, why) with no network: MONID_API_KEY is set and cfg monid.enabled is not false."""
    if _opts(cfg).get("enabled") is False:
        return False, "monid disabled in config"
    if not os.environ.get("MONID_API_KEY", "").strip():
        return False, "$MONID_API_KEY not set"
    return True, "ok"


def is_limited(now: float | None = None) -> bool:
    return limited_until > (now or time.time())


def _http(method: str, path: str, body: dict | None = None, timeout: float = 15) -> tuple[int, str, object]:
    """(status, text, headers). HTTP errors come back as their status; network errors raise."""
    base = (os.environ.get("MONID_BASE_URL", "").strip() or DEFAULT_BASE_URL).rstrip("/")
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), method=method)
    # unredirected: urllib would otherwise forward the key to wherever a redirect points
    req.add_unredirected_header("Authorization", f"Bearer {os.environ.get('MONID_API_KEY', '').strip()}")
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace"), r.headers
    except urllib.error.HTTPError as e:
        with e:                                    # closing it releases the connection
            return e.code, e.read().decode("utf-8", "replace"), e.headers


def _query(task: SwarmTask) -> str:
    """Project name + task text only (no files, paths, todos, brief or transcripts), redacted, <= QUERY_CHARS."""
    return _clean(f"{task.project.name}: {task.text}")[:QUERY_CHARS]


def _notes(text: str) -> list[str]:
    try:
        notes = json.loads(text).get("notes")
    except (ValueError, AttributeError):
        return []
    notes = [_clean(n)[:NOTE_CHARS] for n in notes if isinstance(n, str)] if isinstance(notes, list) else []
    return [n for n in notes if n][:NOTE_LIMIT]


def _retry_after(headers) -> float:
    try:
        return float(headers.get("Retry-After") or 0)
    except (AttributeError, TypeError, ValueError):
        return 0.0


def research(task: SwarmTask, timeout: float = 30) -> str:
    """Up to 3 redacted notes on `task` as "- " bullets; "" on any failure, while limited, or without a key.
    One call, no retry: a limit pauses Monid for LIMIT_BACKOFF (or a longer Retry-After)."""
    global limited_until
    if not available()[0] or is_limited():
        return ""
    try:
        status, text, headers = _http("POST", RESEARCH_PATH, {"query": _query(task)}, timeout)
        notes = _notes(text) if status == 200 else []
        if status in (402, 429) or (not notes and _LIMIT_RE.search(text)):
            limited_until = time.time() + max(LIMIT_BACKOFF, _retry_after(headers))
        return "\n".join(f"- {n}" for n in notes)
    except Exception:
        return ""


def _when(v) -> float | None:
    """Epoch seconds (or ms) or ISO-8601 -> epoch seconds."""
    n = _num(v)
    if n is not None:
        return n / 1000 if n > 1e11 else n
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _next_month(now: float) -> float:
    d = datetime.fromtimestamp(now, timezone.utc)
    return datetime(d.year + d.month // 12, d.month % 12 + 1, 1, tzinfo=timezone.utc).timestamp()


def credits() -> Usage | None:
    """Live credit balance (provisional GET /v1/credits) as a Usage; None without a key or on any failure."""
    if not available()[0]:
        return None
    try:
        status, text, _ = _http("GET", CREDITS_PATH)
        d = json.loads(text) if status == 200 else None
        if not isinstance(d, dict):
            return None
        used, limit, left = _num(d.get("used")), _num(d.get("limit")), _num(d.get("remaining"))
        if used is None and left is not None:
            used = limit - left if limit is not None else 0.0      # a bare balance: nothing used of what is left
        if limit is None and used is not None and left is not None:
            limit = used + left
        if used is None:
            return None
        resets = _when(d.get("resets_at"))
        return Usage(name="monid", lane="monid", unit="credits", used=used, limit=limit,
                     resets_at=resets or _next_month(time.time()), source="live",
                     note="" if resets else "reset not reported; assuming the 1st of next month")
    except Exception:
        return None


def tools() -> list[str]:
    """Names of the tools Monid routes to (provisional GET /v1/tools); [] without a key or on any failure."""
    if not available()[0]:
        return []
    try:
        status, text, _ = _http("GET", TOOLS_PATH)
        d = json.loads(text) if status == 200 else None
        items = d.get("tools") if isinstance(d, dict) else d
        names = [t.get("name") if isinstance(t, dict) else t for t in items] if isinstance(items, list) else []
        return [_clean(n) for n in names if isinstance(n, str) and n.strip()]
    except Exception:
        return []


def enricher(cfg: dict | None):
    """Callable[[SwarmTask], str] that fills an empty task.brief from research(), or None when Monid is off."""
    if not available(cfg)[0]:
        return None
    timeout = _num(_opts(cfg).get("timeout")) or 30.0

    def enrich(task: SwarmTask) -> str:
        if not task.brief:                         # a retried task keeps its brief: one paid call per task
            task.brief = research(task, timeout)
        return task.brief
    return enrich
