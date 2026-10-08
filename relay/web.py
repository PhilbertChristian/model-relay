"""Live web dashboard for burn-week: `relay web`, or `relay burn week|demo --web PORT`.

    GET /                     docs/dashboard.html (one self-contained page: no CDN, no external requests)
    GET /events               Server-Sent Events: `event: reset`, the replayed rows, `event: live`, then live rows
                              (plain `data:` messages, one JSON row each) and `: hb` heartbeat comments
    GET /api/state            the folded state as JSON (relay.dash.DashState when importable, else a built-in fold)
    GET /sample-events.jsonl  docs/sample-events.jsonl (the recorded demo), 404 when absent

Rows come from a Bus (bus.rows replayed, then bus.subscribe) or, without one, from tailing the JSONL ledger,
whose replay starts at its last `swarm_start` so an old ledger doesn't flood the page. Any object with `.rows`
and `.subscribe(fn) -> unsubscribe` works as the bus (see Feed, used by `--supabase`).

Safety: binds 127.0.0.1 by default, sends no CORS headers, refuses other-origin browser requests and (while
bound to loopback) any Host header that isn't a loopback name, which stops DNS rebinding. Only the fixed routes
above exist, so no path reaches the filesystem. Every string sent passes through contracts.redact().
"""
from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import queue
import select
import socket
import socketserver
import sys
import threading
import time
import webbrowser
from collections import deque
from dataclasses import fields, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .contracts import redact

DOCS = Path(__file__).resolve().parent.parent / "docs"
DASHBOARD = DOCS / "dashboard.html"
SAMPLE = DOCS / "sample-events.jsonl"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
HEARTBEAT = 15.0          # seconds of silence before an SSE keep-alive comment
POLL = 0.25               # SSE loop tick: file tail interval, shutdown and disconnect checks
MAX_QUEUE = 20000         # rows buffered per SSE client; a client that falls this far behind is dropped (it reconnects)
MAX_LINE = 1 << 20        # a partial JSONL line longer than this is garbage, not a row being written
REPLAY_MAX = 5000         # ledger rows replayed when the file has no swarm_start
NO_CACHE = "no-cache, no-store, must-revalidate"
MARK = b'<meta name="relay-dashboard" content="burn-week">'               # served by relay web: the page goes live on SSE
LIVE = b'<meta name="relay-dashboard" content="burn-week" data-source="relay-web">'
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
       "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
_END = ("done", "blocked", "failed", "limited")


# --------------------------------------------------------------------------- rows
def _clean(o: Any, depth: int = 0) -> Any:
    """JSON-safe deep copy: strings redacted, NaN/inf -> None, objects -> dicts (to_dict / dataclass / vars)."""
    if depth > 24:
        return None
    if o is None or isinstance(o, (bool, int)):
        return o
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, str):
        return redact(o)
    if isinstance(o, dict):
        return {str(k): _clean(v, depth + 1) for k, v in o.items()}
    if isinstance(o, (list, tuple, set, frozenset, deque)):
        return [_clean(v, depth + 1) for v in o]
    for name in ("to_dict", "as_dict", "snapshot"):
        f = getattr(o, name, None)
        if callable(f):
            return _clean(f(), depth + 1)
    if is_dataclass(o) and not isinstance(o, type):
        return _clean({f.name: getattr(o, f.name) for f in fields(o)}, depth + 1)
    if hasattr(o, "__dict__"):
        return _clean({k: v for k, v in vars(o).items() if not k.startswith("_")}, depth + 1)
    return redact(str(o))


def _parse(line: bytes) -> dict | None:
    try:
        row = json.loads(line)
    except ValueError:                      # also UnicodeDecodeError; blank lines land here too
        return None
    return row if isinstance(row, dict) else None


def _current_run(rows: list[dict]) -> list[dict]:
    """Rows from the last swarm_start on (the run on screen); the tail of the ledger when there is none."""
    for i in range(len(rows) - 1, -1, -1):
        if rows[i].get("event") == "swarm_start":
            return rows[i:]
    return rows[-REPLAY_MAX:]


