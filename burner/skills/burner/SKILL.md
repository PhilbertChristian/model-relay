---
name: burner
description: >-
  Turns leftover weekly quota into project work with BurnerAgent, Agent37, and
  Monid, including the no-key demo. Use when an Orca coordinator should launch
  BurnerAgent, record the demo video, or burn remaining weekly quota.
---

# BurnerAgent

You are an Orca coordinator. Run BurnerAgent from the BurnerAgent repo. It spends leftover weekly quota on real work in isolated git worktrees, using Agent37 and Monid when their keys are set.

## Demo

For the video, with no keys:

```bash
npx tsx src/cli.ts demo
```

## Live burn

When `AGENT37_API_KEY` / `MONID_API_KEY` are set:

```bash
npx tsx src/cli.ts run
```

The dashboard is on port 3737 (`http://127.0.0.1:3737`).

## Safety rules

- Worktrees only. Agents run in isolated git worktrees, never in the user's main checkout.
- Never git push. Never touch remotes.
- Never read `.env` into prompts. Never pass credentials to a lane.
- Stop on usage limits (no retries, no account switching).
