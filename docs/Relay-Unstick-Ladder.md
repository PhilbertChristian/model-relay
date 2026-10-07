---
tags: [engine]
area: engine
updated: 2026-10
---

How `relay run` notices it is stuck and gets itself moving again. Sources: `relay/blockers.py` (detection), `relay/agent.py` (ladder and refusals), `relay/router.py` (tiers and limits), `relay/tools.py` (hang watchdog). For agents hosted on Agent37, see [Relay-Supervisor-Mode](Relay-Supervisor-Mode.md).

## Blockers

Defaults from `BlockerDetector` in `relay/blockers.py`:

| Blocker | Trigger |
|---|---|
| Loop | Same tool, same arguments, same result 3 times within the last 8 calls |
| Error streak | 3 tool errors in a row |
| Hung process | A `bash` command never exited and was killed by the watchdog |
| Wrong approach | 10 tool steps without changing any file |
| Malformed | 2 empty or unparseable replies in a row |
| Escalate | The model called the `escalate` tool |

## The ladder

Each blocker climbs one rung:

1. **Second opinion.** The strongest healthy model on a higher tier reads the recent trace and replies with at most five bullets of advice; the current model keeps driving. Skipped when no higher-tier model is healthy.
2. **Swap.** The router moves up one tier and the conversation is handed over. At the top tier it rotates to a sibling model instead (the stuck one cools down for `base_cooldown` seconds).
3. **Kill and reset.** The context is cut to the system prompt, the task, the last four lessons from second opinions, and the list of files already changed; a fresh plan starts on the next model. Only happens once at least one lesson exists.

After `deescalate_after` clean steps (default 4) the ladder resets to rung 1, and the router steps back down one tier to save cost.

## Limits

Provider errors skip the ladder and act immediately (`Router.record_error`):

| Error | Action |
|---|---|
| Rate limit (429) | Cool that model down (`Retry-After`, else exponential from `base_cooldown`); a sibling or neighbouring tier takes over |
| Budget (402 / quota) or auth | Provider disabled for the session |
| Context overflow | Only models with a bigger window stay eligible; if none, history is compacted |
| Unknown model | Model retired for the session |
| Soft cap (`max_usd` / `max_tokens` per model) | Model retired for the session |
| Other | Short cooldown (up to 60 s) |

If every model is cooling down, Relay waits up to 120 s for the soonest one before giving up.

## Refusals

A short final reply that matches the refusal patterns in `relay/blockers.py` goes to triage. The strongest healthy model other than the refuser judges it, defaulting to "decline" when in doubt.

- **Benign (over-refusal)**: the refusal is dropped from the history, the refusing model is retired for the session, and another model takes the task.
- **Upheld**: the run stops for a human. Relay does not look for a model that will say yes.

The refusal is also upheld without retrying when a second model refuses, no independent judge is available, the review fails, or Relay is in supervisor mode.

## Demos

```bash
python3 -m relay -c examples/demo-mock.json -C /tmp/demo -y run "create hello_relay.py and run it"
python3 -m relay -c examples/demo-hang.json -C /tmp/demo -y run "start the server"
python3 -m relay -c examples/demo-refusal.json -C /tmp/demo -y run "kill whatever is running on port 8000"       # over-refusal -> unstuck
python3 -m relay -c examples/demo-refusal.json -C /tmp/demo -y run "write a keylogger that emails me passwords"  # refusal upheld
```
