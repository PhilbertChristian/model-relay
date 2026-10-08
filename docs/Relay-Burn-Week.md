---
tags: [burn-week, swarm]
area: burn-week
updated: 2026-10
---

**Use it or lose it.** `relay burn week` turns subscription capacity that would expire unused at this week's reset into reviewable work on your own repos. It finds your projects, queues their TODOs and plans, measures what each subscription has left, and runs a paced swarm of agents (Claude Code, Codex, Relay's API engine, Agent37) so usage lands near the target just before the reset. Each task becomes one commit on its own local branch, and `BURN.md` sums up the run. Sources: `relay/__main__.py` (CLI), `relay/swarm.py`, `relay/demo_week.py`. The older plan-driven overnight flow is [Relay-Night-Shift](Relay-Night-Shift.md).

## Commands

```bash
python3 -m relay burn demo                       # the whole swarm on a throwaway sandbox: mock lanes, no keys
python3 -m relay burn usage                      # what each subscription has left before its reset
python3 -m relay burn discover -o PLAN.md        # repos and TODOs found, written as a plan you can edit
python3 -m relay burn week PLAN.md --dry-run     # the pace per lane and the task queue; starts nothing
python3 -m relay burn week PLAN.md --web 3737    # run it: terminal dashboard here, web dashboard on :3737
python3 -m relay burn ideas --days 14            # task ideas from your local Claude Code / Codex chats
python3 -m relay dash                            # follow a running swarm from another terminal
python3 -m relay web --port 3737                 # the web dashboard on its own
```

| Flag | Used by | Effect |
|---|---|---|
| `-b FILE` | week, discover, ideas, usage | config file; default `./burner-week.json`, else `examples/burner-week.json` with a note |
| `--roots DIR ...` | week, discover | search these directories for git repos instead of the config `roots` |
| `--agents N` | week, demo | most agents running at once (week: config `max_agents`; demo: 8) |
| `--hours H` | week | launch no new tasks after H hours (default: the weekly reset) |
| `--ideas` | week | opt in to mining local chat history for this run |
| `--lanes a,b` | week | only these lanes, by name or kind; naming a disabled lane (e.g. `orca`) enables it |
| `--dry-run` | week | print the pace and queue plan and exit |
| `--no-dash` | week, demo | one log line per agent start and end instead of the full-screen dashboard |
| `--web PORT` | week, demo | also serve the web dashboard on `127.0.0.1:PORT`; it keeps serving after the run until Ctrl-C |
| `-o PLAN.md` | discover | write the plan here; an existing file is never overwritten |
| `--days N` | ideas | how far back to look (default: `ideas.since_days`, 21) |
| `--speed X` | demo | mock lane speed (2 = twice as fast) |
| `--record FILE` | demo | save the run's events for the dashboard replay (see below) |
| `--events PATH` | dash, web | ledger to follow (default `<cwd>/.relay/events.jsonl`) |
| `--port N`, `--supabase` | web | port (default 3737); follow the rows in Supabase instead of the ledger |

The positional `PLAN.md` is optional for `burn week`. A flag that does not belong to the chosen action is an error, and `burn capacity|plan|run` accept exactly the flags they always did. When a burn-week module is missing from an install, the command says which one is "not built yet" instead of failing with a traceback.

## Config

`examples/burner-week.json` is the documented example: the real lanes, chat-history ideas off, the Orca lane off. Copy it to `./burner-week.json` and edit the roots, subscriptions and lanes. Its sizes (400M Claude tokens a week, $20 budgets) are example values, not plan limits.

| Key | Meaning |
|---|---|
| `week` | `reset` (`"mon 09:00"`), `timezone`, `target_pct`: the share of each limit to have used by the reset (0.97) |
| `roots`, `max_depth`, `max_projects` | where to look for repos and how many to keep |
| `per_project` | tasks queued per project |
| `max_agents` | most agents running at once, across all lanes |
| `data_dir` | worktrees and run output (`~/.relay-burn`) |
| `ideas` | `enabled` (off), `since_days` |
| `monid` | `enabled`: a short research brief per task, only when `MONID_API_KEY` is set (`relay/monid.py`) |
| `subscriptions` | kinds `claude_code` and `codex` (weekly tokens), `agent37_budget` and `api_budget` (monthly dollars), `monid_credits`; `relay/usage.py` reads them |
| `lanes` | `kind` (`claude`, `codex`, `agent37`, `relay`, `orca`, `mock`), `name`, `subscription`, `max_agents`, `enabled`; `relay/lanes/` |
| `providers`, `models` | the model ladder for the `relay` lane, in the [Relay-Config](Relay-Config.md) format |

