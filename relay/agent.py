"""The agent loop: one conversation, many models."""
from __future__ import annotations

import json
import os
import platform

from . import ui
from .blockers import BlockerDetector
from .providers import ProviderError
from .router import NoModelAvailable, Router
from .telemetry import Telemetry
from .tools import SCHEMAS, Escalate, Toolbox

SYSTEM = """You are Relay, an autonomous coding agent working in {cwd} on {os}.
Use the tools to inspect files, edit code and run commands. Verify your work by running it.
Be concise. When the task is complete, reply with a short summary and no tool calls.
If you are stuck after a couple of attempts, call `escalate` with the reason: a stronger model will take over.
You may be one of several models working on this task in turn; earlier turns may be from another model."""


def _approx_tokens(messages: list[dict]) -> int:
    return sum(len(json.dumps(m)) for m in messages) // 4


def _compact(messages: list[dict], keep: int = 12) -> list[dict]:
    """Keep the system prompt, the original task, and the last `keep` messages
    (starting on a non-tool message so no tool result is orphaned)."""
    head = messages[:2]
    tail = messages[-keep:]
    while tail and tail[0].get("role") == "tool":
        tail = tail[1:]
    note = {"role": "user", "content": "[relay] Earlier history was compacted to fit the context window. Continue the task."}
    return head + [note] + tail