def _num(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _blank() -> dict:
    return {"run": None, "started": None, "ended": None, "resets_at": None, "max_agents": 0, "host": None,
            "queue": 0, "queued": 0, "lanes": {}, "meters": {}, "agents": {}, "landed": [], "limited": {}, "end": None,
            "totals": {"tokens": 0, "usd": 0.0, "running": 0, "queued": 0, **{k: 0 for k in _END}}}


def _fold(rows: list[dict]) -> dict:
    """Built-in fold, used when relay.dash is missing: run, meters, lanes, running agents, landed, totals."""
    s, started, n = _blank(), set(), 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        n += 1
        ev, ts = r.get("event"), r.get("ts")
        s["host"] = r.get("host") or s["host"]
        key = str(r.get("task_id") or r.get("agent"))
        if ev == "swarm_start":
            s, started = _blank(), set()
            s.update(run=r.get("run"), started=ts, resets_at=r.get("resets_at"), host=r.get("host"),
                     max_agents=r.get("max_agents") or 0, queue=int(_num(r.get("queue"))))
            for ln in r.get("lanes") or []:
                if isinstance(ln, dict) and ln.get("name"):
                    s["lanes"][str(ln["name"])] = {**ln, "tokens": 0, "usd": 0.0, "tasks": 0}
            for u in r.get("usages") or []:
                if isinstance(u, dict) and u.get("name"):
                    s["meters"][str(u["name"])] = dict(u)
        elif ev == "pace":
            name = r.get("subscription") or r.get("lane")
            if name:
                m = s["meters"].setdefault(str(name), {"name": name})
                m.update({k: v for k, v in r.items() if k not in ("ts", "session", "host", "event")}, updated=ts)
                now, left = _num(ts), _num(r.get("hours_left"))
                if now > 0 and left > 0 and _num(m.get("resets_at")) <= now:    # missing or passed: each plan has its own clock
                    m["resets_at"] = now + left * 3600
        elif ev == "task_queued":
            s["queued"] += 1
        elif ev == "agent_start":
            started.add(r.get("task_id"))
            s["agents"][key] = {k: r.get(k) for k in ("agent", "task_id", "project", "text", "lane", "branch")} | {
                "tokens": 0, "usd": 0.0, "note": "", "since": ts}
        elif ev == "agent_progress" and key in s["agents"]:
            a = s["agents"][key]
            a.update(tokens=int(_num(r.get("tokens"), a["tokens"])), usd=_num(r.get("usd"), a["usd"]),
                     note=r.get("note") or a["note"])
        elif ev == "agent_end":
            a = s["agents"].pop(key, None) or {}
            status = r.get("status") if r.get("status") in _END else "failed"
            tok, usd = int(_num(r.get("tokens"), a.get("tokens", 0))), _num(r.get("usd"), a.get("usd", 0.0))
            t = s["totals"]
            t[status] += 1
            t["tokens"] += tok
            t["usd"] += usd
            lane = str(r.get("lane") or a.get("lane") or "?")
            ls = s["lanes"].setdefault(lane, {"name": lane, "tokens": 0, "usd": 0.0, "tasks": 0})
            ls["tokens"] += tok
            ls["usd"] += usd
            ls["tasks"] += 1
            done = {k: r.get(k) for k in ("agent", "task_id", "project", "lane", "summary", "commit", "diffstat",
                                          "tests_ok", "seconds")}
            s["landed"] = [{**done, "status": status, "tokens": tok, "usd": usd, "branch": a.get("branch"),
                            "text": a.get("text"), "ts": ts}] + s["landed"][:49]
        elif ev == "lane_limited":
            s["limited"][str(r.get("lane") or "?")] = {"until": r.get("until"), "reason": r.get("reason"), "ts": ts}
        elif ev == "swarm_end":
            s["end"] = {k: v for k, v in r.items() if k not in ("session", "host", "event")}
            s["ended"] = ts
    t, agents = s["totals"], list(s["agents"].values())
    t["tokens"] += sum(int(_num(a["tokens"])) for a in agents)        # in flight
    t["usd"] += sum(_num(a["usd"]) for a in agents)
    t["running"], t["queued"] = len(agents), max(0, max(s["queue"], s["queued"]) - len(started))   # queue: same tasks
    for k in ("tokens", "usd", *_END):
        if s["end"] and isinstance(s["end"].get(k), (int, float)):
            t[k] = s["end"][k]                                        # swarm_end is authoritative
    burning = {str(ln.get("subscription")) for ln in s["lanes"].values() if isinstance(ln, dict)} & set(s["meters"])
    clocks = sorted((_num(m.get("resets_at")), k) for k, m in s["meters"].items() if not burning or k in burning)
    nxt = next((c for c in clocks if c[0] > time.time()), None)       # the soonest reset among the plans being burned
    s.update(agents=agents, rows=n, next_reset={"at": nxt[0], "plan": nxt[1]} if nxt else None)
    return s


def fold(rows: list[dict]) -> tuple[dict, str]:
    """(state, source): relay.dash.DashState folded over rows when importable, else the built-in fold."""
    try:
        st = importlib.import_module(f"{__package__}.dash").DashState()
        for r in rows:
            st.apply(r)
        out = _clean(st)
        if isinstance(out, dict):
            return out, "relay.dash"
    except Exception:                       # missing, half-written or failing sibling: never break the page
        pass
    return _clean(_fold(rows)), "relay.web"


def _sse(event: str | None, obj: Any) -> bytes:
    data = json.dumps(_clean(obj), separators=(",", ":"), allow_nan=False)
    return ((f"event: {event}\n" if event else "") + f"data: {data}\n\n").encode()


# --------------------------------------------------------------------------- feeds
class _BusFeed:
    """A per-client queue fed by bus.subscribe; the replay is bus.rows with no gap and no duplicate."""
    kind = "bus"

    def __init__(self, bus: Any):
        self.bus, self.q, self.overflow, self._unsub = bus, queue.Queue(MAX_QUEUE), False, None
        self._dup: dict[int, dict] = {}

    def _push(self, row: dict) -> None:     # runs on the emitting (swarm) thread: never block it
        try:
            self.q.put_nowait(row)
        except queue.Full:
            self.overflow = True

    def open(self) -> list[dict]:
        rows = getattr(self.bus, "rows", None) or []
        n0 = len(rows)
        unsub = self.bus.subscribe(self._push)
        self._unsub = unsub if callable(unsub) else (lambda: self.bus.unsubscribe(self._push))
        snap = list(getattr(self.bus, "rows", None) or [])
        # Bus.emit appends under its lock and notifies after: rows appended after n0 may also reach the queue.
        self._dup = {id(r): r for r in snap[n0:]}     # holding the row keeps its id unique
        return snap

    def poll(self, timeout: float) -> tuple[bool, list[dict]]:
        out = []
        try:
            row = self.q.get(timeout=timeout)
            while True:
                if self._dup.pop(id(row), None) is None:
                    out.append(row)
                row = self.q.get_nowait()
        except queue.Empty:
            pass
        return False, out

    def close(self) -> None:
        if self._unsub:
            self._unsub, unsub = None, self._unsub
            try:
                unsub()
            except Exception:
                pass


class _FileFeed:
    """Tail a JSONL ledger. poll() -> (reset, rows): reset when the file was truncated, replaced or removed."""
    kind = "file"
    overflow = False

    def __init__(self, path: str, stop: threading.Event):
        self.path, self.stop = path, stop
        self.pos, self.ino, self.buf = 0, None, b""

    def open(self) -> list[dict]:
        return _current_run(self._read()[1])

    def poll(self, timeout: float) -> tuple[bool, list[dict]]:
        self.stop.wait(timeout)
        reset, rows = self._read()
        return reset, _current_run(rows) if reset else rows

    def close(self) -> None:
        pass

    def _read(self) -> tuple[bool, list[dict]]:
        try:
            st = os.stat(self.path)
        except OSError:
            reset = self.pos > 0
            self.pos, self.ino, self.buf = 0, None, b""
            return reset, []
        reset = self.ino is not None and (st.st_ino != self.ino or st.st_size < self.pos)
        if reset:
            self.pos, self.buf = 0, b""
        self.ino = st.st_ino
        if st.st_size <= self.pos:
            return reset, []
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read(st.st_size - self.pos)
        except OSError:
            return reset, []
        self.pos += len(data)
        *lines, self.buf = (self.buf + data).split(b"\n")     # keep a half-written last line for later
        if len(self.buf) > MAX_LINE:
            self.buf = b""
        return reset, [r for r in map(_parse, lines) if r is not None]


class Feed:
    """Bus stand-in fed by polling fetch(since_ts) -> rows, e.g. relay.supa.recent_events (`relay web --supabase`)."""

    def __init__(self, fetch: Callable[[float], list[dict]], interval: float = 2.0):
        self.rows: list[dict] = []
        self._subs: list[Callable[[dict], None]] = []
        self._lock, self._stop, self._seen = threading.Lock(), threading.Event(), set()
        threading.Thread(target=self._loop, args=(fetch, interval), name="relay-web-feed", daemon=True).start()

    def subscribe(self, fn: Callable[[dict], None]) -> Callable[[], None]:
        with self._lock:
            self._subs.append(fn)

        def unsubscribe() -> None:
            with self._lock:
                if fn in self._subs:
                    self._subs.remove(fn)
        return unsubscribe

    def close(self) -> None:
        self._stop.set()

    def _loop(self, fetch: Callable[[float], list[dict]], interval: float) -> None:
        since = 0.0
        while not self._stop.is_set():
            try:
                got = [r for r in fetch(since) or [] if isinstance(r, dict)]
            except Exception:               # offline or misconfigured: keep the page up, try again later
                got = []
            for r in sorted(got, key=lambda r: _num(r.get("ts"))):
                key = (r.get("session"), r.get("ts"), r.get("event"), r.get("task_id"), r.get("agent"), r.get("lane"))
                if key in self._seen:
                    continue
                self._seen.add(key)
                since = max(since, _num(r.get("ts")))
                with self._lock:
                    self.rows.append(r)
                    subs = list(self._subs)
                for fn in subs:
                    try:
                        fn(r)
                    except Exception:
                        pass
            self._stop.wait(interval)


# --------------------------------------------------------------------------- HTTP
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "relay-web"
    sys_version = ""
    timeout = 60                            # idle keep-alive connections give their thread back
    server: WebServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the terminal belongs to the TUI
        pass

    def do_GET(self) -> None:
        self._streaming = False
        try:
            self._route()
        except OSError:                     # client went away mid-response
            self.close_connection = True
        except Exception as e:
            self.close_connection = True
            if not self._streaming:
                try:
                    self._send(500, f"relay web: {type(e).__name__}\n".encode(), "text/plain; charset=utf-8")
                except Exception:
                    pass

    def _trusted(self) -> bool:
        """Same-machine browsers only: loopback Host (DNS rebinding) and no foreign Origin."""
        if not self.server.loopback:
            return True                     # bound to an interface on purpose: the user exposed it
        host, origin = self.headers.get("Host"), self.headers.get("Origin")
        try:                                # no Host: HTTP/1.0 tool; no Origin: same-origin GET or not a browser
            names = [urlsplit("//" + host).hostname if host else "localhost",
                     urlsplit(origin).hostname if origin else "localhost"]   # "null" -> None -> refused
        except ValueError:
            return False
        return all(n and (n in LOOPBACK or n.endswith(".localhost")) for n in names)

    def _route(self) -> None:
        if not self._trusted():
            return self._send(403, b"relay web: loopback only\n", "text/plain; charset=utf-8")
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path in ("/", "/index.html", "/dashboard.html"):
            return self._file(DASHBOARD, "text/html; charset=utf-8",
                              {"Content-Security-Policy": CSP, "X-Frame-Options": "DENY"}, live=True)
        if path == "/events":
            return self._events()
        if path == "/api/state":
            state, source = fold(self.server.rows())
            return self._send(200, json.dumps(state, allow_nan=False).encode(), "application/json; charset=utf-8",
                              {"X-Relay-Fold": source})
        if path == "/sample-events.jsonl":
            return self._file(SAMPLE, "application/x-ndjson; charset=utf-8")
        self._send(404, b"not found\n", "text/plain; charset=utf-8")

    def _send(self, status: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", NO_CACHE)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, ctype: str, headers: dict | None = None, live: bool = False) -> None:
        try:
            body = path.read_bytes()        # fixed paths only: nothing from the URL reaches the filesystem
        except OSError:
            return self._send(404, b"not found\n", "text/plain; charset=utf-8")
        self._send(200, body.replace(MARK, LIVE, 1) if live else body, ctype, headers)

    def _events(self) -> None:
        srv = self.server
        feed = _BusFeed(srv.bus) if srv.bus is not None else _FileFeed(srv.events_path, srv.stopping)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", NO_CACHE)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection, self._streaming = True, True
        srv.track(1)
        try:
            self._replay(feed.open(), feed.kind, b"retry: 3000\n\n")
            quiet = time.monotonic()
            while not srv.stopping.is_set() and not feed.overflow:
                reset, rows = feed.poll(POLL)
                if reset:
                    self._replay(rows, feed.kind)
                elif rows:
                    self.wfile.write(b"".join(_sse(None, r) for r in rows))
                if reset or rows:
                    quiet = time.monotonic()
                elif time.monotonic() - quiet >= srv.heartbeat:
                    self.wfile.write(b": hb\n\n")
                    quiet = time.monotonic()
                if self._gone():
                    break
        finally:
            feed.close()                    # unsubscribes from the bus
            srv.track(-1)

    def _replay(self, rows: list[dict], kind: str, head: bytes = b"") -> None:
        n = len(rows)
        self.wfile.write(head + _sse("reset", {"source": kind, "replay": n}) +
                         b"".join(_sse(None, r) for r in rows) + _sse("live", {"replayed": n}))

    def _gone(self) -> bool:
        """The client hung up: its socket reads EOF (checked without consuming anything)."""
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
            return bool(ready) and not self.connection.recv(1, socket.MSG_PEEK)
        except (OSError, ValueError):
            return True


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], events_path: str, bus: Any = None):
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        self.events_path, self.bus = events_path, bus
        self.loopback = addr[0] in LOOPBACK or addr[0].startswith("127.")
        self.stopping, self.heartbeat, self.thread = threading.Event(), HEARTBEAT, None
        self._streams, self._cv = 0, threading.Condition()
        super().__init__(addr, _Handler)

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)   # skip HTTPServer's reverse-DNS lookup (slow on some Macs)
        self.server_name, self.server_port = str(self.server_address[0]), int(self.server_address[1])

    @property
    def url(self) -> str:
        host, port = str(self.server_address[0]), int(self.server_address[1])
        host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
        return f"http://{f'[{host}]' if ':' in host else host}:{port}/"

    def rows(self) -> list[dict]:
        if self.bus is not None:
            return list(getattr(self.bus, "rows", None) or [])
        return _current_run(_FileFeed(self.events_path, self.stopping)._read()[1])

    def track(self, n: int) -> None:
        with self._cv:
            self._streams += n
            self._cv.notify_all()

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass                                # quiet: a dropped browser tab is not news

    def shutdown(self) -> None:
        """Stop serving, end every SSE stream, free the port. Safe to call twice."""
        self.stopping.set()
        if self.thread is not None and self.thread.is_alive():
            super().shutdown()
        self.server_close()
        with self._cv:
            self._cv.wait_for(lambda: self._streams <= 0, timeout=2)
        if self.thread is not None:
            self.thread.join(timeout=2)


