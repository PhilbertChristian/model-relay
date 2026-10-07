---
tags: [night-shift, config]
area: night-shift
updated: 2026-10
---

How the night shift decides what capacity will go to waste and how much to burn tonight. Source: `relay/capacity.py`. Configured in `burner.json`; the flow that consumes it is [Relay-Night-Shift](Relay-Night-Shift.md).

`burner.json` holds `idle`, `subscriptions`, and optionally a model ladder in the same shape as `relay.json` ([Relay-Config](Relay-Config.md)). With no `models` key, the built-in default ladder is merged in (`relay/__main__.py`). Worked example: `examples/burner-demo.json` (demo values, not real plan limits).

## Idle hours

```json
"idle": {"timezone": "America/Los_Angeles", "weekday": "23:00-07:00", "weekend": "all"}
```

- Values: `"HH:MM-HH:MM"` (may cross midnight), `"all"`, or `"none"`.
- Per-day keys (`mon` … `sun`) override `weekday` / `weekend`.
- Timezone falls back to `$TZ`, then UTC.
- Default when `idle` is absent: weekdays `23:00-07:00`, weekends `all`.

## Subscription kinds

Common keys: `name`, `kind`, `providers` (provider names from the ladder this subscription pays for; default `[name]`), `reserve_pct` (share kept for your daytime use; default 30).

| Kind | Remaining comes from | Extra keys |
|---|---|---|
| `agent37_budget` | `GET /v1/instances/{id}/budget` when `AGENT37_API_KEY` and `instance` are set; else `monthly_usd` minus the local ledger | `instance`, `monthly_usd`, `daytime_usd_per_day` |
| `api_budget` | `monthly_usd` minus spend in Relay's ledger (`.relay/events.jsonl`) | `monthly_usd`, `reset_day`, `daytime_usd_per_day` |
| `rolling_window` | what you describe; these plans expose no usage API to Relay | `window_hours` (default 5), `windows_per_week` (default 20), `weekly_reset` (e.g. `"mon 09:00"`), `windows_used_this_week` |

Monthly kinds reset on `reset_day` (default 1) in UTC.

## The numbers

For dollar budgets:

- **tonight** = remaining × (1 − reserve) ÷ idle nights left before reset
- **expiring** = remaining − `daytime_usd_per_day` × days left

For rolling windows:

- **tonight** = how many whole windows fit in the current/next idle window, capped by windows remaining
- **expiring** = min(remaining × (1 − reserve), whole windows that fit in idle time before the weekly reset)

**Burn order**: highest expiring ÷ remaining first, then soonest reset. The night shift hands this order to the router as provider priority, and dollar `tonight` values as hard per-shift allowances. Rolling-window `tonight` is reported but not enforced as a cap.
