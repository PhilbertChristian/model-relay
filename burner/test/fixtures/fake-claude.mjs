#!/usr/bin/env node
// Stand-in for the Claude Code CLI. Reads no secrets, no home directory, and no project files.
// Writes only inside the process cwd (the runner's worktree).
//
// Default contract other agents can rely on:
//   - write burner-note.txt containing "burner was here"
//   - print one JSON line and exit 0
//
// Test hooks are whole argv words: LIMIT, HANG, FAIL, DUMP_ARGV, MULTI, HIT, RATE, TOO_MANY.

import { writeFileSync } from "node:fs";

const args = process.argv.slice(2);
const has = (word) => args.includes(word);

if (has("DUMP_ARGV")) {
  writeFileSync("burner-argv.json", JSON.stringify({ argv: process.argv, cwd: process.cwd() }));
}

if (has("HANG")) {
  await new Promise((resolve) => setTimeout(resolve, 30_000));
  process.exit(0);
}

if (has("FAIL")) {
  console.error("fake-claude failed");
  process.exit(1);
}

if (has("LIMIT")) {
  console.log("usage limit reached");
  process.exit(0);
}

if (has("HIT")) {
  console.error("hit your limit");
  process.exit(0);
}

if (has("RATE")) {
  console.log("rate limit");
  process.exit(0);
}

if (has("TOO_MANY")) {
  console.error("status 429");
  process.exit(0);
}

writeFileSync("burner-note.txt", "burner was here");

if (has("MULTI")) {
  console.log(
    JSON.stringify({
      type: "assistant",
      message: {
        content: [
          { type: "text", text: "working" },
          { type: "tool_use", name: "Write", input: { file_path: "burner-note.txt" } },
        ],
        usage: {
          input_tokens: 10,
          output_tokens: 5,
          cache_read_input_tokens: 1,
          cache_creation_input_tokens: 2,
        },
      },
    }),
  );
}

console.log(
  JSON.stringify({
    type: "result",
    result: "added burner-note.txt",
    usage: has("MULTI")
      ? {
          input_tokens: 120,
          output_tokens: 40,
          cache_read_input_tokens: 3,
          cache_creation_input_tokens: 4,
        }
      : { input_tokens: 120, output_tokens: 40 },
    ...(has("MULTI") ? { total_cost_usd: 0.02 } : {}),
  }),
);