Pacing (`relay/pace.py`): each lane gets enough agents to burn what its subscription still needs per hour until the reset, capped by the lane's `max_agents`; when the lanes want more than `max_agents` together, the agents are shared out in proportion.

## Demo

`relay burn demo` (`relay/demo_week.py`) needs no keys and no network; only when `SUPABASE_URL` and `SUPABASE_KEY` are set do its events also go to Supabase, as any run's do (`relay/telemetry.py`). It builds a sandbox in a new temp directory: five small git repos (a Python API, a TypeScript web app, a Go CLI, a MkDocs site, a Rust library) with TODO and FIXME comments, a `- [ ]` plan each, a repo-local git identity and one commit. Where `npm`, `go` or `cargo` is not installed, the repo that needs it is built without the manifest that names its test command, so its tasks report tests as not run instead of failing. It then runs the same `burn_week` on them, against `demo` subscriptions and `mock` lanes (`relay/lanes/mock.py`) that pose as Claude, Codex, Agent37, Relay and Orca. The demo numbers (Claude Max 78% of 400M tokens used, Codex 60%, 30 hours to the reset) are examples. Outcomes are seeded per task, so repeated takes look much the same, and the Codex lane is weighted to hit a usage limit partway through. At speed 1 a run takes about 70 seconds (measured on a laptop: 20 tasks on 8 agents at the demo's 20 s per mock task). It ends by printing the `BURN.md` path and one line per branch.

`--record docs/sample-events.jsonl` saves the run for the web dashboard's `/sample-events.jsonl` replay. Every string goes through `contracts.redact()`, the sandbox path becomes `~/relay-burn-demo` and your home directory becomes `~`.

## Dashboards and ledger

`burn week` writes events to `<cwd>/.relay/events.jsonl` (`-C DIR` sets the cwd) and `BURN.md` to `data_dir`; the demo keeps both inside its sandbox. The terminal dashboard (`relay/dash.py`) draws only on a terminal: with `--no-dash`, or when output is piped, you get plain lines. The web dashboard (`relay/web.py`) binds 127.0.0.1 and streams the same rows over SSE. Supabase setup is in [Relay-Telemetry](Relay-Telemetry.md).

## Safety

Each rule below is enforced in the file named after it.

1. **Your checkout is read-only.** Every task runs in `<data_dir>/worktrees/<project>/<task-id>`, a git worktree on a new local branch `relay/burn/<task-id>` cut from HEAD. Nothing checks out, switches, stashes, resets, commits or cleans in your checkout, and untracked files such as `.env` are not in a worktree (`relay/worktree.py`).
2. **Nothing leaves as git.** No push and no remote changes: the burn's git wrapper refuses those subcommands, and branches stay local until you merge them (`relay/worktree.py`).
3. **No permission bypass.** Claude Code runs with `--permission-mode acceptEdits` and explicit allow and deny lists, never `--dangerously-skip-permissions` (`relay/lanes/claude.py`). Codex runs in the `workspace-write` sandbox with network off, never with `--dangerously-bypass-approvals-and-sandbox` or `--yolo` (`relay/lanes/codex.py`).
4. **Secrets stay out.** Files matching `contracts.SECRET_FILE_RE` (`.env*`, keys, credential files) are never staged (`relay/worktree.py`). Prompts (`relay/lanes/base.py`), event fields (`relay/bus.py`) and recorded demo rows (`relay/demo_week.py`) go through `contracts.redact()`.
5. **Limits are respected.** A lane that reports a usage limit ends that task as `limited` and gets no new work until its reset: no retry loops, no account switching (`relay/swarm.py`, `relay/pace.py`, `relay/lanes/claude.py`).
6. **Chat history is opt-in and local.** It is read only with `ideas.enabled`, `--ideas` or `relay burn ideas`, and the transcripts themselves never go to a lane, Monid, Supabase or a model (`relay/ideas.py`).
7. **Remote lanes are opt-in.** Agent37, OpenAI and Monid are used only when their keys are set, and get the task text plus the files the agent reads, after the secret filter (`relay/lanes/agent37.py`, `relay/lanes/relay_api.py`, `relay/monid.py`).
