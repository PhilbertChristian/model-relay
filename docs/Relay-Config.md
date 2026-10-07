---
tags: [config]
area: engine
updated: 2026-10
---

`relay.json`: providers, the model ladder, and engine knobs. Sources: `relay/config.py`, `relay/router.py` (`ModelSpec`), `relay/providers.py`. Full example: `relay.example.json`. Night-shift keys (`idle`, `subscriptions`) live in `burner.json`: [Relay-Capacity-Planner](Relay-Capacity-Planner.md).

```bash
cp relay.example.json relay.json      # edit the ladder: tiers, prices, caps
python3 -m relay doctor               # which providers and models are reachable
python3 -m relay models               # every model each provider serves
python3 -m relay                      # interactive: /status, /tier N
```

## Lookup order

`-c PATH`, else `$RELAY_CONFIG`, else `./relay.json`, else `~/.relay.json`, else the built-in default ladder in `relay/config.py`.

## Providers

| Provider | Where | Env |
|---|---|---|
| Agent37 LLM router | inside an Agent37 instance only | `AGENT37_LLM_PROXY_URL`, `AGENT37_MANAGED_TOKEN` (injected by the platform) |
| OpenAI | anywhere | `OPENAI_API_KEY`, optional `OPENAI_BASE_URL` |
| Any OpenAI-compatible API | anywhere | add an entry under `providers` |

Agent37's router serves models from the OpenRouter catalog and bills them against the instance budget [UNVERIFIED — stated in `deploy/agent37.py`, Agent37 docs not linked].

A provider entry has `base_url` (supports `${VAR:-default}`) and `api_key_env`. `"kind": "mock"` with a `mock` block gives a scripted offline provider, used by everything in `examples/`.

## Top-level keys

| Key | Default | Meaning |
|---|---|---|
| `start_tier` | 0 | Tier the router starts on (`--tier` overrides) |
| `deescalate_after` | 4 | Clean steps before stepping down a tier |
| `max_steps` | 40 | Agent steps per task (`--max-steps` overrides) |
| `base_cooldown` | 20 | Seconds; base for rate-limit backoff and top-tier rotation |

## Model fields

```jsonc
{"id": "a37-default", "provider": "agent37", "model": "default", "tier": 0,
 "price_in": 0.1, "price_out": 0.4, "context_window": 128000, "max_usd": 0.50, "max_tokens": 2000000}
```

| Field | Meaning |
|---|---|
| `id` | Handle used in logs and stats |
| `provider` / `model` | Provider name, and the model id sent to it |
| `tier` | 0 = cheapest; higher = stronger. The router picks the cheapest healthy model at the current tier |
| `price_in` / `price_out` | USD per 1M tokens, used when the provider reports no cost |
| `context_window` | Tokens; drives context-overflow failover (default 128000) |
| `max_usd` / `max_tokens` | Per-session soft caps; reaching one retires the model |

How tiers move and what each error does: [Relay-Unstick-Ladder](Relay-Unstick-Ladder.md).
