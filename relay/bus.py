"""Thread-safe event bus for burn-week: every event goes to the JSONL ledger (and Supabase, via Telemetry)
and to in-process subscribers (terminal dashboard, web SSE)."""
from __future__ import annotations

import threading
from typing import Callable

from .contracts import redact
from .telemetry import Telemetry


class Bus:
    def __init__(self, tel: Telemetry | None = None, log_dir: str = ".relay"):
        self.tel = tel or Telemetry(log_dir)
        self._lock = threading.Lock()
        self._subs: list[Callable[[dict], None]] = []
        self.rows: list[dict] = []          # in-memory history, so late subscribers can replay

    def subscribe(self, fn: Callable[[dict], None]) -> Callable[[], None]:
        with self._lock:
            self._subs.append(fn)
        return lambda: self.unsubscribe(fn)

    def unsubscribe(self, fn: Callable[[dict], None]) -> None:
        with self._lock:
            if fn in self._subs:
                self._subs.remove(fn)

    def emit(self, event: str, **data) -> dict:
        data = {k: redact(v) if isinstance(v, str) else v for k, v in data.items()}
        with self._lock:
            row = self.tel.emit(event, **data)
            self.rows.append(row)
            subs = list(self._subs)
        for fn in subs:
            try:
                fn(row)
            except Exception:
                pass                        # a broken dashboard must never stop the swarm
        return row
