# 60–90 second shot list

Record this in one take, about 75 seconds. No API keys. Do not name tools that are not in this repo.

## 0:00–0:12 — Terminal

In the BurnerAgent repo, run:

```bash
npm run demo
```

Hold on the terminal until the localhost URL prints. Leave that URL readable on screen.

## 0:12–0:32 — Dashboard meter

Open the printed URL. Frame the usage meter and let it climb. Do not cut away while the number is still moving.

## 0:32–0:52 — Mock agents

Stay on the dashboard. Show mock agents flip from running to **succeeded**. Keep at least two status changes in frame.

## 0:52–1:08 — Worktree diff

Cut to the demo worktree. Show `git diff` (or the diff the dashboard links) for a `burner/<task>` branch. The change should be visible as a local diff, not a remote pull request.

## 1:08–1:20 — End card

Hold a card for the rest of the clip:

- **Agent37** — cloud lane
- **Monid** — research enrichment
- Safety: isolated `burner/<task>` worktrees, never pushes, never reads `.env` or keys into prompts, stops when a lane hits a usage limit (no retry, no account switching)
