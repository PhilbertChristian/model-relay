"""OpenAI-compatible proxy: point any agent harness at Relay by changing only its base_url.

    python3 -m relay.serve -c relay.json          (or `relay serve` once wired into the CLI)
    -> base_url http://127.0.0.1:8037/v1   model "relay/auto"

Anything that speaks the Chat Completions API (OpenAI SDKs, Aider, OpenCode, Cline, Continue,
LiteLLM, Codex with wire_api = "chat", ...) gets the whole ladder: each request goes to the
Router's pick, and when a provider fails (rate limit, budget, auth, context overflow, 5xx) the
model is cooled down or retired exactly as in `relay run`, and the request is retried on the
next pick. The harness only ever sees one ordinary response. An upstream 400/413/422 about the
request itself (bad tool schema, broken message order) is tried on other models without cooling
anything, and if none accepts it the harness gets that 400 back rather than a retryable 503.
A provider whose base_url points back at this proxy (OPENAI_BASE_URL exported for the harness in
the same shell) is taken out of the ladder at startup instead of looping.

Endpoints (the /v1 prefix is optional, so base_url with or without /v1 both work):
    GET  /v1/models               "relay/auto", "relay/tier-N" aliases, then every ladder id
    POST /v1/chat/completions     routed completion; stream=true answers with one SSE chunk
    GET  /v1/relay/savings        relay.value.month over this proxy's ledger (?month=YYYY-MM)
    GET  /v1/relay/capacity       relay.capacity.assess, or [] when the config has no subscriptions
    GET  /v1/relay/status         live ladder: cooldowns, retirements, calls and spend per model
    GET  /health                  liveness; never needs auth

Choosing a model: "relay/auto" (or any name the ladder does not know) lets the Router pick;
"relay/tier-N" starts at tier N; a ladder id (or its provider model name) starts on that model
and still fails over. Response headers say what happened: X-Relay-Model (ladder id used),
X-Relay-Switches (failovers inside this request), X-Relay-Provider, X-Relay-Tier, X-Relay-Usd,
X-Relay-Attempts. Every upstream success writes a "call" row to <cwd>/.relay/events.jsonl, so
`relay savings` and `relay stats` count proxied traffic.

Security: binds 127.0.0.1 unless --host says otherwise. With RELAY_SERVE_KEY set, every route
but /health needs `Authorization: Bearer <key>` (X-Api-Key also accepted). Without a key,
browser requests from non-loopback origins are refused, and so is any Host header that is a public
domain name, so a web page you visit can neither spend your credits through localhost nor read
your spend via DNS rebinding.
"""
from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import math
import os
import re
import socket
import socketserver
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from . import ui
from .config import DEFAULT, build_router, load
from .providers import Provider, ProviderError, classify
from .router import ModelSpec, NoModelAvailable
from .telemetry import Telemetry

AUTO = "relay/auto"
DEFAULT_PORT = 8037
MAX_BODY = 64 * 1024 * 1024
LOOPBACK = {"localhost", "127.0.0.1", "::1"}
RELAY_HEADERS = ("X-Relay-Model", "X-Relay-Provider", "X-Relay-Tier", "X-Relay-Switches", "X-Relay-Attempts",
                 "X-Relay-Usd")

# request fields forwarded verbatim to OpenAI-compatible upstreams (mock providers take messages + tools)
PASSTHROUGH = ("tools", "tool_choice", "parallel_tool_calls", "response_format", "stop", "temperature", "top_p",
               "presence_penalty", "frequency_penalty", "logit_bias", "seed", "reasoning_effort", "user")
# optional knobs some models reject with a 400 (temperature on reasoning models, say). The provider
# layer would classify that 400 as bad_model and retire a working model, so the knob is dropped and
# the same model retried once instead.
DROPPABLE = ("temperature", "top_p", "presence_penalty", "frequency_penalty", "logit_bias", "seed",
             "reasoning_effort", "parallel_tool_calls")
# upstream statuses that mean "this request is malformed", not "this model is unwell"
CLIENT_FAULT = (400, 413, 415, 422)


def _client_fault(err: ProviderError) -> bool:
    """A 4xx about the request itself (bad tool schema, tool message without a tool call, ...). The
    provider layer files it under "transient", which would cool a healthy model for every client and
    hand the harness a retryable 503 for a request that can never succeed."""
    return err.kind == "transient" and err.status in CLIENT_FAULT


def _upstream_error(err: ProviderError) -> dict:
    """message/param/code from an upstream OpenAI-style error body (which may be truncated JSON)."""
    raw = str(err.args[0]) if err.args else str(err)
    try:
        e = json.loads(raw).get("error")
    except (ValueError, AttributeError):
        e = None
    if isinstance(e, dict):
        return {"message": str(e.get("message") or raw),
                "param": e.get("param") if isinstance(e.get("param"), str) else None,
                "code": e.get("code") if isinstance(e.get("code"), str) else None}
    return {"message": e if isinstance(e, str) else raw, "param": None, "code": None}


