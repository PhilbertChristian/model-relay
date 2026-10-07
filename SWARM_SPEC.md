Build spec for the burn-week swarm. This file is the source of truth for every agent.

## Pitch
**Use it or lose it.** `relay burn week` turns the AI-subscription capacity you would otherwise waste before
this week's reset into reviewable work on your own projects. It finds your repos, harvests TODOs and unfinished
ideas (optionally from your local Claude Code / Codex chat history), measures how much of each subscription is
left, paces a swarm of parallel agents (Claude Code, Codex, Relay's API-model engine, Agent37 cloud, Orca) so you
land near 100% right before the reset, and leaves one tested commit per task on a local branch. You get BURN.md.
The hackathon submission is a VIDEO: `relay burn demo` + the dashboards must look stunning with no API keys.

Sponsors: Agent37 (cloud lane, LLM router, platform cron), Monid (task research), OpenAI (relay lane),
Supabase (live telemetry). Integrations: Orca (lane + skill), Claude Code plugin.

## House rules
- WT = /Users/kai/ModelRElay/.worktrees/burn-week (git worktree, branch burn-week). Work ONLY in WT; `cd WT` in
  every Bash command. Never edit the main checkout /Users/kai/ModelRElay outside .worktrees/burn-week.
- Python 3.10+, standard library only (zero deps), like the rest of relay/. Match existing style:
  `from __future__ import annotations`, dataclasses, short docstrings, compact code, `relay.ui.c()` for color.
- Only create/edit files you own. Shared contracts — relay/contracts.py, relay/bus.py, relay/lanes/base.py,
  relay/lanes/__init__.py, SWARM_SPEC.md — belong to the leader: import them, never edit them. Need a contract
  change? Put it in your final report.
- No git commit / checkout / switch / stash / push in WT. The leader commits.
- Tests: unittest, tests/test_<module>.py. Offline: no network, no real repos, no ~/.claude or ~/.codex, no real
  `claude` / `codex` / `orca` binaries (use fixtures in tests/fixtures/ + tempfile dirs + unittest.mock).
  Run: `cd WT && python3 -m unittest tests.test_<module> -v`. Keep each file under ~10 s.
- Other modules are being written in parallel. Import sibling modules lazily (inside functions) and code against
  the signatures below.

## Safety invariants (non-negotiable — reviewers check these)
1. Agents only work inside a git worktree `<data_dir>/worktrees/<project-slug>/<task-id>` on a fresh branch
   `relay/burn/<task-id>` made from the repo's HEAD. Never modify the user's checkout: no checkout, switch,
   stash, reset, commit or clean there. (Untracked files such as .env are not in a worktree — keep it so.)
2. Never `git push`, never add or modify remotes. Branches stay local for the user to review and merge.
3. Claude lane: never `--dangerously-skip-permissions` (Codex: never `--dangerously-bypass-approvals-and-sandbox`
   / `--yolo`). Use acceptEdits / workspace-write with explicit allow + deny lists (git push, remotes, rm -rf,
   sudo, curl, reading .env ...).
4. Never read credential stores (keychain, browser profiles/cookies, ~/.ssh, app token files). Never put files
   matching contracts.SECRET_FILE_RE into a prompt, log, report or request. Pass every prompt, log line, report
   and remote payload through contracts.redact().
5. Respect usage limits. A lane that reports a usage/rate limit is marked limited until its reset; stop launching
   on it. No retry loops around a limit, no account switching.
6. Chat history is mined locally and is opt-in (`ideas.enabled` / `--ideas`). Transcripts never go to a remote
   lane, Monid, Supabase or an LLM; only short redacted idea strings derived from them may appear in a task.
7. Remote lanes (Agent37, Monid, OpenAI) are opt-in via env keys and receive only the task text plus the files
   the task needs, after the secret filter.

## Data flow
```
usage (left per subscription) ─┐
discover (repos + TODOs) ──────┼─> swarm.build_queue ─> pace (agents per lane) ─> swarm.run_swarm (threads)
ideas (local chat, opt-in) ────┤                                                   worktree.create -> lane.run
monid (research brief) ────────┘                                                   -> worktree.finalize (test, commit)
bus.emit(...) -> .relay/events.jsonl (+ Supabase) -> dash (terminal) / web (SSE dashboard)        -> BURN.md
```

## Contracts (exact; build to these)
Shared types live in relay/contracts.py (ProjectInfo, Idea, SwarmTask, LaneResult, Usage, PacePlan, Emit,
redact, is_secret_file, slugify, to_dict). relay/bus.py has Bus. relay/lanes/base.py has Lane + task_prompt.

### relay/discover.py — owner: discover
    def inspect_project(path: str) -> ProjectInfo
    def discover_projects(roots: list[str], max_depth: int = 3, limit: int = 20, include: list[str] | None = None,
                          exclude: list[str] | None = None, skip: list[str] | None = None) -> list[ProjectInfo]  # score desc
    def harvest_todos(path: str, limit: int = 12) -> list[str]
    def generic_tasks(p: ProjectInfo) -> list[str]
    def to_plan_md(projects, ideas: dict[str, list[Idea]] | None = None, per_project: int = 3, title: str = "Burn week") -> str
    def report(projects: list[ProjectInfo]) -> str

### relay/ideas.py — owner: ideas
    def mine_claude_code(root: str = "~/.claude/projects", since_days: float = 21, limit: int = 200) -> list[Idea]
    def mine_codex(root: str = "~/.codex/sessions", since_days: float = 21, limit: int = 200) -> list[Idea]
    def mine_chatgpt_export(path: str, since_days: float = 60, limit: int = 200) -> list[Idea]
    def mine(cfg: dict | None = None) -> dict[str, list[Idea]]      # key: project_dir ("" when unknown); {} unless enabled
    def report(ideas_by_dir: dict[str, list[Idea]]) -> str

### relay/usage.py — owner: usage
    def week_bounds(reset: str = "mon 09:00", tz: str | None = None, now: float | None = None) -> tuple[float, float]
    def claude_code_tokens(since: float, root: str = "~/.claude/projects") -> dict   # input/output/cache_read/cache_creation/total/by_model/sessions
    def codex_tokens(since: float, root: str = "~/.codex/sessions") -> dict
    def weekly_usage(cfg: dict, ledger: str = ".relay/events.jsonl", now: float | None = None) -> list[Usage]
    def report(usages: list[Usage]) -> str

### relay/pace.py — owner: pace
    def plan_pace(usages: list[Usage], lanes_cfg: list[dict], max_agents: int = 12, target_pct: float = 0.97,
                  now: float | None = None) -> list[PacePlan]
    def adjust(current: int, observed_rate: float, target_rate: float, lo: int = 1, hi: int = 12) -> int
    def schedule_note(plans: list[PacePlan]) -> str
    def report(plans: list[PacePlan]) -> str

### relay/worktree.py — owner: worktree
    def create(repo: str, data_dir: str, task_id: str, slug: str) -> tuple[str, str]          # (path, branch)
    def finalize(path: str, message: str, test_cmd: str | None = None, timeout: float = 600) -> dict
        # {"tests_ok": bool|None, "tests_tail": str, "commit": str|None, "diffstat": str, "files": int, "insertions": int, "deletions": int}
    def remove(repo: str, path: str) -> None        # removes the worktree dir, keeps the branch
    def branches(repo: str) -> list[dict]           # [{"branch","sha","subject"}] for relay/burn/*

### relay/swarm.py — owner: swarm
    def build_queue(projects, ideas=None, plan_path: str | None = None, per_project: int = 3, limit: int | None = None) -> list[SwarmTask]
    def run_swarm(cfg: dict, queue: list[SwarmTask], lanes: list[Lane], bus: Bus, data_dir: str, max_agents: int = 8,
                  deadline: float | None = None, usages: list[Usage] | None = None, enrich=None) -> dict
    def burn_week(cfg: dict, *, roots=None, plan_path=None, max_agents=None, hours=None, ideas=None, dry_run=False,
                  data_dir=None, bus=None, lanes=None) -> dict
    def write_report(result: dict, path: str) -> str          # BURN.md

### relay/lanes/<kind>.py — one owner each; subclass relay.lanes.base.Lane; implement available() and run()
    mock.py MockLane · claude.py ClaudeLane · codex.py CodexLane · relay_api.py RelayLane · agent37.py Agent37Lane · orca.py OrcaLane

### relay/monid.py — owner: monid
    def available() -> tuple[bool, str]
    def research(task: SwarmTask, timeout: float = 30) -> str          # short brief, "" on any failure
    def credits() -> Usage | None
    def enricher(cfg: dict)                                            # Callable[[SwarmTask], str] | None
    def tools() -> list[str]

### relay/dash.py — owner: tui
    class DashState: def apply(self, row: dict) -> None
    def render(state: DashState, width: int = 120, height: int = 34) -> str
    def attach(bus: Bus, refresh: float = 0.2, force: bool = False)    # returns stop()
    def tail(events_path: str, refresh: float = 0.2, once: bool = False) -> None

### relay/web.py — owner: web
    def serve(port: int = 3737, events_path: str = ".relay/events.jsonl", bus: Bus | None = None,
              open_browser: bool = False, host: str = "127.0.0.1")   # ThreadingHTTPServer running in a daemon thread
    routes: / -> docs/dashboard.html · /events -> SSE (replay, then live) · /api/state -> JSON · /sample-events.jsonl

### relay/demo_week.py — owner: demo
    def make_sandbox(root: str) -> dict
    def demo_config(sandbox: dict, speed: float = 1.0) -> dict
    def run_demo(agents: int = 8, speed: float = 1.0, dash: bool = True, web_port: int | None = None,
                 record: str | None = None, root: str | None = None) -> dict

### relay/supa.py — owner: supabase
    def available() -> bool
    def recent_events(since_ts: float = 0, limit: int = 500, session: str | None = None) -> list[dict]
    def runs(limit: int = 20) -> list[dict]

## Events — bus.emit(event, **data); rows land in .relay/events.jsonl as {"ts","session","host","event",...data}
    swarm_start    {run, max_agents, lanes: [{name, kind, subscription, as}], queue: int, usages: [to_dict(Usage)], resets_at}
    pace           {lane, subscription, unit, used, limit, pct, remaining, agents, target_rate, hours_left}
    task_queued    {task_id, project, text, source}
    agent_start    {agent: int (slot 1..N), task_id, project, text, lane, branch, worktree}
    agent_progress {agent, task_id, lane, tokens: int (cumulative for this task), usd, note}
    agent_end      {agent, task_id, project, lane, status, summary, tokens, usd, commit, diffstat, tests_ok, seconds}
    lane_limited   {lane, until, reason}
    swarm_end      {run, done, blocked, failed, limited, tokens, usd, by_lane: {lane: {tokens, usd, tasks}}, report, seconds, reason}
Lanes only call the `emit` they are given: emit("agent_progress", tokens=..., usd=..., note=...). The swarm binds
agent / task_id / lane. Status values: done | blocked | failed | limited.

## Config (burner-week.json; examples/burner-week.json is the documented example)
    {"week": {"reset": "mon 09:00", "timezone": "America/Los_Angeles", "target_pct": 0.97},
     "roots": ["~/Documents/Code", "~/code", "~/Projects", "~/Developer"], "max_depth": 3, "max_projects": 12,
     "per_project": 3, "max_agents": 12, "data_dir": "~/.relay-burn",
     "ideas": {"enabled": false, "since_days": 21}, "monid": {"enabled": true},
     "subscriptions": [
       {"name": "claude-max", "kind": "claude_code", "lane": "claude", "weekly_tokens": 400000000},
       {"name": "codex", "kind": "codex", "lane": "codex", "weekly_tokens": 100000000},
       {"name": "agent37", "kind": "agent37_budget", "lane": "agent37", "monthly_usd": 20, "instance": "$AGENT37_INSTANCE_ID"},
       {"name": "openai", "kind": "api_budget", "lane": "relay", "providers": ["openai"], "monthly_usd": 20},
       {"name": "monid", "kind": "monid_credits", "lane": "monid", "credits": 1000}],
     "lanes": [
       {"kind": "claude", "name": "claude", "subscription": "claude-max", "max_agents": 8, "model": "sonnet"},
       {"kind": "codex", "name": "codex", "subscription": "codex", "max_agents": 3},
       {"kind": "agent37", "name": "agent37", "subscription": "agent37", "max_agents": 2},
       {"kind": "relay", "name": "relay", "subscription": "openai", "max_agents": 2},
       {"kind": "orca", "name": "orca", "max_agents": 4, "enabled": false}],
     "providers": {...}, "models": [...]}          # relay lane model ladder, same schema as relay.json
Demo subscriptions use kind "demo" with {"used", "limit", "unit", "resets_in_hours"}. Demo lanes use kind "mock"
with "as": "claude" | "codex" | "agent37" | "relay" | "orca" (display flavor) plus mock params.

## CLI (relay/__main__.py — owner: cli)
    relay burn week     [-b burner-week.json] [--roots DIR ...] [--agents N] [--hours H] [--ideas] [--lanes a,b] [--dry-run] [--no-dash] [--web PORT]
    relay burn discover [--roots DIR ...] [-o PLAN.md]
    relay burn ideas    [--days N]
    relay burn usage    [-b burner-week.json]
    relay burn demo     [--agents 8] [--speed 1.0] [--web PORT] [--record docs/sample-events.jsonl] [--no-dash]
    relay dash          [--events PATH]
    relay web           [--port 3737] [--events PATH] [--supabase]
    (existing `burn capacity|plan|run`, run, supervise, stats ... stay unchanged)
