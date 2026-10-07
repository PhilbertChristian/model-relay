#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

// bin/burner.mjs → package root (two directories up).
const root = dirname(dirname(fileURLToPath(import.meta.url)));

const child = spawnSync("npx", ["tsx", "src/cli.ts", ...process.argv.slice(2)], {
  stdio: "inherit",
  cwd: root,
});

process.exit(child.status ?? 1);
