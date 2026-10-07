"""python3 -m unittest tests.test_serve -v

The OpenAI-compatible proxy, end to end over real sockets on 127.0.0.1 (no external network):
mock providers for the ladder, and a tiny local fake for the OpenAI-compatible upstream path.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from relay.serve import AUTO, add_arguments, make_server

ROOT = Path(__file__).resolve().parent.parent
SCRUB = ("RELAY_SERVE_KEY", "SUPABASE_URL", "SUPABASE_KEY", "SUPABASE_SERVICE_ROLE_KEY", "AGENT37_API_KEY",
         "RELAY_CONFIG", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
TOOL = {"type": "function", "function": {"name": "bash", "description": "run a shell command",
                                         "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}}}


def mock_cfg(**over):
    """m-flaky is cheapest but always rate-limited (fail_after 0); m-solid answers; m-big is tier 1."""
    cfg = {
        "base_cooldown": 30,
        "providers": {
            "a": {"kind": "mock", "mock": {"delay": 0, "models": {
                "flaky": {"fail_after": 0, "fail_with": "rate_limit"}}}},
            "b": {"kind": "mock", "mock": {"delay": 0, "models": {
                "solid": {"behaviour": "solve", "hint": "pong from solid", "plan": [["bash", {"command": "echo hi"}]]},
                "big": {"behaviour": "solve", "hint": "pong from big"}}}},
        },
        "models": [
            {"id": "m-flaky", "provider": "a", "model": "flaky", "tier": 0, "price_in": 0.10, "price_out": 0.40},
            {"id": "m-solid", "provider": "b", "model": "solid", "tier": 0, "price_in": 0.20, "price_out": 0.80},
            {"id": "m-big", "provider": "b", "model": "big", "tier": 1, "price_in": 2.00, "price_out": 8.00},
        ],
    }
    cfg.update(over)
    return cfg


def failing_cfg(kind, windows=(128000, 128000)):
    return {"base_cooldown": 30,
            "providers": {"a": {"kind": "mock", "mock": {"delay": 0, "models": {"x": {"fail_after": 0, "fail_with": kind}}}},
                          "b": {"kind": "mock", "mock": {"delay": 0, "models": {"y": {"fail_after": 0, "fail_with": kind}}}}},
            "models": [{"id": "mx", "provider": "a", "model": "x", "tier": 0, "price_in": 0.1, "context_window": windows[0]},
                       {"id": "my", "provider": "b", "model": "y", "tier": 0, "price_in": 0.2, "context_window": windows[1]}]}


def parse_sse(text):
    """Strict-ish SSE reader: every event is `data: ...` lines (comments allowed), blank-line separated."""
    datas = []
    for event in text.split("\n\n"):
        for line in event.splitlines():
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data: "):
                raise AssertionError(f"not an SSE data line: {line!r}")
            datas.append(line[len("data: "):])
    return datas


def chat(**kw):
    body = {"model": AUTO, "messages": [{"role": "user", "content": "say pong"}]}
    body.update(kw)
    return body


class ServeCase(unittest.TestCase):
    """Fresh proxy per test on an ephemeral port, own temp ledger, scrubbed env."""

    def setUp(self):
        env = mock.patch.dict(os.environ, {})
        env.start()
        self.addCleanup(env.stop)
        for k in SCRUB:
            os.environ.pop(k, None)
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
        self.dir = tempfile.mkdtemp()

    def start(self, cfg=None, **kw):
        self.srv = make_server(cfg or mock_cfg(), "127.0.0.1", 0, self.dir, include_claude=False, **kw)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.root = self.srv.base_url[: -len("/v1")]
        return self.srv

    def call(self, method, path, body=None, headers=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.root + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with DIRECT.open(req, timeout=15) as r:
                return r.status, r.headers, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode()

    def ledger(self):
        p = Path(self.dir) / ".relay" / "events.jsonl"
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


class Serve(ServeCase):
    def setUp(self):
        super().setUp()
        self.start()

    def test_models_lists_alias_then_ladder(self):
        status, _, text = self.call("GET", "/v1/models")
        self.assertEqual(status, 200)
        body = json.loads(text)
        self.assertEqual(body["object"], "list")
        ids = [m["id"] for m in body["data"]]
        self.assertEqual(ids[0], AUTO)
        for want in ("m-flaky", "m-solid", "m-big", "relay/tier-0", "relay/tier-1"):
            self.assertIn(want, ids)
        self.assertTrue(all(m["object"] == "model" and "owned_by" in m for m in body["data"]))
        status, _, text = self.call("GET", "/v1/models/relay/auto")
        self.assertEqual((status, json.loads(text)["id"]), (200, AUTO))
        self.assertEqual(self.call("GET", "/v1/models/nope")[0], 404)

    def test_completion_fails_over_and_says_so(self):
        status, headers, text = self.call("POST", "/v1/chat/completions", chat(max_tokens=64, temperature=0.2,
                                                                                  tool_choice="auto"))
        self.assertEqual(status, 200, text)
        self.assertEqual(headers["X-Relay-Model"], "m-solid")
        self.assertGreaterEqual(int(headers["X-Relay-Switches"]), 1)
        self.assertEqual(headers["X-Relay-Attempts"], "2")
        self.assertGreater(float(headers["X-Relay-Usd"]), 0)
        body = json.loads(text)
        self.assertEqual(body["object"], "chat.completion")
        self.assertTrue(body["id"].startswith("chatcmpl-"))
        self.assertEqual(body["model"], "solid")   # the provider model actually used
        choice = body["choices"][0]
        self.assertEqual(choice["message"], {"role": "assistant", "content": "pong from solid"})
        self.assertEqual(choice["finish_reason"], "stop")
        u = body["usage"]
        self.assertEqual(u["total_tokens"], u["prompt_tokens"] + u["completion_tokens"])
        self.assertGreater(u["completion_tokens"], 0)
        # flaky is cooling now: the next request goes straight to solid
        status, headers, _ = self.call("POST", "/v1/chat/completions", chat())
        self.assertEqual((status, headers["X-Relay-Model"], headers["X-Relay-Switches"]), (200, "m-solid", "0"))

    def test_tool_calls_round_trip(self):
        status, headers, text = self.call("POST", "/chat/completions", chat(tools=[TOOL]))   # no /v1 prefix
        self.assertEqual(status, 200, text)
        choice = json.loads(text)["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertIsNone(choice["message"]["content"])
        tc = choice["message"]["tool_calls"][0]
        self.assertEqual((tc["type"], tc["function"]["name"]), ("function", "bash"))
        self.assertEqual(json.loads(tc["function"]["arguments"]), {"command": "echo hi"})

    def test_pinned_model_starts_there_and_still_fails_over(self):
        _, h, _ = self.call("POST", "/v1/chat/completions", chat(model="m-flaky"))
        self.assertEqual((h["X-Relay-Model"], h["X-Relay-Switches"]), ("m-solid", "1"))
        _, h, text = self.call("POST", "/v1/chat/completions", chat(model="m-big"))
        self.assertEqual((h["X-Relay-Model"], h["X-Relay-Switches"], json.loads(text)["model"]), ("m-big", "0", "big"))
        _, h, _ = self.call("POST", "/v1/chat/completions", chat(model="relay/tier-1"))
        self.assertEqual(h["X-Relay-Model"], "m-big")
        _, h, _ = self.call("POST", "/v1/chat/completions", chat(model="openai/relay/auto"))  # client-side prefix
        self.assertEqual(h["X-Relay-Model"], "m-solid")
        _, h, _ = self.call("POST", "/v1/chat/completions", chat(model="gpt-4o"))   # unknown: the router picks
        self.assertEqual(h["X-Relay-Model"], "m-solid")

    def test_stream_is_valid_sse_ending_in_done(self):
        status, headers, text = self.call("POST", "/v1/chat/completions", chat(stream=True))
        self.assertEqual(status, 200, text)
        self.assertTrue(headers["Content-Type"].startswith("text/event-stream"))
        self.assertEqual(headers["X-Relay-Model"], "m-solid")
        self.assertGreaterEqual(int(headers["X-Relay-Switches"]), 1)
        datas = parse_sse(text)
        self.assertEqual(datas[-1], "[DONE]")
        chunks = [json.loads(d) for d in datas[:-1]]
        self.assertTrue(chunks)
        self.assertTrue(all(c["object"] == "chat.completion.chunk" and c["model"] == "solid" for c in chunks))
        self.assertEqual(len({c["id"] for c in chunks}), 1)
        content = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks if c["choices"])
        self.assertEqual(content, "pong from solid")
        self.assertEqual(chunks[0]["choices"][0]["delta"]["role"], "assistant")
        finishes = [c["choices"][0]["finish_reason"] for c in chunks if c["choices"] and c["choices"][0]["finish_reason"]]
        self.assertEqual(finishes, ["stop"])

    def test_stream_tool_calls_carry_index_and_usage(self):
        status, _, text = self.call("POST", "/v1/chat/completions",
                                    chat(stream=True, tools=[TOOL], stream_options={"include_usage": True}))
        self.assertEqual(status, 200, text)
        datas = parse_sse(text)
        self.assertEqual(datas[-1], "[DONE]")
        chunks = [json.loads(d) for d in datas[:-1]]
        tcs = [tc for c in chunks if c["choices"] for tc in c["choices"][0]["delta"].get("tool_calls", [])]
        self.assertEqual(len(tcs), 1)
        self.assertEqual((tcs[0]["index"], tcs[0]["function"]["name"]), (0, "bash"))
        self.assertIn("finish_reason", chunks[-2]["choices"][0])
        self.assertEqual(chunks[-2]["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(chunks[-1]["choices"], [])
        self.assertGreater(chunks[-1]["usage"]["total_tokens"], 0)

    def test_ledger_gets_call_rows_and_savings_counts_them(self):
        self.call("POST", "/v1/chat/completions", chat())
        rows = self.ledger()
        calls = [r for r in rows if r["event"] == "call"]
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual((c["model"], c["provider"], c["tier"]), ("m-solid", "b", 0))
        self.assertGreater(c["input_tokens"], 0)
        self.assertGreater(c["output_tokens"], 0)
        self.assertAlmostEqual(c["usd"], (c["input_tokens"] * 0.20 + c["output_tokens"] * 0.80) / 1e6)
        self.assertTrue(all(k in c for k in ("ts", "session", "host")))
        errs = [r for r in rows if r["event"] == "provider_error"]
        self.assertEqual((errs[0]["model"], errs[0]["kind"], errs[0]["status"]), ("m-flaky", "rate_limit", 429))
        sw = [r for r in rows if r["event"] == "switch"]
        self.assertEqual((sw[0]["old"], sw[0]["model"]), ("m-flaky", "m-solid"))
        self.assertTrue(sw[0]["reason"].startswith("rate_limit"))
        self.assertTrue(any(r["event"] == "serve_start" for r in rows))
        # relay.value prices the month from those rows
        status, _, text = self.call("GET", "/v1/relay/savings")
        self.assertEqual(status, 200)
        s = json.loads(text)
        self.assertGreater(s["used_usd"], 0)
        self.assertIn("m-solid", s["by_model"])
        self.assertIn("Relay", s["by_source"])
        self.assertFalse(s["include_claude"])
        self.assertEqual(self.call("GET", "/v1/relay/savings?month=2020-01")[0], 200)
        self.assertEqual(self.call("GET", "/v1/relay/savings?month=soon")[0], 400)

    def test_health_status_and_capacity(self):
        status, _, text = self.call("GET", "/health")
        self.assertEqual((status, json.loads(text)["status"]), (200, "ok"))
        self.assertEqual(json.loads(self.call("GET", "/v1/relay/capacity")[2]), [])
        self.call("POST", "/v1/chat/completions", chat())
        st = json.loads(self.call("GET", "/v1/relay/status")[2])
        by_id = {m["id"]: m for m in st["models"]}
        self.assertEqual(by_id["m-flaky"]["state"], "cooling")
        self.assertEqual((by_id["m-solid"]["state"], by_id["m-solid"]["calls"]), ("ok", 1))

    def test_bad_requests_get_openai_errors(self):
        status, _, text = self.call("POST", "/v1/chat/completions", raw=b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(text)["error"]["type"], "invalid_request_error")
        self.assertEqual(self.call("POST", "/v1/chat/completions", {"model": AUTO, "messages": []})[0], 400)
        self.assertEqual(self.call("POST", "/v1/chat/completions", {"messages": ["hi"]})[0], 400)
        self.assertEqual(self.call("GET", "/v1/chat/completions")[0], 405)
        status, _, text = self.call("POST", "/v1/responses", chat())
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(text))

    def test_shape_errors_are_400_before_any_spend(self):
        status, _, text = self.call("POST", "/v1/chat/completions", chat(stream=True, stream_options=True))
        self.assertEqual(status, 400, text)
        self.assertEqual(json.loads(text)["error"]["param"], "stream_options")
        self.assertFalse([r for r in self.ledger() if r["event"] in ("call", "provider_error")])
        self.assertEqual(self.call("PUT", "/v1/chat/completions", chat())[0], 405)
        status, _, text = self.call("DELETE", "/v1/nothing")
        self.assertEqual((status, json.loads(text)["error"]["code"]), (404, "not_found"))

    def test_dns_rebinding_host_is_refused_without_a_key(self):
        evil = {"Host": "attacker.example:8037"}
        status, _, text = self.call("GET", "/v1/relay/status", headers=evil)
        self.assertEqual((status, json.loads(text)["error"]["code"]), (403, "host_not_allowed"))
        self.assertEqual(self.call("GET", "/v1/relay/savings", headers=evil)[0], 403)
        self.assertEqual(self.call("GET", "/health", headers=evil)[0], 200)
        for ok in ("localhost:8037", "127.0.0.1", "[::1]:8037", "host.docker.internal:8037", "mybox.local", "mybox"):
            self.assertEqual(self.call("GET", "/v1/models", headers={"Host": ok})[0], 200, ok)

    def test_browser_origins_other_than_loopback_are_refused(self):
        self.assertEqual(self.call("GET", "/v1/models", headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.call("POST", "/v1/chat/completions", chat(), {"Origin": "http://evil.example"})[0], 403)
        status, headers, _ = self.call("GET", "/v1/models", headers={"Origin": "http://localhost:5173"})
        self.assertEqual((status, headers["Access-Control-Allow-Origin"]), (200, "http://localhost:5173"))
        status, headers, _ = self.call("OPTIONS", "/v1/chat/completions", headers={
            "Origin": "http://127.0.0.1:3000", "Access-Control-Request-Headers": "authorization, content-type"})
        self.assertEqual(status, 204)
        self.assertIn("X-Relay-Model", headers["Access-Control-Expose-Headers"])


class Auth(ServeCase):
    def test_key_from_env_is_enforced(self):
        os.environ["RELAY_SERVE_KEY"] = "sekret-test-key"
        self.start()
        self.assertEqual(self.call("GET", "/v1/models")[0], 401)
        status, _, text = self.call("POST", "/v1/chat/completions", chat())
        self.assertEqual((status, json.loads(text)["error"]["code"]), (401, "invalid_api_key"))
        self.assertEqual(self.call("GET", "/v1/models", headers={"Authorization": "Bearer wrong"})[0], 401)
        ok = {"Authorization": "Bearer sekret-test-key"}
        self.assertEqual(self.call("GET", "/v1/models", headers=ok)[0], 200)
        status, headers, _ = self.call("POST", "/v1/chat/completions", chat(), ok)
        self.assertEqual((status, headers["X-Relay-Model"]), (200, "m-solid"))
        self.assertEqual(self.call("GET", "/v1/models", headers={"X-Api-Key": "sekret-test-key"})[0], 200)
        self.assertEqual(self.call("GET", "/health")[0], 200)   # liveness stays open
        # with a key, a browser origin is allowed through to the key check
        self.assertEqual(self.call("GET", "/v1/models", headers={"Origin": "http://evil.example"})[0], 401)
        self.assertEqual(self.call("GET", "/v1/models", headers={"Origin": "http://evil.example", **ok})[0], 200)
        # and so is any Host name (a TLS reverse proxy in front, say)
        self.assertEqual(self.call("GET", "/v1/models", headers={"Host": "relay.example.com", **ok})[0], 200)

    def test_no_key_means_open_on_loopback(self):
        self.start()
        self.assertIsNone(self.srv.proxy.key)
        self.assertEqual(self.call("GET", "/v1/models", headers={"Authorization": "Bearer anything"})[0], 200)


class Capacity(ServeCase):
    def test_capacity_lists_subscriptions_as_json(self):
        self.start(mock_cfg(
            idle={"timezone": "UTC", "weekday": "all", "weekend": "all"},
            subscriptions=[{"name": "b", "kind": "api_budget", "providers": ["b"], "monthly_usd": 10.0,
                            "reserve_pct": 0}]))
        self.call("POST", "/v1/chat/completions", chat())   # spend lands in the ledger the assessment reads
        status, _, text = self.call("GET", "/v1/relay/capacity")
        self.assertEqual(status, 200, text)
        caps = json.loads(text)
        self.assertEqual([(c["name"], c["kind"], c["unit"]) for c in caps], [("b", "api_budget", "usd")])
        self.assertLess(caps[0]["remaining"], 10.0)
        self.assertRegex(caps[0]["resets_at"], r"^\d{4}-\d{2}-\d{2}T")
        self.assertGreaterEqual(caps[0]["hours_to_reset"], 0)


class Failure(ServeCase):
    def test_every_model_failing_is_503_with_router_status(self):
        self.start(failing_cfg("budget"))
        status, _, text = self.call("POST", "/v1/chat/completions", chat())
        self.assertEqual(status, 503, text)
        err = json.loads(text)["error"]
        self.assertEqual((err["type"], err["code"]), ("server_error", "relay_no_model_available"))
        self.assertIn("no model available", err["message"])
        self.assertIn("dead provider", err["message"])   # Router.status_line()
        self.assertEqual([a["kind"] for a in err["relay"]["attempts"]], ["budget", "budget"])
        self.assertTrue(any(r["event"] == "abort" for r in self.ledger()))

    def test_cooling_ladder_is_503_with_retry_after(self):
        cfg = failing_cfg("rate_limit")
        self.start(cfg, max_wait=0)
        status, headers, text = self.call("POST", "/v1/chat/completions", chat())
        self.assertEqual(status, 503, text)
        self.assertEqual(json.loads(text)["error"]["code"], "relay_models_cooling")
        self.assertGreaterEqual(int(headers["Retry-After"]), 1)

    def test_context_overflow_everywhere_asks_the_harness_to_compact(self):
        self.start(failing_cfg("context", windows=(1000, 2000)))
        status, _, text = self.call("POST", "/v1/chat/completions", chat())
        self.assertEqual(status, 400, text)
        err = json.loads(text)["error"]
        self.assertEqual(err["code"], "context_length_exceeded")
        self.assertIn("maximum context length", err["message"])
        # the overflow was this request's problem only: the ladder is not narrowed for the next one
        self.assertEqual(self.srv.proxy.router.min_context, 0)

    def test_bounded_retries(self):
        self.start(failing_cfg("transient"), max_attempts=1)
        status, _, text = self.call("POST", "/v1/chat/completions", chat())
        self.assertEqual(status, 503)
        self.assertEqual(len(json.loads(text)["error"]["relay"]["attempts"]), 1)

    def test_provider_pointing_back_at_the_proxy_is_disabled_not_looped(self):
        # `export OPENAI_BASE_URL=<this proxy>` for the harness, then start relay serve in the same shell
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        os.environ["RELAY_TEST_LOOP_URL"] = f"http://localhost:{port}/v1"
        self.srv = make_server({"providers": {"openai": {"base_url": "${RELAY_TEST_LOOP_URL:-https://api.openai.com/v1}"}},
                                "models": [{"id": "oai", "provider": "openai", "model": "gpt-x"}]},
                               "127.0.0.1", port, self.dir, include_claude=False, max_wait=0)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.root = self.srv.base_url[: -len("/v1")]
        self.assertIn("loop back", self.srv.proxy.router.dead_providers["openai"])
        status, _, text = self.call("POST", "/v1/chat/completions", chat())
        self.assertEqual((status, json.loads(text)["error"]["code"]), (503, "relay_no_model_available"))
        self.assertIn("loop back", json.loads(text)["error"]["message"])
        self.assertFalse([r for r in self.ledger() if r["event"] in ("call", "provider_error")])


class _FakeOpenAI(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen.append(body)
        if getattr(self.server, "reject_tool_order", False) and any(m.get("role") == "tool" for m in body["messages"]):
            status, out = 400, {"error": {
                "message": "Invalid parameter: messages with role 'tool' must be a response to a preceeding "
                           "message with 'tool_calls'.",
                "type": "invalid_request_error", "param": "messages.[1].role", "code": None}}
        elif self.server.reject_temperature and "temperature" in body:
            status, out = 400, {"error": {
                "message": "Unsupported value: 'temperature' does not support 0.2 with this model. "
                           "Only the default (1) value is supported.",
                "type": "invalid_request_error", "param": "temperature", "code": "unsupported_value"}}
        else:
            status, out = 200, {
                "id": "chatcmpl-up", "object": "chat.completion", "created": 1, "model": body["model"],
                "choices": [{"index": 0, "finish_reason": "length",
                             "message": {"role": "assistant", "content": "upstream says hi", "refusal": None}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
                          "prompt_tokens_details": {"cached_tokens": 3}}}
        data = json.dumps(out).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Passthrough(ServeCase):
    """kind "openai" providers get the client's OpenAI fields, not just messages + tools."""

    def setUp(self):
        super().setUp()
        self.up = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAI)
        self.up.seen, self.up.reject_temperature = [], False
        threading.Thread(target=self.up.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.up.server_close)
        self.addCleanup(self.up.shutdown)
        self.start({"providers": {"up": {"base_url": f"http://127.0.0.1:{self.up.server_address[1]}/v1"}},
                    "models": [{"id": "up-main", "provider": "up", "model": "gpt-test", "tier": 0,
                                "price_in": 1.0, "price_out": 2.0}]})

    def test_forwards_openai_fields_and_keeps_finish_reason(self):
        status, headers, text = self.call("POST", "/v1/chat/completions", chat(
            tools=[TOOL], tool_choice="auto", temperature=0.2, max_tokens=50, stream=False,
            response_format={"type": "text"}))
        self.assertEqual(status, 200, text)
        sent = self.up.seen[0]
        self.assertEqual(sent["model"], "gpt-test")
        self.assertEqual((sent["tool_choice"], sent["temperature"], sent["max_completion_tokens"]), ("auto", 0.2, 50))
        self.assertEqual(sent["response_format"], {"type": "text"})
        self.assertNotIn("max_tokens", sent)
        self.assertNotIn("stream", sent)
        body = json.loads(text)
        self.assertEqual((body["model"], body["choices"][0]["finish_reason"]), ("gpt-test", "length"))
        self.assertEqual(body["choices"][0]["message"]["content"], "upstream says hi")
        self.assertEqual(body["usage"]["prompt_tokens_details"], {"cached_tokens": 3})
        self.assertEqual((body["usage"]["prompt_tokens"], body["usage"]["total_tokens"]), (11, 18))
        self.assertEqual(headers["X-Relay-Model"], "up-main")
        call = [r for r in self.ledger() if r["event"] == "call"][0]
        self.assertAlmostEqual(call["usd"], (11 * 1.0 + 7 * 2.0) / 1e6)

    def test_rejected_temperature_is_dropped_not_fatal(self):
        self.up.reject_temperature = True
        status, headers, text = self.call("POST", "/v1/chat/completions", chat(temperature=0.2))
        self.assertEqual(status, 200, text)
        self.assertEqual(len(self.up.seen), 2)
        self.assertIn("temperature", self.up.seen[0])
        self.assertNotIn("temperature", self.up.seen[1])
        self.assertEqual((headers["X-Relay-Switches"], headers["X-Relay-Attempts"]), ("0", "1"))
        state = json.loads(self.call("GET", "/v1/relay/status")[2])["models"][0]
        self.assertEqual(state["state"], "ok")   # not retired as a "bad model"


