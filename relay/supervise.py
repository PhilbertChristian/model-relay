"""Supervisor mode: unstick an agent hosted on Agent37 (Hermes, OpenClaw, OpenCode, Claude Code, ...).

Relay watches the turn's live event stream and steps in when the agent is trapped:

    loop          same tool + same arguments started N times     -> cancel turn, switch model
    tool errors   N tool_call.failed in a row                     -> cancel turn, switch model
    hung          no events (only keepalives) for --hang seconds  -> cancel turn, switch model
    limit         response.failed, or the reply reports a rate /
                  budget / context limit                          -> switch model
    same answer   the reply is identical to the last attempt      -> switch model

The model switches per turn on the same session (`model` on POST /v1/responses), so the
next model sees the whole history and picks up where the stuck one left off.
"""
from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from collections import Counter

from . import ui
from .blockers import looks_like_refusal
from .telemetry import Telemetry

LIMIT_RE = re.compile(r"rate.?limit|\b429\b|\b402\b|quota|budget[_ ]exhausted|insufficient[_ ]balance|"
                      r"context (length|window)|maximum context|too many tokens", re.I)


class Stuck(Exception):
    def __init__(self, kind: str, detail: str):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind


class HostedAgent:
    def __init__(self, instance: str, api_key: str, agent: str | None = None):
        self.base = f"https://{instance}.agent37.app"
        self.key = api_key
        self.agent = agent

    def _req(self, method: str, path: str, body: dict | None = None, timeout: float = 200):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        req.add_header("X-Agent37-Key", self.key)
        req.add_header("Content-Type", "application/json")
        return urllib.request.urlopen(req, timeout=timeout)

    def models(self) -> list[str]:
        q = f"?agent={self.agent}" if self.agent else ""
        with self._req("GET", f"/v1/models{q}") as r:
            data = json.loads(r.read())
        ids = [m["id"] for m in data.get("data", [])]
        default = data.get("default_model")
        return ([default] if default in ids else []) + [i for i in ids if i != default]

    def cancel(self, response_id: str) -> None:
        try:
            self._req("POST", f"/v1/responses/{response_id}/cancel", {}).read()
        except Exception:
            pass

    def turn(self, text: str, model: str | None, session_id: str | None, watch) -> tuple[str, str, dict]:
        body: dict = {"input": text, "stream": True}
        if model:
            body["model"] = model
        if session_id:
            body["session_id"] = session_id
        if self.agent:
            body["agent"] = self.agent
        resp = self._req("POST", "/v1/responses", body, timeout=200)  # first byte may wait on a wake; keepalives every 30s after
        event, rid, sid, out = None, "", session_id or "", []
        try:
            for raw in resp:
                line = raw.decode(errors="replace").rstrip("\n")
                if line.startswith(":"):
                    watch.tick(None, {})
                    continue
                if line.startswith("event:"):
                    event = line[6:].strip()
                    continue
                if not line.startswith("data:") or not event:
                    continue
                data = json.loads(line[5:].strip() or "{}")
                if event == "response.created":
                    rid, sid = data.get("id", ""), data.get("session_id", sid)
                elif event == "response.output_text.delta":
                    out.append(data.get("text", ""))
                elif event == "response.completed":
                    return sid, data.get("output_text") or "".join(out), data
                elif event == "response.failed":
                    err = data.get("error") or {}
                    raise Stuck("limit" if LIMIT_RE.search(json.dumps(err)) else "failed",
                                f"{err.get('code')}: {err.get('message', '')[:200]}")
                watch.tick(event, data)
        except Stuck:
            if rid:
                self.cancel(rid)
            watch.session_id = sid
            raise
        except (socket.timeout, TimeoutError):
            if rid:
                self.cancel(rid)
            watch.session_id = sid
            raise Stuck("hung", "stream went silent")
        return sid, "".join(out), {}


