"""Loads relay.json (or the built-in default ladder)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .providers import Provider
from .router import ModelSpec, Router

# Agent37's router is OpenAI-compatible and only reachable from inside an Agent37 instance,
# where AGENT37_LLM_PROXY_URL and AGENT37_MANAGED_TOKEN are injected automatically.
DEFAULT = {
    "start_tier": 0,
    "deescalate_after": 4,
    "max_steps": 40,
    "providers": {
        "agent37": {"base_url": "${AGENT37_LLM_PROXY_URL:-https://api.agent37.com/llm/v1}",
                    "api_key_env": "AGENT37_MANAGED_TOKEN"},
        "openai": {"base_url": "${OPENAI_BASE_URL:-https://api.openai.com/v1}", "api_key_env": "OPENAI_API_KEY"},
    },
    "models": [
        {"id": "a37-default", "provider": "agent37", "model": "default", "tier": 0,
         "price_in": 0.10, "price_out": 0.40, "context_window": 128000},
        {"id": "a37-deepseek", "provider": "agent37", "model": "deepseek/deepseek-v4-flash", "tier": 0,
         "price_in": 0.15, "price_out": 0.60, "context_window": 128000},
        {"id": "oai-mini", "provider": "openai", "model": "gpt-5-mini", "tier": 1,
         "price_in": 0.25, "price_out": 2.00, "context_window": 400000},
        {"id": "a37-sonnet", "provider": "agent37", "model": "anthropic/claude-sonnet-5", "tier": 2,
         "price_in": 3.00, "price_out": 15.00, "context_window": 1000000},
        {"id": "oai-gpt5", "provider": "openai", "model": "gpt-5", "tier": 2,
         "price_in": 1.25, "price_out": 10.00, "context_window": 400000},
    ],
}


def load(path: str | None) -> dict:
    candidates = [path] if path else [os.environ.get("RELAY_CONFIG"), "relay.json", str(Path.home() / ".relay.json")]
    for c in candidates:
        if c and Path(c).exists():
            return json.loads(Path(c).read_text())
    if path:
        raise FileNotFoundError(path)
    return DEFAULT


def build_router(cfg: dict, start_tier: int | None = None) -> Router:
    providers = {name: Provider(name=name, **p) for name, p in cfg["providers"].items()}
    models = [ModelSpec(**m) for m in cfg["models"]]
    return Router(
        models=models,
        providers=providers,
        start_tier=cfg.get("start_tier", 0) if start_tier is None else start_tier,
        deescalate_after=cfg.get("deescalate_after", 4),
        base_cooldown=cfg.get("base_cooldown", 20.0),
    )
