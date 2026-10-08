"""Agent37 lane: burn Agent37 credits on burn-week tasks.

Edits happen locally in the task's worktree through Relay's engine (RelayLane); only the model calls go out, to
Agent37's OpenAI-compatible LLM proxy: the `agent37` provider in relay/config.py (AGENT37_LLM_PROXY_URL +
AGENT37_MANAGED_TOKEN). So Agent37 receives only the task text plus the files the agent reads, after the secret
filter, and nothing is uploaded or pushed. (deploy/agent37.py's instance paths work on a remote checkout, which
would mean shipping the repo out and the results back.) Opt-in: unavailable until the token is set. A 402 /
instance_budget_exhausted ends the task "limited" until the subscription's monthly reset.
"""
from __future__ import annotations

from .relay_api import RelayLane


class Agent37Lane(RelayLane):
    kind = "agent37"
    hint = (" (opt-in: Agent37 injects AGENT37_MANAGED_TOKEN and AGENT37_LLM_PROXY_URL inside an instance; "
            "set them here to burn Agent37 credits)")

    def allowed(self, provider: str) -> bool:
        return provider in (self.cfg.get("providers") or ["agent37"])
