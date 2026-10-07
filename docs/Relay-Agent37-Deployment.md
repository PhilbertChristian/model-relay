---
tags: [deploy, agent37]
area: deploy
updated: 2026-10
---

Running Relay on an Agent37 instance and driving it from your laptop. Source: `deploy/agent37.py`. Instance state is saved in `.relay/instance.json`; pass `--instance ID` to target another.

```bash
export AGENT37_API_KEY=sk_live_...  OPENAI_API_KEY=sk-...   # optional: GITHUB_TOKEN, SUPABASE_URL, SUPABASE_KEY, SUPABASE_TABLE
```

`create` forwards whichever of `OPENAI_API_KEY`, `SUPABASE_URL`, `SUPABASE_KEY`, `SUPABASE_TABLE`, `GITHUB_TOKEN` are set into the instance environment.

## Coding agent

```bash
python3 deploy/agent37.py create --budget 2   # instance + $2 managed-LLM credit
python3 deploy/agent37.py push                # upload relay/ + relay.json to ~/relay-app
python3 deploy/agent37.py doctor              # which models are reachable from the instance
python3 deploy/agent37.py run "build a todo CLI in python with tests"
python3 deploy/agent37.py run --tier 2 "…"    # start on the strong tier
python3 deploy/agent37.py supervise "…"       # unstick the instance's own hosted agent
python3 deploy/agent37.py stats
python3 deploy/agent37.py budget --top-up 1   # read, or raise, the instance budget
python3 deploy/agent37.py destroy
```

`run`, `doctor`, `models` and `stats` execute `python3 -m relay` in `~/work` on the instance. `supervise` runs locally against the instance: [Relay-Supervisor-Mode](Relay-Supervisor-Mode.md). `shell CMD…` runs an arbitrary command.

## Night shift

```bash
export GITHUB_TOKEN=ghp_...                   # for draft PRs
python3 deploy/agent37.py create --always-on --budget 5
python3 deploy/agent37.py push --plan PLAN.md --burner burner.json
python3 deploy/agent37.py schedule --at 23:00 --tz America/Los_Angeles --days 5,6   # Fri + Sat nights
python3 deploy/agent37.py burn-now            # or start a shift right away
python3 deploy/agent37.py capacity            # burn capacity on the instance
python3 deploy/agent37.py morning             # MORNING.md + plan status
```

- `--always-on` disables auto-sleep so a multi-hour detached shift is not parked mid-run.
- `schedule` creates an Agent37 platform cron (`POST /v1/instances/{id}/crons`). Its prompt asks the instance's hosted agent to launch `burn run … --now` as a detached process logging to `~/work/burn.log`. `--days` is the cron day-of-week field (default `*`).
- Platform crons fire and wake the instance even while it sleeps, whereas a crontab inside the container would not [UNVERIFIED — from the `cmd_schedule` docstring, Agent37 docs not linked].

What the shift does once started: [Relay-Night-Shift](Relay-Night-Shift.md).
