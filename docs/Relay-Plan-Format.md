---
tags: [night-shift]
area: night-shift
updated: 2026-10
---

The planning doc the night shift reads and writes back to. Source: `relay/plan.py`. Example: `examples/PLAN.md`.

## Format

Plain markdown. The first `# ` line before any project is the plan title (used in `MORNING.md`); otherwise the filename is. Each `## ` heading is a project. `key: value` lines under it configure the project. `- [ ]` items are tasks, done top to bottom.

```markdown
## todo-cli
repo: https://github.com/you/todo-cli
test: python3 -m pytest -q
budget: 1.50
priority: 1
notes: Python 3.11, click, no other deps

- [ ] add `add`, `list`, `done` commands
- [x] scaffold the package (relay a1b2c3d)
- [!] publish to PyPI (blocked: needs a token)
```

## Project keys

| Key | Meaning |
|---|---|
| `repo:` | Git URL to clone (authenticated with `GITHUB_TOKEN` for GitHub HTTPS URLs) |
| `dir:` | Existing local directory in a git repo; the night works in a worktree of it, not in your checkout ([Relay-Night-Shift](Relay-Night-Shift.md)). Relative paths resolve under the working directory |
| `test:` | Shell command that must pass for a task to count as done |
| `budget:` | Max USD this project may spend per shift (`$` prefix allowed) |
| `priority:` | Integer; lower runs first (default 100) |
| `notes:` | Context passed to the agent with every task |
| `branch:` | Branch the night branch is cut from (default: the repo's HEAD); never checked out |

With neither `repo:` nor `dir:`, a fresh local repo is created.

## Write-back

Relay rewrites one task line at a time and re-reads the file first, so edits you make mid-shift survive. If the task line no longer matches what Relay parsed, it leaves the line alone.

- Done: `- [x] <task> (relay <short-sha>)`
- Blocked: `- [!] <task> (blocked: <reason>)`

Only `[ ]` tasks are queued; `[x]` and `[!]` are skipped on later runs.