class Agent:
    def __init__(self, router: Router, tools: Toolbox, telemetry: Telemetry, max_steps: int = 40):
        self.router = router
        self.tools = tools
        self.tel = telemetry
        self.max_steps = max_steps
        self.blockers = BlockerDetector()
        self.rung = 0              # position on the unstick ladder
        self.rung_clean = 0
        self.lessons: list[str] = []
        self.task = ""
        self.messages: list[dict] = [{"role": "system", "content": SYSTEM.format(cwd=tools.root, os=platform.system())}]
        self.tel.emit("session_start", cwd=str(tools.root), models=[m.id for m in router.models])

    def _switch_note(self, old: str | None, new: str, reason: str) -> None:
        ui.switch(old, new, reason)
        self.tel.emit("switch", model=new, old=old, reason=reason, tier=self.router.tier)
        self.blockers.reset()
        if old is not None:
            self.messages.append({"role": "user", "content":
                f"[relay] Model handoff: {old} -> {new} ({reason}). Review the conversation so far and continue the task."})

    def run(self, task: str) -> str:
        self.task = task
        self.messages.append({"role": "user", "content": task})
        final = ""
        for step in range(1, self.max_steps + 1):
            try:
                spec, switch = self.router.pick()
            except NoModelAvailable as e:
                if self.router.min_context:
                    ui.warn("no model has a big enough window; compacting history")
                    self.messages = _compact(self.messages)
                    self.router.min_context = 0
                    continue
                ui.error("no model available:\n" + str(e))
                self.tel.emit("abort", reason="no model available")
                break
            if switch:
                self._switch_note(switch.old, switch.new, switch.reason)

            provider = self.router.providers[spec.provider]
            ui.thinking(step, spec.id, self.router.tier)
            try:
                comp = provider.chat(spec.model, self.messages, SCHEMAS)
            except ProviderError as err:
                why = self.router.record_error(spec.id, err, approx_context=_approx_tokens(self.messages))
                ui.warn(why)
                self.tel.emit("provider_error", model=spec.id, kind=err.kind, status=err.status, detail=str(err)[:300])
                continue

            usd = self.router.record_success(spec.id, comp.input_tokens, comp.output_tokens, comp.cost_usd)
            self.tel.emit("call", model=spec.id, provider=spec.provider, input_tokens=comp.input_tokens,
                          output_tokens=comp.output_tokens, usd=usd, latency_s=round(comp.latency_s, 2), tier=spec.tier)
            msg = comp.message
            self.messages.append(msg)
            if msg.get("content"):
                ui.say(msg["content"])

            calls = msg.get("tool_calls") or []
            if not calls:
                if not (msg.get("content") or "").strip():
                    blocker = self.blockers.on_malformed("empty reply")
                    self.messages.pop()
                    if blocker:
                        self._blocked(blocker)
                    continue
                final = msg["content"]
                break

            blocker = None
            files_before = len(self.tools.files_changed)
            for c in calls:
                fn = c.get("function") or {}
                name, args = fn.get("name", ""), fn.get("arguments") or "{}"
                ui.tool(name, args)
                try:
                    out, is_err = self.tools.run(name, args)
                except Escalate as e:
                    out, is_err = f"escalation requested: {e}", False
                    blocker = blocker or f"model asked to escalate: {e}"
                ui.tool_result(out, is_err)
                self.messages.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": out})
                self.tel.emit("tool", model=spec.id, tool=name, error=is_err, hung=self.tools.hung)
                changed = len(self.tools.files_changed)
                b = self.blockers.on_tool_result(name, args, out, is_err, hung=self.tools.hung,
                                                 progressed=changed > files_before)
                files_before = changed
                blocker = blocker or b

            if blocker:
                self._blocked(blocker)
            else:
                self.rung_clean += 1
                if self.rung_clean >= self.router.deescalate_after:
                    self.rung = 0  # moving again: the next blocker starts back at the cheap rung
                self.router.record_clean_step()  # may de-escalate; announced by the next pick()
        else:
            ui.warn(f"stopped after {self.max_steps} steps")

        t = self.router.totals()
        baseline = self.router.counterfactual_usd()
        self.tel.emit("session_end", usd=t["usd"], baseline_usd=baseline, **{k: v for k, v in t.items() if k != "usd"})
        ui.summary(self.router, baseline)
        return final

    # ---------------------------------------------------------------- unstick
    # Rung 1  hint   a stronger model diagnoses the trace; the cheap model keeps driving
    # Rung 2  swap   hand the whole conversation to a stronger tier
    # Rung 3  reset  kill the poisoned context: keep the task + lessons learned, fresh plan, next model
    def _blocked(self, reason: str) -> None:
        self.rung += 1
        self.rung_clean = 0
        self.tel.emit("blocker", model=self.router.current, reason=reason, rung=self.rung)
        ui.warn(f"stuck: {reason}")
        if self.rung == 1 and self._hint(reason):
            return
        if self.rung <= 2 or not self.lessons:
            if not self.router.record_blocker(reason):
                ui.warn("already at the top tier; rotating to another model")
            if self.rung == 1:
                self.rung = 2
            return
        self._reset(reason)

    def _hint(self, reason: str) -> bool:
        """Ask the strongest healthy model (other than the current one) for a short diagnosis."""
        import time
        now = time.time()
        pool = [m for m in self.router.models if m.id != self.router.current and self.router._healthy(m, now)
                and m.tier > self.router.spec(self.router.current).tier]
        if not pool:
            return False
        coach = max(pool, key=lambda m: (m.tier, -m.price_in))
        trace = json.dumps(self.messages[-14:], default=str)[-12000:]
        ask = [{"role": "system", "content": "You are a senior engineer unblocking a coding agent. Be brief."},
               {"role": "user", "content": f"The agent is stuck: {reason}.\nOriginal task: {self.task}\n"
                f"Recent trace (JSON):\n{trace}\n\nIn at most 5 bullets: why is it stuck, and what should it do "
                f"differently next? Concrete commands or edits."}]
        try:
            comp = self.router.providers[coach.provider].chat(coach.model, ask, [])
        except ProviderError as err:
            self.router.record_error(coach.id, err)
            return False
        usd = self.router.record_success(coach.id, comp.input_tokens, comp.output_tokens, comp.cost_usd)
        hint = (comp.message.get("content") or "").strip()
        if not hint:
            return False
        self.lessons.append(f"{reason} -> {hint[:600]}")
        self.tel.emit("hint", model=coach.id, usd=usd, reason=reason)
        self.tel.emit("call", model=coach.id, provider=coach.provider, input_tokens=comp.input_tokens,
                      output_tokens=comp.output_tokens, usd=usd, latency_s=round(comp.latency_s, 2), tier=coach.tier)
        ui.hint(coach.id, hint)
        self.messages.append({"role": "user", "content": f"[relay] You are stuck ({reason}). Second opinion from "
                              f"{coach.id}:\n{hint}\nDo not repeat what failed. Apply this advice now."})
        self.blockers.reset()
        return True

    def _reset(self, reason: str) -> None:
        """Rung 3: throw away the poisoned context and restart from the task plus what we learned."""
        self.rung = 0
        files = sorted(self.tools.files_changed)
        self.messages = self.messages[:1] + [{"role": "user", "content":
            f"{self.task}\n\n[relay] A previous attempt got stuck and was reset. Lessons learned:\n- "
            + "\n- ".join(self.lessons[-4:])
            + (f"\nFiles already changed: {', '.join(files)}" if files else "")
            + "\nStart with a fresh plan. Inspect the current state first."}]
        self.blockers.reset()
        self.router.record_blocker(f"reset: {reason}")
        self.tel.emit("reset", model=self.router.current, reason=reason)
        ui.warn("reset: context trimmed to task + lessons, fresh plan")
