"""relay.monid offline: urlopen is mocked, keys are fake. python3 -m unittest tests.test_monid -v"""
import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from relay import monid
from relay.contracts import ProjectInfo, SwarmTask

FAKE = "monid_live_FAKE123456789"


def setUpModule():
    # safety net: nothing in this file may reach the network, even if a test forgets its own mock
    p = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network call in an offline test"))
    p.start()
    unittest.addModuleCleanup(p.stop)


class Resp(io.BytesIO):
    """Stands in for the http.client.HTTPResponse urlopen returns."""
    def __init__(self, body, status=200, headers=None):
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.status, self.headers = status, headers or {}


def http_error(code, body=b"", headers=None):
    return urllib.error.HTTPError("https://api.monid.ai/v1/research", code, "error", headers or {}, io.BytesIO(body))


def task(text="Add retry with backoff to the uploader", **project):
    return SwarmTask(id="widget-abc123", project=ProjectInfo(**{"path": "/nonexistent/widget", "name": "widget",
                                                                **project}), text=text)


class MonidTest(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"MONID_API_KEY": FAKE}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        monid.limited_until = 0.0
        self.addCleanup(setattr, monid, "limited_until", 0.0)

    def urlopen(self, *responses):
        """Mock urlopen to return (or raise) each response in turn; the mock records every Request."""
        p = mock.patch("urllib.request.urlopen", side_effect=list(responses))
        self.addCleanup(p.stop)
        return p.start()

    def sent(self, m, i=0):
        req = m.call_args_list[i].args[0]
        return req, json.loads(req.data) if req.data else None


class Available(MonidTest):
    def test_needs_key_and_config_without_network(self):
        m = self.urlopen()
        self.assertEqual(monid.available(), (True, "ok"))
        self.assertEqual(monid.available({"monid": {"enabled": True}}), (True, "ok"))
        self.assertFalse(monid.available({"monid": {"enabled": False}})[0])
        self.assertFalse(monid.available({"monid": False})[0])
        with mock.patch.dict(os.environ, {"MONID_API_KEY": ""}):
            ok, why = monid.available({"monid": {"enabled": True}})
            self.assertFalse(ok)
            self.assertIn("MONID_API_KEY", why)
        self.assertEqual(m.call_count, 0)

    def test_off_means_no_calls_and_empty_results(self):
        m = self.urlopen()
        self.assertIsNone(monid.enricher({"monid": {"enabled": False}}))
        with mock.patch.dict(os.environ, {"MONID_API_KEY": ""}):
            self.assertIsNone(monid.enricher({}))
            self.assertEqual(monid.research(task()), "")
            self.assertIsNone(monid.credits())
            self.assertEqual(monid.tools(), [])
        self.assertEqual(m.call_count, 0)


class Research(MonidTest):
    def test_posts_a_short_redacted_query_and_returns_bullets(self):
        m = self.urlopen(Resp({"notes": ["Use exponential backoff.", "Cap retries at 5."]}))
        text = "Add retry to the uploader; key sk-ant-api03-SHOULDNOTSEND0123456789 " + "x" * 2000
        brief = monid.research(task(text))
        self.assertEqual(brief, "- Use exponential backoff.\n- Cap retries at 5.")
        req, body = self.sent(m)
        self.assertEqual((req.full_url, req.get_method()), ("https://api.monid.ai/v1/research", "POST"))
        self.assertEqual(req.get_header("Authorization"), f"Bearer {FAKE}")
        self.assertNotIn("Authorization", req.headers)       # unredirected: never forwarded on a redirect
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(m.call_args.kwargs["timeout"], 30)
        self.assertEqual(list(body), ["query"])
        self.assertTrue(body["query"].startswith("widget: Add retry to the uploader; key [redacted] xxx"))
        self.assertEqual(len(body["query"]), 1000)
        self.assertNotIn("SHOULDNOTSEND", req.data.decode())

    def test_no_file_contents_paths_or_brief_in_the_query(self):
        m = self.urlopen(Resp({"notes": ["ok"]}))
        with tempfile.TemporaryDirectory() as d:
            Path(d, "README.md").write_text("FILE_CONTENTS_MARKER")
            Path(d, ".env").write_text("ENV_MARKER=1")
            t = task("Fix the flaky upload test", path=d, todos=["TODO_MARKER"], test_cmd="pytest",
                     remote="https://x-access-token:REMOTE_MARKER@github.com/me/widget")
            t.brief = "OLD_BRIEF_MARKER"
            self.assertEqual(monid.research(t), "- ok")
        req, body = self.sent(m)
        self.assertEqual(body, {"query": "widget: Fix the flaky upload test"})
        for marker in ("FILE_CONTENTS", "ENV_MARKER", "TODO_MARKER", "REMOTE_MARKER", "OLD_BRIEF", "pytest", d):
            self.assertNotIn(marker, req.data.decode() + req.full_url)

    def test_keeps_three_short_redacted_notes(self):
        leak = "leak monid_live_ABCDEFGH12345 ctxt_secret_ABCDEFGH123 Bearer tok.en.value done"
        self.urlopen(Resp({"notes": ["alpha", leak, "n" * 600, "fourth is dropped", 12]}))
        lines = monid.research(task()).splitlines()
        self.assertEqual(lines[0], "- alpha")
        self.assertEqual(lines[1], "- leak [redacted] [redacted] [redacted] done")
        self.assertEqual(lines[2], "- " + "n" * 500)
        self.assertEqual(len(lines), 3)

    def test_limit_backs_off_for_an_hour_without_retrying(self):
        m = self.urlopen(http_error(429, b'{"error":"rate limit"}'), Resp({"notes": ["back"]}))
        t0 = time.time()
        self.assertEqual(monid.research(task()), "")
        self.assertTrue(monid.is_limited())
        self.assertAlmostEqual(monid.limited_until, t0 + monid.LIMIT_BACKOFF, delta=5)
        self.assertEqual(monid.research(task()), "")              # skipped: no second call
        self.assertEqual(m.call_count, 1)
        monid.limited_until = time.time() - 1                    # the hour is over
        self.assertEqual(monid.research(task()), "- back")
        self.assertEqual(m.call_count, 2)

    def test_402_limit_bodies_and_long_retry_after_count_as_limits(self):
        for resp in (http_error(402, b"payment required"), Resp({"error": "rate-limit exceeded"}),
                     http_error(403, b"usage limit reached for this month")):
            monid.limited_until = 0.0
            self.urlopen(resp)
            self.assertEqual(monid.research(task()), "")
            self.assertTrue(monid.is_limited(), resp)
        monid.limited_until = 0.0
        self.urlopen(http_error(429, headers={"Retry-After": "7200"}))
        monid.research(task())
        self.assertGreater(monid.limited_until, time.time() + 7000)

    def test_any_failure_is_an_empty_brief(self):
        for resp in (urllib.error.URLError("offline"), TimeoutError("slow"), http_error(500, b"internal error"),
                     Resp(b"<html>not json</html>"), Resp({"notes": "not a list"}), Resp([1, 2])):
            self.urlopen(resp)
            self.assertEqual(monid.research(task()), "", resp)
            self.assertFalse(monid.is_limited(), resp)
        broken = SwarmTask(id="x", project=None, text="t")      # never raises, even on a malformed task
        self.assertEqual(monid.research(broken), "")


