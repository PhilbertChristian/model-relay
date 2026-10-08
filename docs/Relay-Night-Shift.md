---
tags: [night-shift]
area: night-shift
updated: 2026-10
---

The night shift works through your planning doc during your downtime, spending subscription capacity that would otherwise expire. Source: `relay/burn.py`. Inputs: a planning doc ([Relay-Plan-Format](Relay-Plan-Format.md)) and `burner.json` ([Relay-Capacity-Planner](Relay-Capacity-Planner.md)). To run it on an always-on Agent37 instance, see [Relay-Agent37-Deployment](Relay-Agent37-Deployment.md#night-shift).

## Commands

```bash
python3 -m relay burn capacity -b burner.json            # what expires, when you're idle, burn order
python3 -m relay burn plan PLAN.md -b burner.json        # tonight's queue
python3 -m relay burn run PLAN.md -b burner.json --wait  # sleep until downtime, then work the queue
```

`burn run` flags (`relay/__main__.py`):

| Flag | Effect |
|---|---|
| `--now` | start even outside a downtime window |
| `--wait` | sleep until the next downtime window, then start |
| `--hours N` | stop after N hours |
| `--max-tasks N` | cap the queue |
| `--no-pr` | leave the branch local |

Without `--now` or `--wait`, outside downtime it prints the next window and exits.

## What a shift does

1. Assesses capacity and orders subscriptions by burn urgency. The router prefers models from the most urgent subscription, and each subscription's tonight allowance caps its spend: once it is reached, that provider is disabled for the shift.
2. Builds the queue: todo tasks ordered by project `priority`, then file order.
3. For each task, runs one Relay agent ([Relay-Unstick-Ladder](Relay-Unstick-Ladder.md)) in the project's worktree (below) with an unattended prompt that tells it to reply `BLOCKED: <what you need>` rather than guess.
4. If the agent finishes, runs the project's `test:` command (600 s timeout). Pass: commit, mark the task `[x]`. Fail, blocked, refused, out of steps or out of capacity: commit what exists as `WIP (blocked): …`, mark the task `[!] blocked: <why>`.
5. Per project with at least one commit: push the night branch, and only it, from its worktree and open a **draft** PR against the repo's default branch when the remote is GitHub and `GITHUB_TOKEN` is set; otherwise the branch stays local. The token reaches that one `git push` as an HTTP header in its environment, never `.git/config`.
6. Writes `MORNING.md` in the working directory.

Repo per project: `dir:` if given, else a clone of `repo:` into `<root>/<slug>`, else a fresh local git repo at `<root>/<slug>`. The work happens in a git worktree `<data_dir>/worktrees/<slug>/night-<YYYYMMDD>` on a fresh branch `relay/night-<YYYYMMDD>` (`-2`, `-3` … if taken) cut from the repo's HEAD, or from `branch:`; `data_dir` comes from `burner.json` (default `~/.relay-burn`). Relay never checks out, switches, stashes, resets, commits or cleans in your checkout, so its uncommitted changes and untracked files such as `.env` stay out of the night's work (`relay/burn.py`, `relay/worktree.py`); the agent is told not to commit, push, stash or change branches. Worktrees are removed after the shift unless `keep_worktrees` is set; the branch keeps the work.

## Stop conditions

The shift stops at the first of:

- queue empty
- downtime window ended (or `--hours` elapsed; with `--now` outside a window, 8 h)
- every provider disabled or retired ("all tonight's capacity burned")

A project whose `budget:` is reached skips its remaining tasks. So does a project whose worktree can't be made, for example a `dir:` outside any git repo, which is never `git init`ed: its first task is listed under **Needs you** and all its tasks stay `[ ]`.

## MORNING.md

Sections: counts of done / need you / still queued and the stop reason; **Done** (task, commit, cost); **Needs you** (blocked reasons); **Review** (PR links or local branch paths); **Capacity burned** per subscription against tonight's allowance and what would have expired; **Models** (calls and cost each).

## Offline demo

```bash
python3 -m relay -C /tmp/night burn run examples/PLAN.md -b examples/burner-demo.json --now
```

Uses scripted mock providers; no keys needed.
