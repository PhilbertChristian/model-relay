"""Event log: local JSONL always, Supabase (PostgREST) when SUPABASE_URL + SUPABASE_KEY are set."""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
import uuid
from pathlib import Path


class Telemetry:
    def __init__(self, log_dir: str = ".relay"):
        self.session = uuid.uuid4().hex[:10]
        self.path = Path(log_dir) / "events.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.sb_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
        self.sb_key = os.environ.get("SUPABASE_KEY", "") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        self.sb_table = os.environ.get("SUPABASE_TABLE", "relay_events")
        self.where = os.environ.get("AGENT37_INSTANCE_ID") or "local"

    def emit(self, event: str, **data) -> None:
        row = {"ts": time.time(), "session": self.session, "host": self.where, "event": event, **data}
        with self.path.open("a") as f:
            f.write(json.dumps(row) + "\n")
        if self.sb_url and self.sb_key:
            threading.Thread(target=self._push, args=(row,), daemon=True).start()

    def _push(self, row: dict) -> None:
        body = {"session": row["session"], "host": row["host"], "event": row["event"],
                "model": row.get("model"), "data": row}
        req = urllib.request.Request(f"{self.sb_url}/rest/v1/{self.sb_table}", data=json.dumps(body).encode(), method="POST")
        req.add_header("apikey", self.sb_key)
        req.add_header("Authorization", f"Bearer {self.sb_key}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Prefer", "return=minimal")
        try:
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:
            pass  # telemetry must never break the agent


def summarize(log_path: str = ".relay/events.jsonl") -> str:
    p = Path(log_path)
    if not p.exists():
        return "no events yet"
    rows = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    by_model: dict[str, dict] = {}
    switches: dict[str, int] = {}
    sessions = set()
    saved = spent = 0.0
    for r in rows:
        sessions.add(r["session"])
        if r["event"] == "call":
            m = by_model.setdefault(r["model"], {"calls": 0, "in": 0, "out": 0, "usd": 0.0})
            m["calls"] += 1
            m["in"] += r.get("input_tokens", 0)
            m["out"] += r.get("output_tokens", 0)
            m["usd"] += r.get("usd", 0.0)
        elif r["event"] == "switch":
            k = r.get("reason", "").split(":")[0]
            switches[k] = switches.get(k, 0) + 1
        elif r["event"] == "session_end":
            spent += r.get("usd", 0.0)
            saved += max(0.0, r.get("baseline_usd", 0.0) - r.get("usd", 0.0))
    lines = [f"sessions: {len(sessions)}", "", f"{'model':<22}{'calls':>6}{'in tok':>10}{'out tok':>10}{'usd':>10}"]
    for k, m in sorted(by_model.items(), key=lambda kv: -kv[1]["calls"]):
        lines.append(f"{k:<22}{m['calls']:>6}{m['in']:>10}{m['out']:>10}{m['usd']:>10.4f}")
    lines.append("")
    lines.append("switches: " + (", ".join(f"{k} x{v}" for k, v in switches.items()) or "none"))
    lines.append(f"spent ${spent:.4f}  vs  ${spent + saved:.4f} on top-tier only  (saved ${saved:.4f})")
    return "\n".join(lines)
