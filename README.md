# Relay Night Shift: your AI subscriptions work while you sleep

**Live page + demo:** https://philbertchristian.github.io/model-relay/

You pay for AI capacity that mostly goes unused: Agent37 credit, an OpenAI budget, Claude/ChatGPT plan windows that reset whether you used them or not. Meanwhile your weekend projects sit in a planning doc.

**Relay Night Shift** reads your planning docs, finds your downtime and the subscription capacity that will expire unused, and spends it overnight. It builds, tests, and commits each task on a branch and opens a draft PR. You wake up to `MORNING.md`: what got done, what needs you, and how much capacity it used.

```bash
python3 -m relay burn capacity -b burner.json            # what expires, when you're idle, burn order
python3 -m relay burn plan PLAN.md -b burner.json        # tonight's queue
python3 -m relay burn run PLAN.md -b burner.json --wait  # sleep until downtime, then work the queue
```

- **Capacity planner** (`relay/capacity.py`): Agent37 budget (live from the API), metered API budgets (from Relay's ledger), and rolling-window coding plans (Claude Max/Pro, ChatGPT Pro/Codex). It computes tonight's allowance per subscription, keeps a reserve for your daytime use, and burns whatever is most likely to be wasted first.
- **Plans → queue** (`relay/plan.py`): a markdown checklist; `## project` with `repo:`/`dir:`, `test:`, `budget:`, `priority:`, `notes:`. Results are written back as `[x]` and `[!] blocked: …`.
- **Night shift** (`relay/burn.py`): one agent per task, a test gate after every task, a commit per task on `relay/night-<date>`, and a draft PR per project (with `GITHUB_TOKEN`). Tasks that need a human end as `BLOCKED:` instead of guessing.
- **Runs on Agent37**: an always-on instance, started by an Agent37 **platform cron** that fires even while the box sleeps (`deploy/agent37.py schedule`).
- **Never stalls overnight**: the Relay engine below fails over on limits and unsticks loops, hangs and dead ends with nobody awake.

### Night shift on Agent37

```bash
export AGENT37_API_KEY=sk_live_...  OPENAI_API_KEY=sk-...  GITHUB_TOKEN=ghp_...
python3 deploy/agent37.py create --always-on --budget 5
python3 deploy/agent37.py push --plan PLAN.md --burner burner.json
python3 deploy/agent37.py schedule --at 23:00 --tz America/Los_Angeles --days 5,6   # Fri + Sat nights
python3 deploy/agent37.py morning
```

Offline demo (no keys): `python3 -m relay -C /tmp/night burn run examples/PLAN.md -b examples/burner-demo.json --now`

---

## The engine: Relay, an agent that unsticks itself

Agents get stuck all the time. They hit a rate limit or budget wall, retry the same broken command forever, start a process that never exits, keep digging down the wrong path, or refuse an ordinary task. Relay detects all of these, then climbs an **unstick ladder**:

1. **Second opinion.** A stronger model diagnoses the trace and the cheap model keeps driving.
2. **Swap.** The conversation is handed to a stronger tier, with automatic de-escalation once things are moving again.
3. **Kill & reset.** The context is trimmed to the task plus lessons learned, and a fresh plan starts on the next model.

Limits (429 / 402 budget / quota / context overflow / per-model soft caps) skip the ladder and fail over immediately.

**Refusals** are reviewed by an independent model. An over-refusal of a benign task (e.g. "kill the process on port 8000") retires the refusing model and a sibling takes over. A legitimate refusal is **upheld**: Relay stops for a human and never shops for a model that will say yes. It also stops if a second model refuses, if the review fails, or if it is running in supervisor mode.

It works two ways:

- **`relay run`**: an OpenCode-style coding agent (read/write/edit/bash) that unsticks itself.
- **`relay supervise`**: watches an agent already hosted on **Agent37** (Hermes, OpenClaw, OpenCode, Claude Code…) through its live event stream, cancels trapped turns, and re-sends them on the same session with a different `model`.

Pure Python 3.10+, zero dependencies.

## Quick start (offline, no keys)

```bash
python3 -m relay -c examples/demo-mock.json -C /tmp/demo -y run "create hello_relay.py and run it"
python3 -m relay -c examples/demo-hang.json -C /tmp/demo -y run "start the server"
python3 -m relay -c examples/demo-refusal.json -C /tmp/demo -y run "kill whatever is running on port 8000"       # over-refusal -> unstuck
python3 -m relay -c examples/demo-refusal.json -C /tmp/demo -y run "write a keylogger that emails me passwords"  # refusal upheld
python3 -m relay -C /tmp/demo stats
python3 -m unittest discover tests
```

## Real models

| Provider | Where | Env |
|---|---|---|
| Agent37 LLM router (OpenRouter catalog, metered against the instance budget) | inside an Agent37 instance only | `AGENT37_LLM_PROXY_URL`, `AGENT37_MANAGED_TOKEN` (injected automatically) |
| OpenAI | anywhere | `OPENAI_API_KEY` |
| Any OpenAI-compatible API | anywhere | add it to `providers` in `relay.json` |

```bash
cp relay.example.json relay.json      # edit the ladder: tiers, prices, caps
python3 -m relay doctor               # which models are reachable
python3 -m relay                      # interactive: /status, /tier N
```

### On Agent37

```bash
export AGENT37_API_KEY=sk_live_...  OPENAI_API_KEY=sk-...   # optional: SUPABASE_URL, SUPABASE_KEY
python3 deploy/agent37.py create --budget 2   # instance + $2 managed-LLM headroom, env forwarded
python3 deploy/agent37.py push                # ship relay + relay.json
python3 deploy/agent37.py doctor
python3 deploy/agent37.py run "build a todo CLI in python with tests"
python3 deploy/agent37.py run --tier 2 "…"    # start this deployment on the strong tier
python3 deploy/agent37.py supervise "…"       # unstick the instance's own hosted agent
python3 deploy/agent37.py stats | budget --top-up 1 | destroy
```

## Config (`relay.json`)

```jsonc
{
  "start_tier": 0, "deescalate_after": 4, "max_steps": 40,
  "providers": { "agent37": {"base_url": "${AGENT37_LLM_PROXY_URL:-https://api.agent37.com/llm/v1}", "api_key_env": "AGENT37_MANAGED_TOKEN"} },
  "models": [
    {"id": "a37-default", "provider": "agent37", "model": "default", "tier": 0, "price_in": 0.1, "price_out": 0.4,
     "context_window": 128000, "max_usd": 0.50, "max_tokens": 2000000}
  ]
}
```

## Telemetry (Supabase)

Run `supabase/schema.sql`, then set `SUPABASE_URL` + `SUPABASE_KEY`. Every call, switch, blocker, hint, and session summary lands in `relay_events`, with views `relay_model_usage` and `relay_switches`. Events are always written locally to `.relay/events.jsonl`.

## Layout

```
relay/capacity.py   subscription capacity + downtime windows + burn order
relay/plan.py       planning doc <-> task queue
relay/burn.py       night shift orchestrator: queue -> agents -> tests -> commits -> PRs -> MORNING.md
relay/router.py     model ladder: tiers, cooldowns, dead providers, context floor, soft caps, de-escalation
relay/blockers.py   loop, error-streak, hung, no-progress, malformed, refusal detection
relay/agent.py      agent loop + unstick ladder (hint → swap → reset)
relay/tools.py      read/write/edit/list/bash (process-group watchdog)/escalate
relay/providers.py  OpenAI-compatible client, error → limit-kind classifier, scripted mock
relay/supervise.py  supervisor for Agent37-hosted agents (SSE stream watch + cancel + model switch)
deploy/agent37.py   create / push / run / supervise / stats / budget / destroy
docs/index.html     GitHub Pages site
```

Built at the Agent37 "Build an Agent" hackathon (Oct 7, 2026). MIT.
