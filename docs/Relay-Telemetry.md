---
tags: [telemetry]
area: engine
updated: 2026-10
---

Every model call, switch, blocker, hint, refusal and session summary is logged as an event. Sources: `relay/telemetry.py`, `supabase/schema.sql`.

## Local log

Always written to `<cwd>/.relay/events.jsonl`, one JSON object per line with `ts`, `session`, `host` (`$AGENT37_INSTANCE_ID` or `local`) and `event`. This file is also the spend ledger the capacity planner reads ([Relay-Capacity-Planner](Relay-Capacity-Planner.md)).

```bash
python3 -m relay -C /tmp/demo stats   # calls and cost per model, switches by reason, spend vs top-tier-only baseline
```

The baseline is what the same tokens would have cost on the priciest model in the ladder.

## Supabase

1. Run `supabase/schema.sql`. It creates table `relay_events` and views `relay_model_usage` and `relay_switches`.
2. Set `SUPABASE_URL` and `SUPABASE_KEY` (or `SUPABASE_SERVICE_ROLE_KEY`). Optional: `SUPABASE_TABLE` (default `relay_events`).

Rows are posted in a background thread through PostgREST. Failures are swallowed so telemetry never breaks a run.
