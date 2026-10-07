"""OpenAI-compatible chat providers plus a scripted mock for offline demos.

Every provider error is normalised into a ProviderError with a `kind`, which is
what the router uses to decide how to switch:

    rate_limit   429 / "rate limit"          -> cool the model down, try a sibling
    budget       402 / insufficient_quota /
                 instance_budget_exhausted   -> disable the whole provider
    context      context length exceeded     -> move to a bigger-window model
    bad_model    404 / unknown model         -> disable that model
    auth         401 / 403                   -> disable the whole provider
    transient    5xx / timeout / network     -> short cooldown
"""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


class ProviderError(Exception):
    def __init__(self, kind: str, message: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retry_after = retry_after

    def __str__(self) -> str:
        code = f" {self.status}" if self.status else ""
        return f"[{self.kind}{code}] {self.args[0]}"


def classify(status: int | None, body: str) -> str:
    text = (body or "").lower()
    if "context_length" in text or "context length" in text or "maximum context" in text or "too many tokens" in text:
        return "context"
    if status == 402 or "insufficient_quota" in text or "budget_exhausted" in text or "insufficient_balance" in text:
        return "budget"
    if status == 429 or "rate limit" in text or "rate_limit" in text:
        return "rate_limit"
    if status in (401, 403):
        return "auth"
    if status == 404 or "model_not_found" in text or "not a valid model" in text or "no endpoints found" in text:
        return "bad_model"
    if status == 400 and "model" in text and ("not" in text or "invalid" in text or "unsupported" in text):
        return "bad_model"
    return "transient"


@dataclass
class Completion:
    message: dict[str, Any]          # assistant message (role, content, tool_calls)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None    # provider-reported cost when available
    latency_s: float = 0.0


@dataclass
class Provider:
    name: str
    base_url: str = ""
    api_key_env: str = ""
    kind: str = "openai"             # "openai" (any OpenAI-compatible API) or "mock"
    timeout: float = 120.0
    mock: dict[str, Any] = field(default_factory=dict)
    _mock_calls: dict[str, int] = field(default_factory=dict)

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "") if self.api_key_env else ""

    def resolved_base_url(self) -> str:
        # allow "$ENV_VAR" or "${ENV_VAR:-fallback}" style base urls
        url = self.base_url
        if url.startswith("$"):
            name, _, fallback = url.strip("${}").partition(":-")
            url = os.environ.get(name, fallback)
        return url.rstrip("/")

    def available(self) -> tuple[bool, str]:
        if self.kind == "mock":
            return True, "mock"
        if not self.resolved_base_url():
            return False, "no base_url"
        if self.api_key_env and not self.api_key:
            return False, f"${self.api_key_env} not set"
        return True, "ok"

    # ------------------------------------------------------------------ http
    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = self.resolved_base_url() + path
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            ra = e.headers.get("retry-after") if e.headers else None
            try:
                retry_after = float(ra) if ra else None
            except ValueError:
                retry_after = None
            raise ProviderError(classify(e.code, body), body[:400] or str(e), e.code, retry_after) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
            raise ProviderError("transient", f"network: {e}") from None

    def list_models(self) -> list[str]:
        if self.kind == "mock":
            return list(self.mock.get("models", ["mock-model"]))
        data = self._request("GET", "/models")
        return sorted(m.get("id", "?") for m in data.get("data", []))

    def chat(self, model: str, messages: list[dict], tools: list[dict], max_tokens: int | None = None) -> Completion:
        if self.kind == "mock":
            return self._mock_chat(model, messages, tools)
        payload: dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            payload["tools"] = tools
        if max_tokens:
            payload["max_completion_tokens"] = max_tokens
        t0 = time.time()
        data = self._request("POST", "/chat/completions", payload)
        if "error" in data and not data.get("choices"):
            err = data["error"]
            msg = json.dumps(err) if not isinstance(err, str) else err
            raise ProviderError(classify(err.get("code") if isinstance(err, dict) and isinstance(err.get("code"), int) else None, msg), msg)
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError("transient", f"empty response: {json.dumps(data)[:200]}")
        usage = data.get("usage") or {}
        msg = choices[0].get("message") or {}
        msg = {k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")}
        msg["role"] = "assistant"
        return Completion(
            message=msg,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            cost_usd=usage.get("cost"),
            latency_s=time.time() - t0,
        )

    # ------------------------------------------------------------------ mock
    def _mock_chat(self, model: str, messages: list[dict], tools: list[dict] | None = None) -> Completion:
        """Scripted behaviour per model, configured in the provider's `mock` block:

        "mock": {"models": {"cheap": {"fail_after": 2, "fail_with": "rate_limit"},
                            "stuck": {"behaviour": "loop"},
                            "smart": {"behaviour": "solve"}}}
        """
        n = self._mock_calls.get(model, 0) + 1
        self._mock_calls[model] = n
        spec = (self.mock.get("models") or {}).get(model, {}) if isinstance(self.mock.get("models"), dict) else {}
        time.sleep(float(self.mock.get("delay", 0.15)))
        fail_after = spec.get("fail_after")
        if fail_after is not None and n > int(fail_after):
            kind = spec.get("fail_with", "rate_limit")
            status = {"rate_limit": 429, "budget": 402, "context": 400, "auth": 401}.get(kind, 503)
            raise ProviderError(kind, f"mock {model}: simulated {kind}", status, retry_after=spec.get("retry_after"))

        scale = float(self.mock.get("tokens_scale", 1))   # demo realism: make mock calls cost real-looking amounts
        tokens_in = int((sum(len(str(m.get("content") or "")) for m in messages) // 4 + 50) * scale)
        behaviour = spec.get("behaviour", "solve")
        last_tool = next((m for m in reversed(messages) if m.get("role") == "tool"), None)

        def call(name: str, args: dict) -> dict:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": f"call_{model}_{n}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}]}

        if behaviour == "refuse" and tools:
            task = next((str(m.get("content")) for m in messages if m.get("role") == "user"), "").lower()
            text = ("I'm sorry, but I can't help with creating software that captures other people's passwords."
                    if any(w in task for w in ("keylogger", "password")) else
                    "I'm sorry, but I can't help with killing processes. Terminating programs could cause harm.")
            msg = {"role": "assistant", "content": spec.get("refusal", text)}
            return Completion(message=msg, input_tokens=tokens_in, output_tokens=30, latency_s=0.15)
        if not tools and "REFUSAL_TRIAGE" in str(messages[0].get("content")):
            task = str(messages[-1].get("content", "")).lower().split("the agent replied")[0]
            bad = any(w in task for w in spec.get("decline_if", ["keylogger", "steal", "password"]))
            verdict = {"verdict": "decline", "reason": "The task asks for malware that steals credentials."} if bad else \
                      {"verdict": "benign", "reason": "Stopping a local dev process on a port is routine development work."}
            return Completion(message={"role": "assistant", "content": json.dumps(verdict)},
                              input_tokens=tokens_in, output_tokens=40, latency_s=0.15)
        if not tools:  # asked for a second opinion (no tools): give a diagnosis
            msg = {"role": "assistant", "content": spec.get("hint",
                   "- The same import keeps failing: the module does not exist, retrying cannot fix it.\n"
                   "- Stop running it. Write the file the task asks for, then run that file.")}
        elif behaviour == "hang":
            msg = call("bash", {"command": "sleep 600", "timeout": int(spec.get("timeout", 3))})
        elif behaviour == "loop":
            # keeps running the same failing command: the blocker detector should catch this
            msg = call("bash", {"command": "python3 -c 'import missing_module_xyz'"})
        elif behaviour == "solve":
            first_user = next((str(m.get("content")) for m in messages if m.get("role") == "user"), "").lower()
            by_kw = next((v for k, v in (spec.get("plans") or {}).items() if k in first_user), None)
            if isinstance(by_kw, str):  # a plain string means: answer with this text, no tools
                return Completion(message={"role": "assistant", "content": by_kw}, input_tokens=tokens_in,
                                  output_tokens=40, latency_s=0.15)
            plan = by_kw or spec.get("plan") or [
                ["write_file", {"path": "hello_relay.py", "content": "print('hello from relay')\n"}],
                ["bash", {"command": "python3 hello_relay.py"}],
            ]
            # how many of *this model's* plan steps already ran
            ran = sum(1 for m in messages if m.get("role") == "tool" and str(m.get("tool_call_id", "")).startswith(f"call_{model}_"))
            if ran < len(plan):
                name, args = plan[ran]
                msg = call(name, args)
            else:
                out = (last_tool or {}).get("content", "")
                msg = {"role": "assistant", "content": spec.get("final", f"Done. Last output:\n{out}".strip())}
        else:
            msg = {"role": "assistant", "content": spec.get("final", "ok")}
        return Completion(message=msg, input_tokens=tokens_in, output_tokens=int(60 * scale), latency_s=0.15)