# --------------------------------------------------------------------------- upstream call
@dataclass
class Reply:
    message: dict[str, Any]
    finish_reason: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    latency_s: float
    usage_extra: dict[str, Any] = field(default_factory=dict)   # upstream usage details, passed through


def _relax(payload: dict, err: ProviderError) -> bool:
    """A 400 that names an optional knob: drop it (or rename the token cap) so the model gets a fair retry."""
    if err.status != 400:
        return False
    text = str(err).lower()
    changed = False
    for k in DROPPABLE:
        if k in payload and k in text:
            payload.pop(k)
            changed = True
    if "max_completion_tokens" in payload and "max_completion_tokens" in text:
        payload["max_tokens"] = payload.pop("max_completion_tokens")   # older OpenAI-compatible servers
        changed = True
    return changed


def upstream_chat(provider: Provider, spec: ModelSpec, body: dict) -> Reply:
    """One call to one model. Raises ProviderError, which the caller feeds to the Router."""
    messages = body["messages"]
    tools = body.get("tools") or []
    max_tokens = body.get("max_completion_tokens") or body.get("max_tokens")
    if provider.kind == "mock":
        comp = provider.chat(spec.model, messages, tools, max_tokens)
        msg = dict(comp.message)
        return Reply(msg, "tool_calls" if msg.get("tool_calls") else "stop", comp.input_tokens,
                     comp.output_tokens, comp.cost_usd, comp.latency_s)

    payload: dict[str, Any] = {"model": spec.model, "messages": messages}
    payload.update({k: body[k] for k in PASSTHROUGH if body.get(k) is not None})
    if not tools:
        for k in ("tools", "tool_choice", "parallel_tool_calls"):
            payload.pop(k, None)
    if max_tokens:
        payload["max_completion_tokens"] = max_tokens   # same convention as Provider.chat
    t0 = time.time()
    try:
        data = provider._request("POST", "/chat/completions", payload)
    except ProviderError as err:
        if not _relax(payload, err):
            raise
        data = provider._request("POST", "/chat/completions", payload)
    if "error" in data and not data.get("choices"):
        e = data["error"]
        text = e if isinstance(e, str) else json.dumps(e)
        code = e.get("code") if isinstance(e, dict) and isinstance(e.get("code"), int) else None
        raise ProviderError(classify(code, text), text, code)
    choices = data.get("choices") or []
    if not choices:
        raise ProviderError("transient", f"empty response: {json.dumps(data)[:200]}")
    choice = choices[0] or {}
    msg = dict(choice.get("message") or {})
    msg["role"] = "assistant"
    usage = data.get("usage") or {}
    return Reply(
        message=msg,
        finish_reason=choice.get("finish_reason") or ("tool_calls" if msg.get("tool_calls") else "stop"),
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
        cost_usd=usage.get("cost"),
        latency_s=time.time() - t0,
        usage_extra={k: v for k, v in usage.items() if k not in ("prompt_tokens", "completion_tokens", "total_tokens")},
    )


# --------------------------------------------------------------------------- routing
@dataclass
class Outcome:
    status: int
    spec: ModelSpec | None = None
    reply: Reply | None = None
    usd: float = 0.0
    switches: int = 0
    attempts: list[dict] = field(default_factory=list)
    error: dict | None = None
    headers: dict[str, str] = field(default_factory=dict)


class _Cooling(Exception):
    def __init__(self, wait: float):
        super().__init__(f"every usable model is cooling down for another {wait:.0f}s")
        self.wait = wait


def _error(message: str, type_: str = "invalid_request_error", code: str | None = None,
           param: str | None = None, **extra: Any) -> dict:
    return {"error": {"message": message, "type": type_, "param": param, "code": code, **extra}}