class Rejected(ServeCase):
    """An upstream 400 about the request itself must not cool the shared ladder or turn into a retryable 503."""

    def test_malformed_request_is_400_and_leaves_the_ladder_warm(self):
        up = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAI)
        up.seen, up.reject_temperature, up.reject_tool_order = [], False, True
        threading.Thread(target=up.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(up.server_close)
        self.addCleanup(up.shutdown)
        url = f"http://127.0.0.1:{up.server_address[1]}/v1"
        self.start({"providers": {"p1": {"base_url": url}, "p2": {"base_url": url}},
                    "models": [{"id": "m1", "provider": "p1", "model": "one", "price_in": 0.1},
                               {"id": "m2", "provider": "p2", "model": "two", "price_in": 0.2}]})
        bad = chat(messages=[{"role": "user", "content": "hi"}, {"role": "tool", "tool_call_id": "x", "content": "?"}])
        status, _, text = self.call("POST", "/v1/chat/completions", bad)
        self.assertEqual(status, 400, text)
        err = json.loads(text)["error"]
        self.assertEqual((err["type"], err["param"]), ("invalid_request_error", "messages.[1].role"))
        self.assertIn("must be a response to a preceeding message", err["message"])
        self.assertEqual([a["model"] for a in err["relay"]["attempts"]], ["m1", "m2"])   # other models got a fair try
        self.assertEqual([m["model"] for m in up.seen], ["one", "two"])
        # nothing cooled, nothing retired: the next (valid) request is served at once by the cheapest model
        st = {m["id"]: m for m in json.loads(self.call("GET", "/v1/relay/status")[2])["models"]}
        self.assertEqual({k: (v["state"], v["errors"]) for k, v in st.items()}, {"m1": ("ok", 0), "m2": ("ok", 0)})
        status, headers, _ = self.call("POST", "/v1/chat/completions", chat())
        self.assertEqual((status, headers["X-Relay-Model"], headers["X-Relay-Switches"]), (200, "m1", "0"))
        rows = [r for r in self.ledger() if r["event"] == "provider_error"]
        self.assertTrue(rows and all(r.get("rejected") for r in rows))


class Cli(unittest.TestCase):
    def test_banner_prints_base_url_and_server_answers(self):
        d = tempfile.mkdtemp()
        cfg = Path(d) / "relay.json"
        cfg.write_text(json.dumps(mock_cfg()))
        env = {k: v for k, v in os.environ.items() if k not in SCRUB}
        env.update(PYTHONUNBUFFERED="1", NO_COLOR="1")
        proc = subprocess.Popen([sys.executable, "-m", "relay.serve", "-c", str(cfg), "--port", "0", "--cwd", d,
                                 "--no-claude", "-q"], cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        try:
            first = []
            reader = threading.Thread(target=lambda: first.append(proc.stdout.readline()), daemon=True)
            reader.start()
            reader.join(15)
            self.assertTrue(first and "base_url" in first[0], f"no banner: {first}")
            self.assertEqual(len(first[0].strip().splitlines()), 1)
            base = re.search(r"base_url (\S+)", first[0]).group(1)
            self.assertRegex(base, r"^http://127\.0\.0\.1:\d+/v1$")
            with DIRECT.open(base + "/models", timeout=10) as r:
                self.assertIn(AUTO, [m["id"] for m in json.loads(r.read())["data"]])
        finally:
            proc.terminate()
            proc.communicate(timeout=10)
        self.assertTrue((Path(d) / ".relay" / "events.jsonl").exists())

    def test_wiring_into_relay_cli_keeps_global_flags(self):
        ap = argparse.ArgumentParser(prog="relay")
        ap.add_argument("-c", "--config")
        ap.add_argument("-C", "--cwd", default=".")
        ap.add_argument("--tier", type=int)
        add_arguments(ap.add_subparsers(dest="cmd").add_parser("serve"))
        a = ap.parse_args(["-c", "x.json", "-C", "/tmp", "serve", "--port", "9000"])
        self.assertEqual((a.cmd, a.config, a.cwd, a.port, a.host, a.no_claude), ("serve", "x.json", "/tmp", 9000,
                                                                                 "127.0.0.1", False))
        b = ap.parse_args(["serve", "-c", "y.json", "--no-claude", "--tier", "1"])
        self.assertEqual((b.config, b.cwd, b.tier, b.no_claude), ("y.json", ".", 1, True))


if __name__ == "__main__":
    unittest.main()
