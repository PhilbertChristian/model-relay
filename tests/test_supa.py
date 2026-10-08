"""relay.supa offline: urlopen is mocked, keys are fake. python3 -m unittest tests.test_supa -v"""
import io
import json
import os
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest import mock

from relay import supa

URL, KEY = "https://abc.supabase.co", "sb_secret_FAKE123456789"
BASE = URL + "/rest/v1/relay_events?"


def setUpModule():
    # safety net: nothing in this file may reach the network, even if a test forgets its own mock
    p = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network call in an offline test"))
    p.start()
    unittest.addModuleCleanup(p.stop)


class Resp(io.BytesIO):
    """Stands in for the http.client.HTTPResponse urlopen returns."""
    def __init__(self, body, status=200):
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.status = status


def row(event, ts=None, session="s1", created_at="2026-10-07T16:20:00.5+00:00", **data):
    """A relay_events row as PostgREST returns it: telemetry pushed the whole JSONL row into `data`."""
    d = {**({"ts": ts} if ts is not None else {}), "session": session, "host": "local", "event": event, **data}
    return {"id": 1, "created_at": created_at, "session": session, "host": "local", "event": event, "model": None,
            "data": d}


class SupaTest(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"SUPABASE_URL": URL + "/", "SUPABASE_KEY": KEY}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def urlopen(self, *responses):
        """Mock urlopen to return (or raise) each response in turn; the mock records every Request."""
        p = mock.patch("urllib.request.urlopen", side_effect=list(responses))
        self.addCleanup(p.stop)
        return p.start()


class Available(SupaTest):
    def test_needs_url_and_a_key_without_network(self):
        m = self.urlopen()
        self.assertTrue(supa.available())
        with mock.patch.dict(os.environ, {"SUPABASE_KEY": ""}):
            self.assertFalse(supa.available())
            with mock.patch.dict(os.environ, {"SUPABASE_SERVICE_ROLE_KEY": KEY}):
                self.assertTrue(supa.available())
        with mock.patch.dict(os.environ, {"SUPABASE_URL": ""}):
            self.assertFalse(supa.available())
            self.assertEqual(supa.recent_events(), [])
            self.assertEqual(supa.runs(), [])
        self.assertEqual(m.call_count, 0)


class RecentEvents(SupaTest):
    def test_query_url_and_headers(self):
        m = self.urlopen(Resp([]))
        self.assertEqual(supa.recent_events(1759870800.5, limit=50, session="abc123"), [])
        req = m.call_args.args[0]
        self.assertEqual(req.full_url, BASE + "data-%3Ets=gt.1759870800.5&session=eq.abc123&order=data-%3Ets.asc&limit=50")
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(req.get_header("Apikey"), KEY)
        self.assertEqual(req.get_header("Authorization"), f"Bearer {KEY}")
        self.assertEqual(req.get_header("Accept"), "application/json")
        self.assertFalse({"Apikey", "Authorization"} & set(req.headers))    # unredirected: never forwarded
        self.assertEqual(m.call_args.kwargs["timeout"], supa.TIMEOUT)

    def test_url_encoding_and_custom_table(self):
        with mock.patch.dict(os.environ, {"SUPABASE_TABLE": "burn_events"}):
            m = self.urlopen(Resp([]))
            supa.recent_events(10, limit=5, session="a b&c=d")
        self.assertEqual(m.call_args.args[0].full_url, URL + "/rest/v1/burn_events?data-%3Ets=gt.10.0"
                                                             "&session=eq.a%20b%26c%3Dd&order=data-%3Ets.asc&limit=5")

    def test_rows_are_shaped_like_the_jsonl_ledger_and_redacted(self):
        self.urlopen(Resp([row("agent_end", 1759870801.25, task_id="t1", summary="done, sk-ant-api03-FAKEFAKEFAKEFAKE12",
                               lanes=[{"note": "token ghp_FAKEFAKEFAKEFAKEFAKE1234"}]), "not a row"]))
        rows = supa.recent_events(1759870800)
        self.assertEqual(rows, [{"ts": 1759870801.25, "session": "s1", "host": "local", "event": "agent_end",
                                 "task_id": "t1", "summary": "done, [redacted]", "lanes": [{"note": "token [redacted]"}]}])
        self.assertEqual(list(rows[0])[:4], ["ts", "session", "host", "event"])

    def test_created_at_fills_a_missing_ts(self):
        self.urlopen(Resp([row("note", created_at="2026-10-07T16:20:00.5+00:00")]))
        [r] = supa.recent_events()
        self.assertEqual(r["ts"], datetime(2026, 10, 7, 16, 20, 0, 500000, tzinfo=timezone.utc).timestamp())
        self.assertEqual(supa._epoch("2026-10-07T16:20:00.123456789Z"),
                         datetime(2026, 10, 7, 16, 20, 0, 123456, tzinfo=timezone.utc).timestamp())
        self.assertEqual(supa._epoch(None), 0.0)

    def test_since_zero_is_the_newest_rows_oldest_first(self):
        m = self.urlopen(Resp([row("b", 20.0), row("a", 10.0)]))
        self.assertEqual([r["event"] for r in supa.recent_events(limit=2)], ["a", "b"])
        self.assertEqual(m.call_args.args[0].full_url, BASE + "order=data-%3Ets.desc.nullslast&limit=2")

    def test_any_failure_is_empty(self):
        denied = urllib.error.HTTPError(BASE, 401, "denied", {}, io.BytesIO(b'{"message":"JWT expired"}'))
        for resp in (denied, urllib.error.URLError("offline"), TimeoutError("slow"), Resp(b"not json"),
                     Resp({"message": "permission denied"})):
            self.urlopen(resp)
            self.assertEqual(supa.recent_events(1.0), [], resp)


class Runs(SupaTest):
    def test_groups_start_and_end_into_newest_first_summaries(self):
        m = self.urlopen(Resp([
            row("swarm_start", 300.0, session="s3", run="r3", max_agents=4, queue=5),
            row("swarm_end", 250.0, session="s2", run="r2", done=3, failed=1, tokens=1000, usd=0.5, seconds=50.0,
                reason="queue empty"),
            row("swarm_start", 200.0, session="s2", run="r2", max_agents=8, queue=4),
            row("swarm_end", 150.0, session="s1", run="r1", done=1)]))
        out = supa.runs(limit=5)
        self.assertEqual(m.call_args.args[0].full_url,
                         BASE + "event=in.%28swarm_start%2Cswarm_end%29&order=data-%3Ets.desc.nullslast&limit=10")
        self.assertEqual([r["run"] for r in out], ["r3", "r2", "r1"])
        r3, r2, r1 = out
        self.assertEqual((r3["status"], r3["started"], r3["ended"], r3["queue"]), ("running", 300.0, None, 5))
        self.assertEqual(r2, {"run": "r2", "started": 200.0, "ended": 250.0, "status": "ended", "session": "s2",
                              "host": "local", "max_agents": 8, "queue": 4, "done": 3, "failed": 1, "tokens": 1000,
                              "usd": 0.5, "seconds": 50.0, "reason": "queue empty"})
        self.assertEqual((r1["status"], r1["started"], r1["ended"]), ("ended", None, 150.0))

    def test_limit_and_failure(self):
        self.urlopen(Resp([row("swarm_start", 2.0, run="b"), row("swarm_start", 1.0, run="a")]),
                     urllib.error.URLError("offline"))
        self.assertEqual([r["run"] for r in supa.runs(limit=1)], ["b"])
        self.assertEqual(supa.runs(), [])


if __name__ == "__main__":
    unittest.main()
