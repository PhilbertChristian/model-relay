# BurnerAgent

**Use it or lose it.** BurnerAgent spends leftover weekly AI subscription quota on your own repos before the reset, and leaves reviewable local branches behind.

## Safety

Agents work in isolated git worktrees on `burner/<task>` branches. BurnerAgent never pushes, and it never reads `.env` files or keys into prompts. When a lane hits a usage limit, that lane stops: no retry, no account switching.

## Sponsors

Two sponsors are wired in:

- **Agent37** — cloud lane
- **Monid** — research enrichment

**Orca** is a local lane, plus the skill at `skills/burner/SKILL.md`.

## Demo (no keys)

Requires Node 22+.

```bash
npm install
npm run demo
```

Open the localhost URL the command prints.

## Real run

```bash
cp .env.example .env
```

Set `AGENT37_API_KEY` and `MONID_API_KEY` in `.env`.

## Tests

```bash
npm test
```