class Enricher(MonidTest):
    def test_sets_the_brief_once_per_task(self):
        m = self.urlopen(Resp({"notes": ["Read the retry docs."]}))
        enrich = monid.enricher({"monid": {"enabled": True, "timeout": 5}})
        t = task()
        self.assertEqual(enrich(t), "- Read the retry docs.")
        self.assertEqual(t.brief, "- Read the retry docs.")
        self.assertEqual(m.call_args.kwargs["timeout"], 5)
        self.assertEqual(enrich(t), "- Read the retry docs.")    # a retry reuses it: no second paid call
        self.assertEqual(m.call_count, 1)

    def test_failure_leaves_the_brief_empty(self):
        self.urlopen(urllib.error.URLError("offline"))
        t = task()
        self.assertEqual(monid.enricher({})(t), "")
        self.assertEqual(t.brief, "")


class Credits(MonidTest):
    def test_live_usage(self):
        m = self.urlopen(Resp({"used": 120, "limit": 1000, "resets_at": "2026-11-01T00:00:00Z"}))
        u = monid.credits()
        self.assertEqual((u.name, u.lane, u.unit, u.used, u.limit, u.source),
                         ("monid", "monid", "credits", 120.0, 1000.0, "live"))
        self.assertEqual(u.resets_at, datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp())
        req, _ = self.sent(m)
        self.assertEqual((req.full_url, req.get_method()), ("https://api.monid.ai/v1/credits", "GET"))
        self.assertEqual(req.get_header("Authorization"), f"Bearer {FAKE}")

    def test_remaining_only_shapes(self):
        self.urlopen(Resp({"remaining": 880, "limit": 1000, "resets_at": 1793491200}),
                     Resp({"remaining": 50}))
        u = monid.credits()
        self.assertEqual((u.used, u.limit, u.remaining, u.resets_at), (120.0, 1000.0, 880.0, 1793491200.0))
        u = monid.credits()
        self.assertEqual((u.used, u.limit), (0.0, 50.0))
        self.assertGreater(u.resets_at, time.time())
        self.assertIn("assuming", u.note)

    def test_failure_is_none(self):
        for resp in (http_error(500), urllib.error.URLError("offline"), Resp(b"nope"), Resp({"plan": "free"})):
            self.urlopen(resp)
            self.assertIsNone(monid.credits(), resp)


class Tools(MonidTest):
    def test_names_from_either_shape(self):
        with mock.patch.dict(os.environ, {"MONID_BASE_URL": "http://127.0.0.1:8787/"}):
            m = self.urlopen(Resp({"tools": [{"name": "web_search"}, {"name": "github"}, "docs", {"id": 3}]}),
                             Resp(["a", "b"]))
            self.assertEqual(monid.tools(), ["web_search", "github", "docs"])
            self.assertEqual(monid.tools(), ["a", "b"])
        req, _ = self.sent(m)
        self.assertEqual((req.full_url, req.get_method()), ("http://127.0.0.1:8787/v1/tools", "GET"))

    def test_failure_is_empty(self):
        for resp in (http_error(401, b"bad key"), urllib.error.URLError("offline"), Resp({"tools": "web"})):
            self.urlopen(resp)
            self.assertEqual(monid.tools(), [], resp)


if __name__ == "__main__":
    unittest.main()