class RelayProxy:
    """The routing core, independent of HTTP: one Router shared by every request thread."""

    def __init__(self, cfg: dict, cwd: str = ".", include_claude: bool = True, key: str | None = None,
                 max_attempts: int = 6, max_wait: float = 90.0, start_tier: int | None = None,
                 log: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.router = build_router(cfg, start_tier)
        self.cwd = cwd
        self.ledger = os.path.join(cwd, ".relay", "events.jsonl")
        self.tel = Telemetry(os.path.join(cwd, ".relay"))
        self.include_claude = include_claude
        self.key = (os.environ.get("RELAY_SERVE_KEY") if key is None else key) or None
        self.max_attempts = max(1, int(max_attempts))
        self.max_wait = max(0.0, float(max_wait))
        self.log = log
        self.started = time.time()
        self._lock = threading.Lock()       # guards all Router state
        self._tel_lock = threading.Lock()   # one JSONL row at a time
        tiers = sorted({m.tier for m in self.router.models})
        self.aliases: dict[str, int | None] = {AUTO: None, **{f"relay/tier-{t}": t for t in tiers}}

    # ------------------------------------------------------------ helpers
    def emit(self, event: str, **data: Any) -> None:
        with self._tel_lock:
            self.tel.emit(event, **data)

    def _say(self, line: str) -> None:
        if self.log:
            self.log(line)

    def _alive(self, m: ModelSpec, min_ctx: int) -> bool:
        r = self.router
        return m.provider not in r.dead_providers and r.state[m.id].retired is None and m.context_window >= min_ctx

    def _ready(self, m: ModelSpec, min_ctx: int, now: float) -> bool:
        return self._alive(m, min_ctx) and self.router.state[m.id].cooldown_until <= now

    def resolve(self, name: str | None) -> tuple[int | None, ModelSpec | None]:
        """(tier, pinned model) for a requested model name. (None, None): the Router decides."""
        name = (name or AUTO).strip() if isinstance(name, str) else AUTO
        models = self.router.models
        # also try without a client-side provider prefix: "openai/relay/auto" -> "relay/auto"
        for cand in (name, name.split("/", 1)[1] if "/" in name else ""):
            if not cand:
                continue
            if cand in self.aliases:
                return self.aliases[cand], None
            spec = next((m for m in models if m.id == cand), None) or next((m for m in models if m.model == cand), None)
            if spec:
                return spec.tier, spec
        return None, None

    def _acquire(self, tier: int | None, min_ctx: int, deadline: float,
                 skip: frozenset[str] | set[str] = frozenset()) -> ModelSpec:
        """Router.pick() for this request's tier and context need, waiting (outside the lock) for a cooldown.
        `skip`: models that already rejected this request as malformed; healthy, just not for this one."""
        r = self.router
        while True:
            with self._lock:
                now = time.time()
                alive = [m for m in r.models if self._alive(m, min_ctx) and m.id not in skip]
                if not alive:
                    raise NoModelAvailable(r.status_line())
                if any(self._ready(m, min_ctx, now) for m in alive):
                    # tier and min_context are per request here: one huge conversation must not
                    # lock every other client out of the small-window models
                    saved = r.tier, r.min_context
                    current = r.current   # a detour for one odd request must not move everyone's sticky pick
                    held = {i: r.state[i].cooldown_until for i in skip}
                    r.tier = saved[0] if tier is None else min(max(tier, 0), r.max_tier)
                    r.min_context = min_ctx
                    for i in skip:   # hidden from this pick only, restored below
                        r.state[i].cooldown_until = math.inf
                    try:
                        return r.pick()[0]   # never sleeps: a ready model exists
                    finally:
                        r.tier, r.min_context = saved
                        for i, until in held.items():
                            r.state[i].cooldown_until = until
                        if skip:
                            r.current = current
                wait = min(r.state[m.id].cooldown_until for m in alive) - now
            if time.time() + wait > deadline:
                raise _Cooling(wait)
            time.sleep(min(wait, 0.5) + 0.01)

    def _call(self, spec: ModelSpec, body: dict) -> Reply:
        try:
            return upstream_chat(self.router.providers[spec.provider], spec, body)
        except ProviderError:
            raise
        except Exception as e:   # a provider bug fails over like a 5xx instead of killing the request
            raise ProviderError("transient", f"{type(e).__name__}: {e}") from None

    # ------------------------------------------------------------ the request
    def complete(self, body: dict) -> Outcome:
        requested = body.get("model") or AUTO
        tier, pinned = self.resolve(body.get("model"))
        approx = len(json.dumps(body.get("messages"), default=str)) // 4
        deadline = time.time() + self.max_wait
        min_ctx, switches, why = 0, 0, ""
        prev: ModelSpec | None = None
        last_err: ProviderError | None = None
        attempts: list[dict] = []
        skip: set[str] = set()   # rejected this request as malformed: try another, don't cool it for everyone
        for n in range(self.max_attempts):
            spec = None
            if n == 0 and pinned is not None:
                with self._lock:
                    spec = pinned if self._ready(pinned, min_ctx, time.time()) else None
            if spec is None:
                try:
                    spec = self._acquire(tier, min_ctx, deadline, skip)
                except NoModelAvailable:   # its text is Router.status_line(), which _fail appends anyway
                    return self._fail(requested, attempts, last_err, min_ctx, approx, "no model available")
                except _Cooling as c:
                    return self._fail(requested, attempts, last_err, min_ctx, approx, str(c), retry_after=c.wait)
            if prev is not None and spec.id != prev.id:
                switches += 1
                self.emit("switch", model=spec.id, old=prev.id, reason=why, tier=spec.tier)
            prev = spec
            try:
                reply = self._call(spec, body)
            except ProviderError as err:
                last_err = err
                if _client_fault(err):
                    skip.add(spec.id)
                    why = f"rejected: {spec.id} refused this request ({err.status}), trying another model"
                    detail = str(err)[:300]
                    attempts.append({"model": spec.id, "kind": err.kind, "status": err.status, "detail": detail,
                                     "rejected": True})
                    self.emit("provider_error", model=spec.id, kind=err.kind, status=err.status, detail=detail,
                              rejected=True)
                    continue
                with self._lock:
                    saved = self.router.min_context
                    self.router.min_context = min_ctx
                    why = self.router.record_error(spec.id, err, approx_context=approx)
                    min_ctx = self.router.min_context
                    self.router.min_context = saved
                detail = str(err)[:300]
                attempts.append({"model": spec.id, "kind": err.kind, "status": err.status, "detail": detail})
                self.emit("provider_error", model=spec.id, kind=err.kind, status=err.status, detail=detail)
                continue
            with self._lock:
                usd = self.router.record_success(spec.id, reply.input_tokens, reply.output_tokens, reply.cost_usd)
            self.emit("call", model=spec.id, provider=spec.provider, input_tokens=reply.input_tokens,
                      output_tokens=reply.output_tokens, usd=usd, latency_s=round(reply.latency_s, 2), tier=spec.tier)
            attempts.append({"model": spec.id, "ok": True})
            hop = ui.c("35", f"  ⇄ {switches} switch{'es' if switches != 1 else ''}") if switches else ""
            self._say(f"{requested} → " + ui.c("1;34", spec.id) + hop +
                      ui.c("2", f"  {reply.input_tokens}+{reply.output_tokens} tok  ${usd:.4f}  {reply.latency_s:.1f}s"))
            return Outcome(200, spec, reply, usd, switches, attempts)
        return self._fail(requested, attempts, last_err, min_ctx, approx, f"{self.max_attempts} attempts failed")

    def _fail(self, requested: str, attempts: list[dict], last_err: ProviderError | None, min_ctx: int,
              approx: int, reason: str, retry_after: float | None = None) -> Outcome:
        with self._lock:
            status = self.router.status_line()
            # overflowed some windows, and a model is still alive if the conversation were shorter:
            # tell the harness in the words it already understands, so it compacts and retries
            compactable = bool(min_ctx) and any(self._alive(m, 0) for m in self.router.models)
        relay = {"attempts": attempts, "status": [s.strip() for s in status.splitlines()]}
        if retry_after is None and last_err is not None and _client_fault(last_err):
            # the request itself is bad: a 400 the harness shows to its user, not a 503 its SDK retries
            up = _upstream_error(last_err)
            ids = ", ".join(dict.fromkeys(a["model"] for a in attempts if a.get("rejected")))
            out = Outcome(400, attempts=attempts, error=_error(
                f"{up['message']} (relay: upstream HTTP {last_err.status}; rejected by {ids})",
                "invalid_request_error", up["code"] or "relay_request_rejected", up["param"], relay=relay))
        elif retry_after is None and compactable and last_err is not None and last_err.kind == "context":
            biggest = max((m.context_window for m in self.router.models if self._alive(m, 0)), default=0)
            out = Outcome(400, attempts=attempts, error=_error(
                f"relay: the conversation (~{approx} tokens) is over the maximum context length of every usable "
                f"model in the ladder (largest window {biggest} tokens). Compact or trim the messages and retry.",
                "invalid_request_error", "context_length_exceeded", "messages", relay=relay))
        else:
            code = "relay_models_cooling" if retry_after is not None else (
                "relay_no_model_available" if reason.startswith("no model") else "relay_attempts_exhausted")
            last = f" Last error: {last_err}." if last_err else ""
            out = Outcome(503, attempts=attempts, error=_error(
                f"relay: {reason}.{last}\n{status}", "server_error", code, relay=relay))
            if retry_after is not None:
                out.headers["Retry-After"] = str(max(1, math.ceil(retry_after)))
        self.emit("abort", reason=out.error["error"]["code"], requested=requested, attempts=len(attempts))
        self._say(f"{requested} " + ui.c("31", f"✗ {out.status} {out.error['error']['code']}") +
                  ui.c("2", f"  ({len(attempts)} attempts)"))
        return out

    # ------------------------------------------------------------ read-only endpoints
    def models(self) -> dict:
        created = int(self.started)
        data = [{"id": a, "object": "model", "created": created, "owned_by": "relay"} for a in self.aliases]
        data += [{"id": m.id, "object": "model", "created": created, "owned_by": m.provider} for m in self.router.models]
        return {"object": "list", "data": data}

    def health(self) -> dict:
        with self._lock:
            now = time.time()
            ready = sum(self._ready(m, 0, now) for m in self.router.models)
        return {"status": "ok", "models": len(self.router.models), "ready": ready,
                "uptime_s": round(time.time() - self.started, 1)}

    def status(self) -> dict:
        r = self.router
        with self._lock:
            now = time.time()
            rows = []
            for m in r.models:
                s = r.state[m.id]
                state = ("retired" if s.retired else "dead" if m.provider in r.dead_providers else
                         "cooling" if s.cooldown_until > now else "ok")
                rows.append({"id": m.id, "provider": m.provider, "model": m.model, "tier": m.tier,
                             "context_window": m.context_window, "state": state,
                             "reason": s.retired or r.dead_providers.get(m.provider),
                             "cooldown_s": round(max(0.0, s.cooldown_until - now), 1), "calls": s.calls,
                             "errors": s.errors, "input_tokens": s.input_tokens, "output_tokens": s.output_tokens,
                             "usd": round(s.usd, 6)})
            return {"tier": r.tier, "current": r.current, "uptime_s": round(now - self.started, 1),
                    "ledger": self.ledger, "models": rows, "totals": r.totals()}

    def savings(self, period: str | None = None, include_claude: bool | None = None) -> dict:
        from . import value
        claude = self.include_claude if include_claude is None else include_claude
        m = value.month(period, [self.ledger], None, include_claude=claude)
        return {"period": m.period, "used_usd": round(m.used_usd, 6), "rescued_usd": round(m.rescued_usd, 6),
                "rescue_rate": round(m.rescue_rate, 4), "nights": m.nights, "tasks_done": m.tasks_done,
                "tasks_blocked": m.tasks_blocked, "by_source": dict(m.by_source), "by_model": dict(m.by_model),
                "unpriced": sorted(m.unpriced), "ledger": self.ledger, "include_claude": claude}

    def capacity(self) -> list[dict]:
        if not self.cfg.get("subscriptions"):
            return []
        from . import capacity
        out = []
        for c in capacity.assess(self.cfg, self.ledger):
            d = asdict(c)
            d["resets_at"] = c.resets_at.isoformat()
            d["hours_to_reset"] = round(c.hours_to_reset, 2)
            out.append(d)
        return out


# --------------------------------------------------------------------------- wire format
def _client_message(msg: dict) -> dict:
    m = dict(msg)
    m["role"] = "assistant"
    if m.get("tool_calls"):
        m.setdefault("content", None)
    else:
        m.pop("tool_calls", None)
        m["content"] = m.get("content") or ""
    return m


def _usage(reply: Reply) -> dict:
    return {**reply.usage_extra, "prompt_tokens": reply.input_tokens, "completion_tokens": reply.output_tokens,
            "total_tokens": reply.input_tokens + reply.output_tokens}


def completion_json(out: Outcome, cid: str, created: int) -> dict:
    return {"id": cid, "object": "chat.completion", "created": created, "model": out.spec.model,
            "choices": [{"index": 0, "message": _client_message(out.reply.message), "logprobs": None,
                         "finish_reason": out.reply.finish_reason}],
            "usage": _usage(out.reply)}


def sse_body(out: Outcome, cid: str, created: int, include_usage: bool = False) -> str:
    """The whole answer as one delta chunk, a finish chunk, optional usage chunk, then [DONE].

    finish_reason gets its own chunk because some stream readers stop at the first chunk that
    carries one and would drop content sent alongside it."""
    msg = _client_message(out.reply.message)
    delta: dict[str, Any] = {"role": "assistant"}
    if msg.get("content") is not None or not msg.get("tool_calls"):
        delta["content"] = msg.get("content") or ""
    for k in ("refusal", "reasoning_content", "reasoning"):
        if msg.get(k):
            delta[k] = msg[k]
    if msg.get("tool_calls"):
        delta["tool_calls"] = [{**tc, "index": i} for i, tc in enumerate(msg["tool_calls"])]
    base = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": out.spec.model}
    chunks = [{**base, "choices": [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": None}]},
              {**base, "choices": [{"index": 0, "delta": {}, "logprobs": None,
                                    "finish_reason": out.reply.finish_reason}]}]
    if include_usage:
        chunks.append({**base, "choices": [], "usage": _usage(out.reply)})
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def _validate(body: Any) -> tuple[str, str | None] | None:
    """(message, param) for a request that must not reach an upstream, else None. Checked before any
    spend, so a shape error never costs a model call."""
    if not isinstance(body, dict):
        return "request body must be a JSON object", None
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return "'messages' must be a non-empty array", "messages"
    if not all(isinstance(m, dict) for m in msgs):
        return "every entry in 'messages' must be an object", "messages"
    if body.get("tools") is not None and not isinstance(body.get("tools"), list):
        return "'tools' must be an array", "tools"
    if body.get("stream_options") is not None and not isinstance(body.get("stream_options"), dict):
        return "'stream_options' must be an object", "stream_options"
    return None


class _TooLarge(Exception):
    pass


# --------------------------------------------------------------------------- HTTP
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "relay-serve"
    sys_version = ""
    timeout = 300   # an idle keep-alive connection gives its thread back

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - routed calls are logged by the proxy
        pass

    @property
    def proxy(self) -> RelayProxy:
        return self.server.proxy  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:      # OpenAI-shaped 405/404 instead of http.server's HTML 501
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_OPTIONS(self) -> None:
        if not self._origin_ok():
            return self._error(403, "cross-origin requests are refused unless RELAY_SERVE_KEY is set",
                               "permission_error", "origin_not_allowed")
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         self.headers.get("Access-Control-Request-Headers") or "Authorization, Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ------------------------------------------------------------ plumbing
    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True   # not a browser
        host = (urlsplit(origin).hostname or "").lower()
        return host in LOOPBACK or host.endswith(".localhost") or bool(self.proxy.key)

    def _host_ok(self) -> bool:
        """DNS-rebinding guard. A page on attacker.example that rebinds its name to 127.0.0.1 can GET
        this server without sending an Origin header (same-origin GETs carry none), but its Host header
        still says attacker.example. Without a key, answer only to names a public site can't own."""
        if self.proxy.key:
            return True
        raw = (self.headers.get("Host") or "").strip()
        if not raw:
            return True   # HTTP/1.0 tools; browsers always send Host
        try:
            name = (urlsplit("//" + raw).hostname or "").lower().rstrip(".")
        except ValueError:
            return False
        try:
            ipaddress.ip_address(name)
            return True   # a literal address can't be rebound
        except ValueError:
            pass
        return (name in LOOPBACK or "." not in name or name.endswith((".localhost", ".local", ".internal"))
                or name == str(self.server.server_address[0]).lower())

    def _authorized(self) -> bool:
        key = self.proxy.key
        if not key:
            return True
        auth = self.headers.get("Authorization") or ""
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        token = token or self.headers.get("X-Api-Key") or self.headers.get("Api-Key") or ""
        return hmac.compare_digest(token.encode(), key.encode())

    def _cors(self) -> None:
        origin = self.headers.get("Origin")
        if origin and self._origin_ok():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Expose-Headers", ", ".join(RELAY_HEADERS + ("Retry-After",)))

    def _send(self, status: int, data: bytes, ctype: str, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, str(v).encode("ascii", "replace").decode())
        self._cors()
        if status >= 400:   # the request body may be unread: don't reuse this connection
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: int, obj: Any, headers: dict | None = None) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json", headers)

    def _error(self, status: int, message: str, type_: str = "invalid_request_error", code: str | None = None,
               param: str | None = None, headers: dict | None = None) -> None:
        self._json(status, _error(message, type_, code, param), headers)

    def _read_body(self) -> bytes:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            parts, total = [], 0
            while True:
                size = int(self.rfile.readline(65537).split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline(65537) not in (b"\r\n", b"\n", b""):
                        pass   # trailers
                    return b"".join(parts)
                total += size
                if total > MAX_BODY:
                    raise _TooLarge
                parts.append(self.rfile.read(size))
                self.rfile.readline()
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise _TooLarge
        return self.rfile.read(n) if n > 0 else b""

    # ------------------------------------------------------------ routes
    def _dispatch(self, method: str) -> None:
        try:
            self._route(method)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as e:  # keep serving; report in OpenAI shape
            self.close_connection = True
            try:
                self._error(500, f"relay serve: {type(e).__name__}: {e}", "server_error", "relay_internal_error")
            except Exception:
                pass

    def _route(self, method: str) -> None:
        url = urlsplit(self.path)
        path = url.path.rstrip("/") or "/"
        if path == "/v1" or path.startswith("/v1/"):
            path = path[3:] or "/"
        if not self._origin_ok():
            return self._error(403, "browser requests from other origins are refused unless RELAY_SERVE_KEY is set",
                               "permission_error", "origin_not_allowed")
        if path == "/health" and method == "GET":
            return self._json(200, self.proxy.health())
        if not self._host_ok():
            return self._error(403, f"Host '{self.headers.get('Host')}' is not a local name; set RELAY_SERVE_KEY to "
                                    "serve other host names (DNS-rebinding guard)", "permission_error", "host_not_allowed")
        if not self._authorized():
            return self._error(401, "missing or wrong API key: send 'Authorization: Bearer $RELAY_SERVE_KEY'",
                               "invalid_request_error", "invalid_api_key")
        q = parse_qs(url.query)
        if method == "POST" and path == "/chat/completions":
            return self._chat()
        if method == "GET":
            if path == "/models":
                return self._json(200, self.proxy.models())
            if path.startswith("/models/"):
                mid = unquote(path[len("/models/"):])
                hit = next((m for m in self.proxy.models()["data"] if m["id"] == mid), None)
                return self._json(200, hit) if hit else self._error(
                    404, f"model '{mid}' is not in the relay ladder", "invalid_request_error", "model_not_found", "model")
            if path == "/relay/savings":
                period = (q.get("month") or [None])[0]
                if period and not re.fullmatch(r"\d{4}-\d{2}", period):
                    return self._error(400, "month must look like YYYY-MM", param="month")
                claude = (q.get("claude") or [None])[0]
                return self._json(200, self.proxy.savings(period, None if claude is None else claude not in ("0", "false", "no")))
            if path == "/relay/capacity":
                return self._json(200, self.proxy.capacity())
            if path == "/relay/status":
                return self._json(200, self.proxy.status())
            if path == "/":
                return self._json(200, {"name": "relay serve", "base_url": self.server.base_url,  # type: ignore[attr-defined]
                                        "model": AUTO, "endpoints": ["GET /v1/models", "POST /v1/chat/completions",
                                        "GET /v1/relay/savings", "GET /v1/relay/capacity", "GET /v1/relay/status",
                                        "GET /health"]})
        if path in ("/chat/completions", "/models", "/relay/savings", "/relay/capacity", "/relay/status", "/health"):
            return self._error(405, f"{method} is not allowed on {url.path}", code="method_not_allowed")
        return self._error(404, f"relay serve speaks the OpenAI Chat Completions API; nothing at {method} {url.path} "
                                "(use POST /v1/chat/completions)", code="not_found")

    def _chat(self) -> None:
        try:
            raw = self._read_body()
        except _TooLarge:
            return self._error(413, f"request body is over {MAX_BODY // (1024 * 1024)} MB", code="request_too_large")
        except ValueError:
            return self._error(400, "malformed request body (bad Content-Length or chunked encoding)")
        try:
            body = json.loads(raw or b"{}")
        except (ValueError, UnicodeDecodeError) as e:
            return self._error(400, f"request body is not valid JSON: {e}")
        problem = _validate(body)
        if problem:
            return self._error(400, problem[0], param=problem[1])
        out = self.proxy.complete(body)
        if out.status != 200:
            return self._json(out.status, out.error, out.headers)
        headers = {"X-Relay-Model": out.spec.id, "X-Relay-Provider": out.spec.provider,
                   "X-Relay-Tier": out.spec.tier, "X-Relay-Switches": out.switches,
                   "X-Relay-Attempts": len(out.attempts), "X-Relay-Usd": f"{out.usd:.6f}"}
        cid, created = f"chatcmpl-relay-{uuid.uuid4().hex[:24]}", int(time.time())
        if body.get("stream"):
            include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
            data = sse_body(out, cid, created, include_usage).encode()
            return self._send(200, data, "text/event-stream; charset=utf-8",
                              {**headers, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        return self._json(200, completion_json(out, cid, created), headers)


class RelayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], proxy: RelayProxy):
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        self.proxy = proxy
        super().__init__(addr, _Handler)

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)   # skip HTTPServer's reverse-DNS lookup (slow on some Macs)
        self.server_name, self.server_port = str(self.server_address[0]), int(self.server_address[1])

    @property
    def base_url(self) -> str:
        host, port = str(self.server_address[0]), int(self.server_address[1])
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        return f"http://{f'[{host}]' if ':' in host else host}:{port}/v1"


def _points_at(url: str, host: str, port: int) -> bool:
    """Does an upstream base_url name this very server (a loopback or the bound address, same port)?"""
    try:
        u = urlsplit(url)
        h = (u.hostname or "").lower()
        p = u.port or {"http": 80, "https": 443}.get(u.scheme.lower())
    except ValueError:
        return False
    if not h or p != port:
        return False
    try:
        ip = ipaddress.ip_address(h)
        if ip.is_loopback or ip.is_unspecified:
            return True
    except ValueError:
        pass
    return h in LOOPBACK or h.endswith(".localhost") or h == host.lower().strip("[]")


def _disable_self_loops(proxy: RelayProxy, host: str, port: int) -> None:
    """The usual integration step is `export OPENAI_BASE_URL=http://127.0.0.1:8037/v1` for the harness,
    and the default ladder's openai provider reads that same variable. Started from such a shell, the
    proxy would call itself recursively (thousands of nested requests for one prompt). Take any such
    provider out of the ladder and say why."""
    r = proxy.router
    for name, p in r.providers.items():
        if p.kind == "mock":
            continue
        url = p.resolved_base_url()
        if _points_at(url, host, port):
            hint = f" (from {p.base_url})" if p.base_url.startswith("$") else ""
            r.dead_providers[name] = (f"base_url {url}{hint} is this relay serve, so every call would loop back "
                                      "here; point the provider at the real API, or unset that variable in the shell "
                                      "that starts relay serve")


def make_server(cfg: dict, host: str = "127.0.0.1", port: int = DEFAULT_PORT, cwd: str = ".",
                include_claude: bool = True, key: str | None = None, max_attempts: int = 6,
                max_wait: float = 90.0, start_tier: int | None = None,
                log: Callable[[str], None] | None = None) -> RelayServer:
    """A bound, not yet serving, proxy. key=None reads RELAY_SERVE_KEY; key="" turns auth off. port=0: any free port."""
    proxy = RelayProxy(cfg, cwd, include_claude, key, max_attempts, max_wait, start_tier, log)
    srv = RelayServer((host, port), proxy)
    _disable_self_loops(proxy, host, int(srv.server_address[1]))
    proxy.emit("serve_start", base_url=srv.base_url, auth=bool(proxy.key),
               models=[m.id for m in proxy.router.models], dead=dict(proxy.router.dead_providers))
    return srv


# --------------------------------------------------------------------------- CLI
def _exposed(host: str) -> bool:
    return not (host in LOOPBACK or host.startswith("127."))


def _log_line(line: str) -> None:
    print(ui.c("2", time.strftime("%H:%M:%S")) + "  " + line, flush=True)


def banner(srv: RelayServer) -> str:
    p = srv.proxy
    ready = p.health()["ready"]
    return (ui.c("1;36", "relay serve") + f" · base_url {srv.base_url} · model {AUTO} · "
            f"{ready}/{len(p.router.models)} models ready · auth {'on' if p.key else 'off'} · ledger {p.ledger}")


def add_arguments(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Flags for `relay serve`. -c/-C/--tier default to SUPPRESS so the top-level relay flags of the same
    name still apply when given before the subcommand (`relay -c x.json serve`)."""
    s = argparse.SUPPRESS
    p.add_argument("-c", "--config", default=s, help="relay.json ladder (default: ./relay.json, ~/.relay.json, built-in)")
    p.add_argument("-C", "--cwd", default=s, help="directory whose .relay/events.jsonl records proxied calls (default: .)")
    p.add_argument("--tier", type=int, default=s, help="tier relay/auto starts on (overrides config)")
    p.add_argument("--host", default="127.0.0.1", help="interface to bind (default 127.0.0.1; 0.0.0.0 needs RELAY_SERVE_KEY)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default {DEFAULT_PORT}; 0 = any free port)")
    p.add_argument("--no-claude", action="store_true", help="/v1/relay/savings leaves out Claude Code logs")
    p.add_argument("--max-attempts", type=int, default=6, help="upstream calls per request before giving up (default 6)")
    p.add_argument("--max-wait", type=float, default=90.0,
                   help="seconds a request may wait for a cooling model before a 503 (default 90)")
    p.add_argument("-q", "--quiet", action="store_true", help="no per-request log lines")
    return p


def run(args: argparse.Namespace) -> int:
    """Serve until Ctrl-C. Works with the namespace from add_arguments() or from the relay CLI."""
    try:
        cfg = load(getattr(args, "config", None))
    except FileNotFoundError as e:
        print(f"relay serve: config not found: {e}", file=sys.stderr)
        return 2
    if "models" not in cfg:   # e.g. a burner.json with only subscriptions + idle: serve the default ladder
        cfg = {**DEFAULT, **cfg}
    cwd = getattr(args, "cwd", None) or "."
    try:
        srv = make_server(cfg, args.host, args.port, cwd, include_claude=not args.no_claude,
                          key=os.environ.get("RELAY_SERVE_KEY") or "", max_attempts=args.max_attempts,
                          max_wait=args.max_wait, start_tier=getattr(args, "tier", None),
                          log=None if args.quiet else _log_line)
    except OSError as e:
        print(f"relay serve: cannot listen on {args.host}:{args.port}: {e}", file=sys.stderr)
        return 1
    proxy = srv.proxy
    print(banner(srv), flush=True)
    if _exposed(args.host) and not proxy.key:
        print(ui.c("1;33", f"warning: listening on {args.host} without RELAY_SERVE_KEY - anyone who can reach "
                           f"port {srv.server_address[1]} can spend your model credits"), file=sys.stderr, flush=True)
    for name, why in proxy.router.dead_providers.items():
        print(ui.c("2", f"  provider {name} unavailable: {why}"), flush=True)
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print()
    finally:
        srv.server_close()
        proxy.emit("serve_stop", **proxy.router.totals())
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="relay serve",
                                 description="OpenAI-compatible proxy: point any agent harness at Relay's model ladder.")
    return run(add_arguments(ap).parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
