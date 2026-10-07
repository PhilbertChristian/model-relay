"""The model ladder: which model runs the next step, and when to switch.

Models are grouped into tiers (0 = cheapest/fastest, higher = stronger/pricier).
The router always prefers the cheapest healthy model at the *current* tier, and
moves the current tier when something happens:

    blocker  (agent stuck)            -> escalate one tier
    N clean steps at elevated tier    -> de-escalate one tier (save money)
    rate limit / transient error      -> cooldown that model, sibling or neighbour tier takes over
    budget / auth error               -> provider disabled for the session
    context overflow                  -> only models with a bigger context window are eligible
    soft limit (session $ / tokens)   -> model retired for the session
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .providers import Provider, ProviderError


@dataclass
class ModelSpec:
    id: str                       # handle used in logs, e.g. "a37-flash"
    provider: str
    model: str                    # id sent to the provider
    tier: int = 0
    context_window: int = 128_000
    price_in: float = 0.0         # USD per 1M input tokens (used when provider reports no cost)
    price_out: float = 0.0        # USD per 1M output tokens
    max_usd: float | None = None  # soft limit per session
    max_tokens: int | None = None # soft limit per session (in+out)


@dataclass
class ModelState:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    errors: int = 0
    cooldown_until: float = 0.0
    retired: str | None = None    # reason, when permanently out for this session


@dataclass
class Switch:
    old: str | None
    new: str
    reason: str


class NoModelAvailable(Exception):
    pass


@dataclass
class Router:
    models: list[ModelSpec]
    providers: dict[str, Provider]
    start_tier: int = 0
    deescalate_after: int = 4
    base_cooldown: float = 20.0
    state: dict[str, ModelState] = field(default_factory=dict)
    tier: int = 0
    current: str | None = None
    min_context: int = 0
    clean_steps: int = 0
    dead_providers: dict[str, str] = field(default_factory=dict)
    # night shift: [{"name", "providers", "usd", "spent"}] allowances, and provider -> burn order
    allowances: list[dict] = field(default_factory=list)
    provider_priority: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.tier = self.start_tier
        for m in self.models:
            self.state.setdefault(m.id, ModelState())
        for name, p in self.providers.items():
            ok, why = p.available()
            if not ok:
                self.dead_providers[name] = why

    # ------------------------------------------------------------- selection
    @property
    def max_tier(self) -> int:
        return max((m.tier for m in self.models), default=0)

    def spec(self, model_id: str) -> ModelSpec:
        return next(m for m in self.models if m.id == model_id)

    def _healthy(self, m: ModelSpec, now: float) -> bool:
        s = self.state[m.id]
        return (
            m.provider not in self.dead_providers
            and s.retired is None
            and s.cooldown_until <= now
            and m.context_window >= self.min_context
        )

    def _cost_rank(self, m: ModelSpec) -> tuple:
        # burn expiring subscriptions first, then cheapest
        return (self.provider_priority.get(m.provider, 99), m.price_in + m.price_out * 3, self.state[m.id].errors)

    def pick(self) -> tuple[ModelSpec, Switch | None]:
        """Cheapest healthy model at the current tier; else nearest tier, preferring up."""
        now = time.time()
        order = [self.tier] + [t for d in range(1, self.max_tier + 2) for t in (self.tier + d, self.tier - d)]
        for t in order:
            if t < 0 or t > self.max_tier:
                continue
            pool = sorted((m for m in self.models if m.tier == t and self._healthy(m, now)), key=self._cost_rank)
            # stick with the current model if it is still a valid choice at this tier
            cur = next((m for m in pool if m.id == self.current), None)
            choice = cur or (pool[0] if pool else None)
            if choice:
                switch = None
                if choice.id != self.current:
                    switch = Switch(self.current, choice.id, self._pending_reason or f"start: tier {t}")
                    self.current = choice.id
                self._pending_reason = None
                return choice, switch
        # everything is cooling down? wait for the soonest one rather than dying
        cooling = [m for m in self.models if m.provider not in self.dead_providers and self.state[m.id].retired is None
                   and m.context_window >= self.min_context]
        if cooling:
            soonest = min(cooling, key=lambda m: self.state[m.id].cooldown_until)
            wait = self.state[soonest.id].cooldown_until - now
            if 0 < wait <= 120:
                time.sleep(wait)
                return self.pick()
        raise NoModelAvailable(self.status_line())

    _pending_reason: str | None = None

    # --------------------------------------------------------------- signals
    def record_success(self, model_id: str, input_tokens: int, output_tokens: int, cost: float | None) -> float:
        m, s = self.spec(model_id), self.state[model_id]
        usd = cost if cost is not None else (input_tokens * m.price_in + output_tokens * m.price_out) / 1e6
        s.calls += 1
        s.input_tokens += input_tokens
        s.output_tokens += output_tokens
        s.usd += usd
        s.errors = 0
        for a in self.allowances:
            if m.provider in a["providers"]:
                a["spent"] = a.get("spent", 0.0) + usd
                if a["usd"] is not None and a["spent"] >= a["usd"]:
                    for prov in a["providers"]:
                        self.dead_providers.setdefault(prov, f"tonight's {a['name']} allowance burned (${a['spent']:.4f})")
                    self._pending_reason = f"allowance: {a['name']} burned ${a['spent']:.4f} of ${a['usd']:.4f} tonight"
        # soft limits
        if m.max_usd is not None and s.usd >= m.max_usd:
            self._retire(model_id, f"soft_limit: ${s.usd:.4f} >= ${m.max_usd} cap")
        elif m.max_tokens is not None and s.input_tokens + s.output_tokens >= m.max_tokens:
            self._retire(model_id, f"soft_limit: {s.input_tokens + s.output_tokens} tokens >= {m.max_tokens} cap")
        return usd

    def record_clean_step(self) -> bool:
        """Called after a step with no blocker. Returns True if we de-escalated."""
        self.clean_steps += 1
        if self.tier > self.start_tier and self.clean_steps >= self.deescalate_after:
            self.tier -= 1
            self.clean_steps = 0
            self._pending_reason = f"de-escalate: {self.deescalate_after} clean steps, back to tier {self.tier} to save cost"
            return True
        return False

    def record_blocker(self, reason: str) -> bool:
        """Agent is stuck. Returns True if there was a higher tier to go to."""
        self.clean_steps = 0
        if self.tier < self.max_tier:
            self.tier += 1
            self._pending_reason = f"escalate: {reason}"
            return True
        # already at the top: try a different model at the same tier
        if self.current:
            self.state[self.current].cooldown_until = time.time() + self.base_cooldown
            self._pending_reason = f"rotate: blocked at top tier ({reason})"
        return False

    def record_error(self, model_id: str, err: ProviderError, approx_context: int = 0) -> str:
        m, s = self.spec(model_id), self.state[model_id]
        s.errors += 1
        now = time.time()
        if err.kind == "rate_limit":
            s.cooldown_until = now + (err.retry_after or self.base_cooldown * (2 ** min(s.errors - 1, 4)))
            why = f"rate_limit: {model_id} cooling {s.cooldown_until - now:.0f}s"
        elif err.kind in ("budget", "auth"):
            self.dead_providers[m.provider] = f"{err.kind}: {err}"
            why = f"{err.kind}: provider '{m.provider}' exhausted -> disabled"
        elif err.kind == "context":
            self.min_context = max(self.min_context, m.context_window + 1, approx_context)
            why = f"context: {model_id} window {m.context_window} overflowed -> need bigger window"
        elif err.kind == "bad_model":
            self._retire(model_id, f"bad model: {err}")
            why = f"bad_model: {m.model} rejected by provider -> retired"
        else:
            s.cooldown_until = now + min(5 * s.errors, 60)
            why = f"transient: {model_id} {err}"
        self._pending_reason = why
        return why

    def _retire(self, model_id: str, reason: str) -> None:
        self.state[model_id].retired = reason
        self._pending_reason = reason

    def has_capacity(self) -> bool:
        return any(m.provider not in self.dead_providers and self.state[m.id].retired is None for m in self.models)

    # ---------------------------------------------------------------- report
    def totals(self) -> dict[str, Any]:
        return {
            "usd": sum(s.usd for s in self.state.values()),
            "input_tokens": sum(s.input_tokens for s in self.state.values()),
            "output_tokens": sum(s.output_tokens for s in self.state.values()),
            "calls": sum(s.calls for s in self.state.values()),
        }

    def counterfactual_usd(self) -> float:
        """What the same tokens would have cost on the priciest model: the 'always use the best' baseline."""
        top = max(self.models, key=lambda m: (m.price_in, m.price_out))
        t = self.totals()
        return (t["input_tokens"] * top.price_in + t["output_tokens"] * top.price_out) / 1e6

    def status_line(self) -> str:
        parts = []
        for m in self.models:
            s = self.state[m.id]
            flag = ("x " + s.retired) if s.retired else (
                f"dead provider ({self.dead_providers[m.provider]})" if m.provider in self.dead_providers else
                f"cooling {s.cooldown_until - time.time():.0f}s" if s.cooldown_until > time.time() else "ok")
            parts.append(f"  t{m.tier} {m.id:<18} {flag}")
        return "\n".join(parts)
