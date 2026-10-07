---
tags: [engine, agent37]
area: engine
updated: 2026-10
---

`relay supervise` watches an agent already hosted on an Agent37 instance (Hermes, OpenClaw, OpenCode, Claude Code, Codex…) and unsticks it by switching models. It does not run tools itself. Source: `relay/supervise.py`. For Relay's own agent, see [Relay-Unstick-Ladder](Relay-Unstick-Ladder.md).

```bash
export AGENT37_API_KEY=sk_live_...
python3 -m relay supervise --instance <id> "task"
python3 deploy/agent37.py supervise "task"     # same, using the instance from `create`
```

| Flag | Default | Meaning |
|---|---|---|
| `--models` | first 4 from the instance's `GET /v1/models`, default model first | Comma-separated model ladder |
| `--agent` | instance default | Harness on the instance |
| `--hang` | 180 | Seconds without any agent event before the turn counts as hung |

## How it works

Each attempt sends one turn to `POST /v1/responses` with `stream: true` and watches the event stream. When the agent is trapped, Relay cancels the turn and re-sends it on the **same session** with the next model in the ladder, so the new model sees the full history. Up to 6 attempts.

| Trap | Detection |
|---|---|
| Loop | Same tool with the same arguments started 3 times |
| Tool errors | 3 `tool_call.failed` in a row |
| Hung | Only keepalives for `--hang` seconds, or the stream goes silent |
| Limit | `response.failed`, or a short reply mentioning a rate, budget, quota or context limit |
| Same answer | The reply is identical to the previous attempt |

## Refusals

There is no independent judge in supervisor mode. A refusal is shown and the run stops; Relay will not switch models to get around it. To get refusal triage, run the task locally with `relay run`.
