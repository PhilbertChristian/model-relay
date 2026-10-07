# BurnerAgent — build spec (source of truth for every agent)

## Pitch
**Use it or lose it.** BurnerAgent turns your leftover weekly AI-subscription quota into real work on your projects. It finds your repos, mines your past Claude chats for ideas you never got to, plans concrete tasks, and runs a fleet of Claude Code agents (plus Agent37 cloud agents) in isolated git worktrees. The fleet is paced so you land near 100% of quota just before the reset, with reviewable branches to show for it.

Hackathon constraints: the project must use ≥2 sponsor tools. We use **Agent37** (cloud agent lane, burns Agent37 credits) and **Monid** (tool router for task research, and as an MCP toolbox for agents). The submission is a **video**, so the dashboard must look stunning and `burner demo` must give a gorgeous, lively run with no API keys.

## Stack & house rules
- Node 26, TypeScript ESM, executed with `tsx` (no build step). Tests: vitest, `test/<module>.test.ts`.
- Imports use the `.js` suffix: `import { log } from "./util.js"`.
- **No runtime deps.** Node built-ins only (node:http, node:child_process, node:fs/promises, node:readline, node:crypto, …). Dev deps: typescript, tsx, vitest, @types/node. Don't add packages; if you truly need one, say so in your report.
- **Only edit files you own** (listed in your assignment). Shared contracts — `src/types.ts`, `src/config.ts`, `src/events.ts`, `src/util.ts` — belong to the integrator: import them, don't edit them. Need a contract change? Put it in your final report.
- `npx tsc --noEmit` checks the whole project while other agents are mid-flight. Fix errors in YOUR files only.
- Run only your own tests: `npx vitest run test/<yours>.test.ts`.
- Tests must not hit the network, the user's real repos, or `~/.claude`. Use temp dirs + injected paths. Tests must never invoke the real `claude` binary; use `test/fixtures/fake-claude.mjs` (owned by the claude-runner agent; others may read it).

## Safety invariants (non-negotiable — reviewers will check these)
1. Agents run **only** inside a git worktree under `<dataDir>/worktrees/<project-id>/<task-id>` on a fresh branch `burner/<task-id>` created from the repo's current HEAD. Never modify the user's main checkout, never switch its branch, never stash, never reset.
2. **Never `git push`** or touch remotes. Branches stay local for the user to review/merge.
3. Never pass `--dangerously-skip-permissions`. Claude runs with `--permission-mode acceptEdits` plus `allowedTools`/`disallowedTools` from config.
4. Never read credential stores (macOS keychain, browser cookie/profile dirs, `~/.ssh` keys, app auth/token files). Never read, prompt with, or upload files matching `SECRET_FILE_RE` (src/util.ts). Redact API-key/token-looking strings from anything that goes into a prompt, a log line, a store file, or a remote request.
5. **Respect usage limits.** When a lane reports a usage/rate limit, mark it `limited`, stop launching on it, and wait for the reset. No retry loops to get around it, no account switching.
6. Remote lanes (Agent37, Monid) are opt-in via keys in `.env`. Never send chat transcripts to a remote lane. Only send files the task actually needs, after the secret filter.

## Architecture
```
 discover ──► planner ◄── ideas (chat-history miner)      ◄── monid (enrichment)
                 │
                 ▼
            scheduler ── adaptive concurrency ◄── budget (weekly window, pacing)
     ┌──────────┼───────────┬──────────┐
  claude      orca       agent37     mock        (lanes = Runner implementations)
 (local CLI) (Orca app)  (cloud)    (demo)
     └── worktree: create → run → commit on burner/<id> → diffstat
                 │
               bus (src/events.ts) ──► store (JSON in dataDir) ──► server (HTTP + SSE) ──► web/ dashboard
```

## Module contracts (exact exports; build to these)

### src/discover.ts  — owner: discover
```ts
export async function inspectProject(path: string): Promise<ProjectInfo>;
export async function discoverProjects(cfg: BurnerConfig): Promise<ProjectInfo[]>; // sorted by score desc
```
Walk `cfg.roots` (skip missing roots) to `cfg.maxDepth` for dirs containing `.git`. Skip node_modules, .git internals, Library, hidden dirs, and `cfg.dataDir`. Apply include/exclude substring filters. Score = recency of last commit + tod