class Watch:
    """Per-turn trap detector fed by the live event stream."""

    def __init__(self, hang_s: float, repeat_limit: int, fail_limit: int):
        self.hang_s, self.repeat_limit, self.fail_limit = hang_s, repeat_limit, fail_limit
        self.calls: Counter = Counter()
        self.fail_streak = 0
        self.last_event = time.time()
        self.session_id: str | None = None

    def tick(self, event: str | None, data: dict) -> None:
        now = time.time()
        if event is None:  # keepalive: is the agent doing anything at all?
            if now - self.last_event > self.hang_s:
                raise Stuck("hung", f"no progress for {now - self.last_event:.0f}s")
            return
        self.last_event = now
        if event == "response.tool_call.started":
            sig = data.get("tool", "") + json.dumps(data.get("arguments"), sort_keys=True)
            self.calls[sig] += 1
            ui.tool(data.get("tool", "?"), json.dumps({"command": data.get("label") or data.get("arguments")}))
            if self.calls[sig] >= self.repeat_limit:
                raise Stuck("loop", f"{data.get('tool')} called {self.calls[sig]}x with the same arguments")
        elif event == "response.tool_call.failed":
            self.fail_streak += 1
            ui.tool_result(f"failed: {data.get('error', '')}", True)
            if self.fail_streak >= self.fail_limit:
                raise Stuck("tool_errors", f"{self.fail_streak} tool failures in a row")
        elif event == "response.tool_call.completed":
            self.fail_streak = 0


def supervise(instance: str, task: str, ladder: list[str] | None = None, agent: str | None = None,
              hang_s: float = 180, repeat_limit: int = 3, fail_limit: int = 3, max_attempts: int = 6) -> str:
    key = os.environ.get("AGENT37_API_KEY") or ""
    if not key:
        raise SystemExit("set AGENT37_API_KEY")
    hosted = HostedAgent(instance, key, agent)
    tel = Telemetry()
    ladder = ladder or hosted.models()[:4] or [None]
    ui.banner()
    print(ui.c("2", f"supervising {instance} · ladder: {' → '.join(m or 'default' for m in ladder)}"))
    session_id, last_answer, idx = None, None, 0
    message = task
    for attempt in range(1, max_attempts + 1):
        model = ladder[min(idx, len(ladder) - 1)]
        ui.thinking(attempt, model or "default", idx)
        tel.emit("call", model=model, mode="supervise", instance=instance)
        watch = Watch(hang_s, repeat_limit, fail_limit)
        try:
            session_id, answer, done = hosted.turn(message, model, session_id, watch)
            if LIMIT_RE.search(answer[:600]) and len(answer) < 1500:
                raise Stuck("limit", answer.strip()[:160])
            if looks_like_refusal(answer):
                # no independent judge in supervisor mode: surface it, never model-shop around a refusal
                ui.say(answer)
                ui.error("hosted agent refused the task; relay will not switch models to get around a refusal. "
                         "Review it, rephrase with context, or run locally with `relay run` (refusal triage).")
                tel.emit("refusal_upheld", model=model, reason="supervisor mode: refusals are surfaced, not retried")
                return answer
            if last_answer is not None and answer.strip() == last_answer.strip():
                raise Stuck("loop", "same answer as the previous attempt")
            ui.say(answer)
            usage = (done or {}).get("usage") or {}
            tel.emit("session_end", usd=usage.get("cost_usd") or 0.0, baseline_usd=usage.get("cost_usd") or 0.0,
                     model=model, attempts=attempt)
            return answer
        except Stuck as s:
            session_id = session_id or watch.session_id
            nxt = ladder[min(idx + 1, len(ladder) - 1)]
            ui.warn(f"trapped ({s})")
            tel.emit("blocker", model=model, reason=str(s))
            if idx + 1 < len(ladder):
                idx += 1
            ui.switch(model or "default", nxt or "default", f"{s.kind}: unstick by switching model")
            tel.emit("switch", model=nxt, old=model, reason=f"{s.kind}: {s}")
            message = (f"[supervisor] The previous attempt got stuck ({s}). Do not repeat what failed. "
                       f"Take a different approach and finish the original task:\n{task}")
        except urllib.error.HTTPError as e:
            ui.error(f"HTTP {e.code}: {e.read().decode()[:300]}")
            return ""
    ui.error("gave up after max attempts")
    return ""