def serve(port: int = 3737, events_path: str = ".relay/events.jsonl", bus: Any = None,
          open_browser: bool = False, host: str = "127.0.0.1") -> WebServer:
    """Start the dashboard in a daemon thread and return the server (.url, .shutdown()). port=0: any free port."""
    srv = WebServer((host, port), os.path.abspath(os.path.expanduser(events_path)), bus)
    srv.thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, name="relay-web",
                                  daemon=True)
    srv.thread.start()
    if open_browser:
        threading.Thread(target=webbrowser.open, args=(srv.url,), name="relay-web-open", daemon=True).start()
    return srv


def main(argv: list[str] | None = None) -> int:
    """`python3 -m relay.web`: serve until Ctrl-C."""
    from . import ui
    ap = argparse.ArgumentParser(prog="relay web", description="Live burn-week dashboard over SSE.")
    ap.add_argument("--port", type=int, default=3737, help="port (default 3737; 0 = any free port)")
    ap.add_argument("--events", default=".relay/events.jsonl", help="JSONL ledger to tail (default .relay/events.jsonl)")
    ap.add_argument("--host", default="127.0.0.1", help="interface to bind (default 127.0.0.1)")
    ap.add_argument("--supabase", action="store_true", help="follow events from Supabase instead of the ledger")
    ap.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    a = ap.parse_args(argv)
    bus = None
    if a.supabase:
        supa = importlib.import_module(f"{__package__}.supa")
        if not supa.available():
            print("relay web: --supabase needs SUPABASE_URL and SUPABASE_KEY", file=sys.stderr)
            return 2
        bus = Feed(lambda since: supa.recent_events(since_ts=since))
    try:
        srv = serve(a.port, a.events, bus, a.open, a.host)
    except OSError as e:
        print(f"relay web: cannot listen on {a.host}:{a.port}: {e}", file=sys.stderr)
        return 1
    print(ui.c("1;36", "relay web") + f" · {srv.url} · " + ("supabase" if a.supabase else srv.events_path), flush=True)
    try:
        while srv.thread.is_alive():
            srv.thread.join(0.5)
    except KeyboardInterrupt:
        print()
    finally:
        srv.shutdown()
        if isinstance(bus, Feed):
            bus.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
