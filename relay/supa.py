"""Read side of the Supabase telemetry relay/telemetry.py writes: PostgREST GETs on SUPABASE_TABLE (relay_events).

Telemetry pushes each JSONL row whole into the jsonb `data` column, so rows come back shaped like
.relay/events.jsonl lines ({"ts", "session", "host", "event", ...data}), redacted, and [] on any failure.
The table has no ts column: filters and ordering use data->ts, the row's own epoch ts. Reads need a key that
row-level security lets select (supabase/schema.sql only opens inserts to anon).
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import quote, urlencode

from .contracts import redact

TIMEOUT = 10.0
RUN_EVENTS = ("swarm_start", "swarm_end")


def _env() -> tuple[str, str, str]:
    """(url, key, table), read at call time like relay.telemetry.Telemetry."""
    return (os.environ.get("SUPABASE_URL", "").strip().rstrip("/"),
            os.environ.get("SUPABASE_KEY", "") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", ""),
            os.environ.get("SUPABASE_TABLE") or "relay_events")


def available() -> bool:
    """SUPABASE_URL and SUPABASE_KEY (or SUPABASE_SERVICE_ROLE_KEY) are set. No network."""
    url, key, _ = _env()
    return bool(url and key)


def _scrub(v):
    """contracts.redact() every string: the shared table may hold rows from any writer."""
    if isinstance(v, str):
        return redact(v)
    if isinstance(v, dict):
        return {k: _scrub(x) for k, x in v.items()}
    return [_scrub(x) for x in v] if isinstance(v, list) else v


def _epoch(s) -> float:
    """PostgREST timestamptz ('2026-10-07T16:20:00.5+00:00') -> epoch seconds; 0.0 when unparseable."""
    try:  # Python 3.10's fromisoformat wants 3 or 6 fraction digits and no 'Z'
        s = re.sub(r"\.(\d+)", lambda m: "." + m[1][:6].ljust(6, "0"), str(s)).replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return 0.0


def _row(r: dict) -> dict:
    """A table row -> the JSONL row it was pushed from: {"ts", "session", "host", "event", ...data}."""
    data = r.get("data") if isinstance(r.get("data"), dict) else {}
    try:
        ts = float(data["ts"])
    except (KeyError, TypeError, ValueError):
        ts = _epoch(r.get("created_at"))
    return _scrub({"ts": ts, "session": r.get("session"), "host": r.get("host"), "event": r.get("event"),
                   **data, "ts": ts})


def _get(params: dict) -> list[dict]:
    url, key, table = _env()
    if not (url and key):
        return []
    req = urllib.request.Request(f"{url}/rest/v1/{quote(table, safe='')}?{urlencode(params, quote_via=quote)}",
                                 method="GET")
    req.add_unredirected_header("apikey", key)     # unredirected: urllib would forward these on a redirect
    req.add_unredirected_header("Authorization", f"Bearer {key}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            rows = json.loads(resp.read().decode("utf-8", "replace"))
        return [_row(r) for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    except urllib.error.HTTPError as e:
        e.close()                                  # release the connection: an error status is a failure too
        return []
    except Exception:
        return []


def recent_events(since_ts: float = 0, limit: int = 500, session: str | None = None) -> list[dict]:
    """Rows after `since_ts`, oldest first, at most `limit` (poll again from the last ts to page forward).
    With since_ts <= 0 it is the newest `limit` rows, still oldest first. [] on any failure."""
    q: dict = {}
    if since_ts and since_ts > 0:
        q["data->ts"] = f"gt.{float(since_ts)}"
    if session:
        q["session"] = f"eq.{session}"
    newest = "data->ts" not in q
    q.update(order="data->ts.desc.nullslast" if newest else "data->ts.asc", limit=max(1, int(limit)))
    rows = _get(q)
    return rows[::-1] if newest else rows


def runs(limit: int = 20) -> list[dict]:
    """Newest-first run summaries: a run's swarm_start fields merged with its swarm_end fields, plus
    "started" / "ended" (epoch or None) and "status" ("running" until its swarm_end lands). [] on any failure."""
    limit = max(1, int(limit))
    rows = _get({"event": f"in.({','.join(RUN_EVENTS)})", "order": "data->ts.desc.nullslast", "limit": limit * 2})
    out: dict[tuple[str, str], dict] = {}
    for r in sorted(rows, key=lambda r: r["ts"]):
        run = r.get("run") or r.get("session")
        s = out.setdefault((str(r.get("session")), str(run)), {"run": run, "started": None, "ended": None,
                                                               "status": "running"})
        s.update({k: v for k, v in r.items() if k not in ("ts", "event", "run")})
        if r["event"] == "swarm_end":
            s.update(ended=r["ts"], status="ended")
        else:
            s["started"] = r["ts"]
    return sorted(out.values(), key=lambda s: s["started"] or s["ended"] or 0, reverse=True)[:limit]
