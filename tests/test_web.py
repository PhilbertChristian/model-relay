"""python3 -m unittest tests.test_web -v

relay web end to end over real sockets on 127.0.0.1 (port 0, no external network): a Bus over a temp .relay
dir, urllib clients, a tiny SSE reader. relay.dash is swapped for a fake or hidden, so these tests don't depend
on the sibling module's shape.
"""
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from relay import web
from relay.bus import Bus

DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SCRUB = ("SUPABASE_URL", "SUPABASE_KEY", "SUPABASE_SERVICE_ROLE_KEY", "AGENT37_INSTANCE_ID")
SECRET = "sk-ant-" + "a1B2c3D4e5F6g7H8i9J0" * 2


def emit_run(bus):
    """One small run: a done task and a task still running."""
    reset = time.time() + 3600
    bus.emit("swarm_start", run="r1", max_agents=2, queue=2, resets_at=reset,
             lanes=[{"name": "claude", "kind": "mock", "subscription": "claude-max", "as": "claude"}],
             usages=[{"name": "claude-max", "lane": "claude", "unit": "tokens", "used": 100, "limit": 1000,
                      "resets_at": reset},
                     {"name": "monid", "lane": "monid", "unit": "credits", "used": 1, "limit": 9,
                      "resets_at": reset - 3000}])                     # sooner, but no lane burns it
    bus.emit("task_queued", task_id="demo-a1", project="demo", text="add a --name flag", source="todo")
    bus.emit("agent_start", agent=1, task_id="demo-a1", project="demo", text="add a --name flag", lane="claude",
             branch="relay/burn/demo-a1", worktree="/tmp/wt/demo-a1")
    bus.emit("agent_progress", agent=1, task_id="demo-a1", lane="claude", tokens=1200, usd=0.01, note="editing")
    bus.emit("agent_end", agent=1, task_id="demo-a1", project="demo", lane="claude", status="done",
             summary="added the flag", tokens=4000, usd=0.03, commit="abc1234",
             diffstat="1 file changed, 3 insertions(+)", tests_ok=True, seconds=12.5)
    bus.emit("agent_start", agent=2, task_id="demo-b2", project="demo", text="write a README", lane="claude",
             branch="relay/burn/demo-b2", worktree="/tmp/wt/demo-b2")
    bus.emit("agent_progress", agent=2, task_id="demo-b2", lane="claude", tokens=500, usd=0.002, note="reading")


class SSE:
    """Minimal EventSource: next() -> (event, data); ':' comments -> ("comment", text); EOF -> None."""

    def __init__(self, url, timeout=5):
        self.resp = DIRECT.open(url, timeout=timeout)

    def next(self):
        event, data = "message", []
        while True:
            line = self.resp.readline()
            if not line:
                return None
            line = line.decode().rstrip("\r\n")
            if not line:
                if data:
                    return event, "\n".join(data)
                event = "message"
                continue
            if line.startswith(":"):
                return "comment", line[1:].strip()
            name, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if name == "event":
                event = value
            elif name == "data":
                data.append(value)

    def msg(self):
        """Next event with its data parsed, skipping heartbeat comments."""
        while (got := self.next()) is not None:
            if got[0] != "comment":
                return got[0], json.loads(got[1])
        raise AssertionError("stream ended")

    def until(self, event, limit=500):
        """Messages (parsed rows) up to and including `event`; raises if the stream ends first."""
        rows = []
        for _ in range(limit):
            got = self.next()
            if got is None:
                raise AssertionError(f"stream ended before {event!r}")
            if got[0] == "message":
                rows.append(json.loads(got[1]))
            if got[0] == event:
                return rows
        raise AssertionError(f"no {event!r} in {limit} messages")

    def close(self):
        self.resp.close()


class CountingBus(Bus):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.subscribed = self.unsubscribed = 0

    def subscribe(self, fn):
        self.subscribed += 1
        return super().subscribe(fn)

    def unsubscribe(self, fn):
        self.unsubscribed += 1
        super().unsubscribe(fn)


