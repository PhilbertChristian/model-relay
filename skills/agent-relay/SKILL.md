---
name: agent-relay
description: >-
  Spend AI plan capacity that would otherwise expire on useful work in the user's repos, with Agent Relay
  choosing what to do and how much to spend and Orca workers doing it. Use when the user asks to burn
  leftover Claude or ChatGPT plan capacity, run a night shift, or coordinate Relay tasks from an Orca
  coordinator.
---

You are an Orca coordinator. Agent Relay is your planner: it knows which subscriptions reset soon with capacity
left over, and which tasks matter most. Orca is how you run the work. Load Orca's `orchestration` skill as well.

## Setup, once

```bash
claude mcp add relay -- python3 -m relay.mcp     # run from a model-relay checkout
```

## Each shift

1. Call `relay_capacity`. It reports what expires unused, tonight's allowance per subscription and the burn
   order. If nothing is left, stop and say so.
2. Call `relay_plan_list` for the queued tasks in run order. Queue new ones with `relay_plan_add`, one
   self-contained, testable task per call.
3. For each task, while the allowance lasts, start one supervised worker in a fresh worktree off the repo's
   default branch:

   ```bash
   orca orchestration worker-start --spec "<task>. Run <test command> before you finish." \
     --worktree new-top-level --repo path:<repo> --base-branch <default branch> --agent claude --json
   ```

   Use the agent whose plan comes first in Relay's burn order (`claude` or `codex`), and never start more
   workers than the allowance covers.
4. Nothing waits on the user: you are the one who keeps the shift moving. Follow progress with
   `orca orchestration worker-read`, and answer a worker's questions with `orca orchestration reply`, picking
   the most reasonable choice that can be undone. When a worker stalls, keep it driving with a concrete next
   step or a second opinion, or restart it on a stronger model. If a task needs something no agent can have,
   such as a live payment key, let the worker finish what it can on its branch, note what is missing, and hand
   it the next task.
5. When the shift ends, list each worktree's branch, test result and diffstat for the user.

## Rules

- Never push, and never add or change git remotes. Work only in the workers' own worktrees and branches, never
  on the default branch. That isolation is why no step needs approval: every branch can be kept or deleted.
- Never read `.env` files, keys or credential stores, and never put a secret into a task.
- When a plan reports a usage limit, stop starting workers on it until it resets. No retry loops, no switching
  accounts.
