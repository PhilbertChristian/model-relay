---
tags: [docs, process]
area: docs
updated: 2026-10
---

How documentation in this repo is structured, named, sourced and corrected. Adapted from Kai's brain2 vault standards (`CLAUDE.md` and `Verification-Standards.md` there) for a public code repo. This note owns the rules; [README](../README.md) is the entry point and index.

## Never duplicate

**One file owns a thing; every other file links to it.** If the same fact, table, command list or explanation would appear in two docs, pick the owner and replace the copy with a link. The README summarises in a sentence and links; it does not restate a doc's content.

Owners:

| Subject | Owner |
|---|---|
| Night shift flow, `MORNING.md`, stop conditions | [Relay-Night-Shift](Relay-Night-Shift.md) |
| `burner.json`: idle hours, subscriptions, burn order | [Relay-Capacity-Planner](Relay-Capacity-Planner.md) |
| Planning doc format | [Relay-Plan-Format](Relay-Plan-Format.md) |
| Blocker detection, unstick ladder, limits, refusals | [Relay-Unstick-Ladder](Relay-Unstick-Ladder.md) |
| Supervising Agent37-hosted agents | [Relay-Supervisor-Mode](Relay-Supervisor-Mode.md) |
| `relay.json`, providers, model fields | [Relay-Config](Relay-Config.md) |
| `deploy/agent37.py` commands | [Relay-Agent37-Deployment](Relay-Agent37-Deployment.md) |
| Event log, Supabase, `relay stats` | [Relay-Telemetry](Relay-Telemetry.md) |

Exception: `docs/index.html` is the marketing page and necessarily restates the pitch. When behaviour changes, update the owning doc first, then the page.

## Structure

- **One file per concept.** Do not fold distinct things into one doc. Link between them from the README index.
- **500-line limit per file.** When a doc approaches it, split into sub-docs and link them from the parent; do not compress.
- Docs live in `docs/`. Example inputs (`examples/`) are fixtures, not docs: `examples/PLAN.md` keeps its H1 because `relay/plan.py` reads it as the plan title.

## Naming and format

- **Title-Kebab-Case, globally unique, self-describing**, identifiable without its folder: `Relay-Unstick-Ladder.md`, not `Ladder.md` or `overview.md`. Every doc is prefixed `Relay-`.
- **No leading H1.** The filename is the title; start with prose or `##`. `README.md` is exempt because GitHub renders its H1 as the repo title.
- **No `title` in frontmatter.** Each doc in `docs/` carries:

  ```yaml
  ---
  tags: [engine]
  area: engine
  updated: 2026-10
  ---
  ```

  Bump `updated` when content changes.
- **Links are relative markdown links**, e.g. `[Relay-Config](Relay-Config.md)`. This deviates from brain2's `[[wikilinks]]` on purpose: GitHub does not render wikilinks, and Obsidian resolves relative links too. Code files use backtick paths (`relay/router.py`).

## Sourcing

Applies the brain2 verification rules to a code repo:

- **Behaviour claims cite the code that implements them.** Each doc lists its source files; a specific default or threshold names the file it lives in (e.g. "3 identical tool calls, `relay/blockers.py`"). If the code changes, the doc changes in the same commit.
- **Third-party claims (Agent37, OpenAI, Claude/ChatGPT plans) carry a source link or `[UNVERIFIED]` inline.** Code comments are not a source for how someone else's platform behaves.
- **No invented guarantees.** "Never", "always", "fires even while asleep" are either backed by code we control or by a cited source, or marked `[UNVERIFIED]` / `[INFERRED — from …]`.
- **Example numbers are labelled as examples.** The rolling-window figures in `examples/burner-demo.json` are demo values, not plan limits.

## Corrections

When a doc is found wrong:

1. Fix the owning doc in place.
2. `git grep -n "<wrong phrase>"` across the repo, including `docs/index.html` and docstrings. Zero stray hits is the exit criterion.
3. Commit with `docs: correction:` in the subject so `git log --grep=correction:` lists them.

## Checks

Run before committing; both also run in the test suite (`tests/test_docs.py`):

```bash
python3 scripts/check_docs_md.py      # naming, H1, frontmatter, line limit, broken relative links
python3 scripts/check_duplication.py  # prose copied between docs
```