class WebCase(unittest.TestCase):
    """Fresh server per test on an ephemeral port, own temp ledger, scrubbed env (no Supabase pushes)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="relay-web-")
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for k in SCRUB:
            os.environ.pop(k, None)
        self.bus = CountingBus(log_dir=os.path.join(self.tmp, ".relay"))
        emit_run(self.bus)
        self.srv = self.start(bus=self.bus)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self, **kw):
        srv = web.serve(port=0, **kw)
        srv.heartbeat = 0.3
        self.addCleanup(srv.shutdown)
        return srv

    def get(self, path, srv=None, headers=None):
        req = urllib.request.Request((srv or self.srv).url.rstrip("/") + path, headers=headers or {})
        try:
            with DIRECT.open(req, timeout=5) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, dict(e.headers), e.read()


class PagesTest(WebCase):
    def test_index_serves_the_dashboard(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertIn(b'<meta name="relay-dashboard" content="burn-week" data-source="relay-web">', body)   # goes live
        self.assertIn(b'<meta name="relay-dashboard" content="burn-week">', web.DASHBOARD.read_bytes())    # static: replay
        self.assertIn("no-cache", headers["Cache-Control"])
        self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.get("/dashboard.html")[2], body)

    def test_page_is_self_contained_and_never_parses_event_text_as_html(self):
        html = web.DASHBOARD.read_text()
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                    "rel=\"stylesheet\"", "@import", "<script src", "src=\"http", "href=\"http", "https://", "url(http"):
            self.assertNotIn(bad, html, bad)
        self.assertEqual(re.findall(r'<link[^>]*href="(?!data:)', html), [])                # favicon is a data: URI
        self.assertIn("EventSource(", html)
        self.assertIn("sample-events.jsonl", html)
        self.assertIn("prefers-color-scheme: dark", html)

    def test_sample_events_served_only_when_recorded(self):
        sample = Path(self.tmp) / "sample-events.jsonl"
        with mock.patch.object(web, "SAMPLE", sample):
            self.assertEqual(self.get("/sample-events.jsonl")[0], 404)
            sample.write_text('{"ts": 1, "event": "swarm_start"}\n')
            status, headers, body = self.get("/sample-events.jsonl")
        self.assertEqual((status, body), (200, b'{"ts": 1, "event": "swarm_start"}\n'))
        self.assertIn("ndjson", headers["Content-Type"])

    def test_unknown_paths_and_traversal_are_404(self):
        for path in ("/nope", "/.env", "/relay/web.py", "/docs/dashboard.html", "/../relay/web.py",
                     "/%2e%2e/%2e%2e/etc/passwd", "/events/../.env", "/api/state/../../.env", "/..%2f.env",
                     "/sample-events.jsonl/../../relay.example.json", "//etc/passwd", "/api"):
            status, headers, body = self.get(path)
            self.assertEqual(status, 404, path)
            self.assertNotIn(b"relay", body.replace(b"not found", b""), path)

    def test_refuses_foreign_host_and_origin(self):
        self.assertEqual(self.get("/api/state", headers={"Host": "evil.example:3737"})[0], 403)   # DNS rebinding
        self.assertEqual(self.get("/events", headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.get("/", headers={"Origin": "null"})[0], 403)
        self.assertEqual(self.get("/api/state", headers={"Host": "localhost:1", "Origin": "http://localhost:1"})[0], 200)

    def test_open_browser_opens_the_dashboard_url(self):
        opened = threading.Event()
        with mock.patch("webbrowser.open", side_effect=lambda url: opened.set()) as op:
            srv = self.start(bus=self.bus, open_browser=True)
            self.assertTrue(opened.wait(3))
        op.assert_called_once_with(srv.url)
        self.assertTrue(srv.url.startswith("http://127.0.0.1:"))


class StateTest(WebCase):
    def test_builtin_fold_reflects_events(self):
        with mock.patch.dict(sys.modules, {"relay.dash": None}):     # sibling missing: built-in fold
            status, headers, body = self.get("/api/state")
            self.assertEqual((status, headers["X-Relay-Fold"]), (200, "relay.web"))
            s = json.loads(body)
            self.assertEqual(s["run"], "r1")
            self.assertEqual(s["totals"]["done"], 1)
            self.assertEqual((s["totals"]["running"], s["totals"]["queued"]), (1, 0))   # queue=2 and its task_queued: same tasks
            self.assertEqual(s["totals"]["tokens"], 4500)                    # 4000 landed + 500 in flight
            self.assertEqual(s["landed"][0]["commit"], "abc1234")
            self.assertEqual(s["landed"][0]["branch"], "relay/burn/demo-a1")
            self.assertEqual([a["task_id"] for a in s["agents"]], ["demo-b2"])
            self.assertEqual(s["meters"]["claude-max"]["limit"], 1000)
            self.assertEqual(s["next_reset"]["plan"], "claude-max")      # every plan keeps its own clock
            self.bus.emit("pace", lane="codex", subscription="codex", unit="tokens", used=5, limit=10, hours_left=0.25)
            s = json.loads(self.get("/api/state")[2])
            self.assertAlmostEqual(s["meters"]["codex"]["resets_at"], s["meters"]["codex"]["updated"] + 900, delta=1)
            self.bus.emit("lane_limited", lane="claude", until=time.time() + 60, reason="usage limit")
            self.bus.emit("swarm_end", run="r1", done=1, blocked=0, failed=0, limited=1, tokens=4600, usd=0.04,
                          by_lane={}, report="BURN.md", seconds=30, reason="limited")
            s = json.loads(self.get("/api/state")[2])
        self.assertEqual(s["limited"]["claude"]["reason"], "usage limit")
        self.assertEqual((s["totals"]["tokens"], s["totals"]["limited"]), (4600, 1))
        self.assertEqual(s["end"]["reason"], "limited")

    def test_uses_dash_state_when_importable(self):
        class DashState:
            def __init__(self):
                self.events = []

            def apply(self, row):
                self.events.append(row["event"])

        fake = types.ModuleType("relay.dash")
        fake.DashState = DashState
        with mock.patch.dict(sys.modules, {"relay.dash": fake}):
            status, headers, body = self.get("/api/state")
        self.assertEqual((status, headers["X-Relay-Fold"]), (200, "relay.dash"))
        self.assertEqual(json.loads(body)["events"][:3], ["swarm_start", "task_queued", "agent_start"])

    def test_broken_dash_state_falls_back(self):
        fake = types.ModuleType("relay.dash")
        fake.DashState = type("DashState", (), {"apply": lambda self, row: 1 / 0})
        with mock.patch.dict(sys.modules, {"relay.dash": fake}):
            status, headers, body = self.get("/api/state")
        self.assertEqual((status, headers["X-Relay-Fold"], json.loads(body)["run"]), (200, "relay.web", "r1"))


class EventsTest(WebCase):
    def test_replays_rows_then_streams_live_rows(self):
        sse = SSE(self.srv.url + "events")
        self.addCleanup(sse.close)
        self.assertEqual(sse.msg(), ("reset", {"source": "bus", "replay": 7}))
        replay = sse.until("live")
        self.assertEqual([r["event"] for r in replay], ["swarm_start", "task_queued", "agent_start", "agent_progress",
                                                         "agent_end", "agent_start", "agent_progress"])
        self.assertEqual(replay[4]["commit"], "abc1234")
        self.bus.emit("lane_limited", lane="codex", until=time.time() + 60, reason="usage limit")
        self.bus.emit("agent_progress", agent=2, task_id="demo-b2", lane="claude", tokens=900, usd=0.004, note="x")
        got = [sse.msg() for _ in range(2)]
        self.assertEqual([(e, r["event"], r.get("lane")) for e, r in got],
                         [("message", "lane_limited", "codex"), ("message", "agent_progress", "claude")])
        self.assertEqual(got[1][1]["tokens"], 900)

    def test_heartbeat_and_unsubscribe_on_disconnect(self):
        sse = SSE(self.srv.url + "events")
        sse.until("live")
        self.assertEqual(sse.next(), ("comment", "hb"))                    # heartbeat 0.3 s in tests
        self.assertEqual((self.bus.subscribed, self.bus.unsubscribed), (1, 0))
        sse.close()
        deadline = time.time() + 3
        while self.bus.unsubscribed < 1 and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual((self.bus.subscribed, self.bus.unsubscribed), (1, 1))
        self.bus.emit("agent_progress", agent=2, task_id="demo-b2", lane="claude", tokens=950)   # no dead subscriber

    def test_nested_secrets_are_redacted(self):
        self.bus.emit("swarm_start", run="r2", lanes=[{"name": "relay", "note": f"key {SECRET}"}], usages=[])
        sse = SSE(self.srv.url + "events")
        self.addCleanup(sse.close)
        rows = sse.until("live")
        self.assertEqual(rows[-1]["lanes"][0]["note"], "key [redacted]")
        self.assertNotIn(SECRET, self.get("/api/state")[2].decode())

    def test_tails_the_ledger_without_a_bus(self):
        path = Path(self.tmp) / "events.jsonl"
        old = [{"ts": 1, "event": "swarm_start", "run": "old"}, {"ts": 2, "event": "agent_end", "status": "done"}]
        new = [{"ts": 3, "event": "swarm_start", "run": "new"}, {"ts": 4, "event": "pace", "lane": "claude"}]
        path.write_text("".join(json.dumps(r) + "\n" for r in old + new) + "not json\n")
        srv = self.start(events_path=str(path))
        with mock.patch.dict(sys.modules, {"relay.dash": None}):
            self.assertEqual(json.loads(self.get("/api/state", srv)[2])["run"], "new")
        sse = SSE(srv.url + "events")
        self.addCleanup(sse.close)
        self.assertEqual(sse.msg(), ("reset", {"source": "file", "replay": 2}))
        self.assertEqual([r["run"] for r in sse.until("live")[:1]], ["new"])         # replay starts at the last run
        with path.open("a") as f:                                                     # a row written in two parts
            f.write('{"ts": 5, "event": "agent_start", "task_id": "t5", "text": "' + SECRET)
            f.flush()
            time.sleep(0.4)
            f.write('"}\n')
        event, row = sse.msg()
        self.assertEqual((event, row["event"], row["text"]), ("message", "agent_start", "[redacted]"))
        fresh = Path(self.tmp) / "fresh.jsonl"
        fresh.write_text(json.dumps({"ts": 9, "event": "swarm_start", "run": "fresh"}) + "\n")
        os.replace(fresh, path)                                                       # a new ledger replaces the old
        self.assertEqual(sse.msg()[0], "reset")
        self.assertEqual([r["run"] for r in sse.until("live")], ["fresh"])

    def test_missing_ledger_streams_once_it_appears(self):
        path = Path(self.tmp) / "later" / "events.jsonl"
        srv = self.start(events_path=str(path))
        sse = SSE(srv.url + "events")
        self.addCleanup(sse.close)
        self.assertEqual(sse.msg(), ("reset", {"source": "file", "replay": 0}))
        sse.until("live")
        path.parent.mkdir()
        path.write_text(json.dumps({"ts": 1, "event": "swarm_start", "run": "late"}) + "\n")
        self.assertEqual(sse.msg()[1]["run"], "late")

    def test_feed_works_as_a_bus(self):
        batches = [[{"ts": 1, "session": "s", "event": "swarm_start", "run": "cloud"}],
                   [{"ts": 1, "session": "s", "event": "swarm_start", "run": "cloud"},          # seen again: skipped
                    {"ts": 2, "session": "s", "event": "pace", "lane": "agent37"}]]
        calls = []

        def fetch(since):
            calls.append(since)
            return batches[min(len(calls) - 1, 1)]

        feed = web.Feed(fetch, interval=0.05)
        self.addCleanup(feed.close)
        srv = self.start(bus=feed)
        deadline = time.time() + 3
        while len(calls) < 3 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual([r["event"] for r in feed.rows], ["swarm_start", "pace"])
        self.assertEqual(calls[:3], [0.0, 1.0, 2.0])                 # asks only for rows newer than it has
        sse = SSE(srv.url + "events")
        self.addCleanup(sse.close)
        self.assertEqual([r["event"] for r in sse.until("live")], ["swarm_start", "pace"])


class ShutdownTest(WebCase):
    def test_shutdown_ends_streams_and_frees_the_port(self):
        sse = SSE(self.srv.url + "events")
        sse.until("live")
        url, t0 = self.srv.url, time.time()
        self.srv.shutdown()
        self.assertLess(time.time() - t0, 2)
        while (got := sse.next()) is not None:                       # stream ends (heartbeats may come first)
            self.assertEqual(got[0], "comment")
        sse.close()
        self.assertFalse(self.srv.thread.is_alive())
        with self.assertRaises(urllib.error.URLError):
            DIRECT.open(url + "api/state", timeout=2)
        self.srv.shutdown()                                          # idempotent
        self.assertEqual((self.bus.subscribed, self.bus.unsubscribed), (1, 1))


if __name__ == "__main__":
    unittest.main()
