import { chmod, copyFile, mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, expect, test } from "vitest";
import { createClaudeRunner } from "../src/runners/claude.js";
import type { BurnerConfig, ProjectInfo, RunContext, RunnerEvent, Task } from "../src/types.js";

const fakeClaude = fileURLToPath(new URL("./fixtures/fake-claude.mjs", import.meta.url));
const prompt = "Add a short note in this worktree.";

const dirs: string[] = [];

afterEach(async () => {
  await Promise.all(dirs.splice(0).map((dir) => rm(dir, { recursive: true, force: true })));
});

async function tempDir(): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(), "burner-claude-"));
  dirs.push(dir);
  return dir;
}

function makeConfig(claude: Partial<BurnerConfig["claude"]> = {}, runTimeoutMin = 25): BurnerConfig {
  return {
    roots: [],
    include: [],
    exclude: [],
    maxDepth: 1,
    concurrency: { min: 1, max: 1 },
    weeklyTokenTarget: 1000,
    resetDay: 0,
    resetHour: 0,
    stopAtPct: 99,
    runTimeoutMin,
    lanes: { claude: true, agent37: false, orca: false, mock: false },
    claude: {
      bin: process.execPath,
      maxTurns: 30,
      permissionMode: "acceptEdits",
      allowedTools: ["Read", "Edit", "Write"],
      disallowedTools: ["Bash(git push:*)"],
      extraArgs: [fakeClaude],
      ...claude,
    },
    agent37: { baseUrl: "http://127.0.0.1:9", template: "agent37-claude-code", maxInstances: 1 },
    monid: { baseUrl: "http://127.0.0.1:9", enabled: false },
    orca: { enabled: false },
    dataDir: join(tmpdir(), "burner-claude-data"),
    port: 0,
    demo: false,
  };
}

function makeTask(text = prompt): Task {
  return {
    id: "task-1",
    projectId: "proj-1",
    kind: "docs",
    title: "Leave a burner note",
    prompt: text,
    priority: 1,
    estTokens: 200,
    lane: "claude",
    context: [],
    createdAt: "2026-10-07T00:00:00.000Z",
  };
}

function makeContext(cwd: string, checkout: string, signal: AbortSignal, claude?: Partial<BurnerConfig["claude"]>, runTimeoutMin = 25): RunContext {
  const project: ProjectInfo = {
    id: "proj-1",
    name: "proj",
    path: checkout,
    languages: ["typescript"],
    lastCommitAt: null,
    dirty: false,
    hasTests: false,
    todoCount: 0,
    packageManager: null,
    testCommand: null,
    readmeExcerpt: "",
    score: 1,
  };
  return {
    runId: "run-1",
    project,
    cwd,
    branch: "burner/task-1",
    signal,
    config: makeConfig(claude, runTimeoutMin),
  };
}

function flagValues(argv: string[], flag: string): string[] {
  const values: string[] = [];
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === flag) values.push(argv[i + 1] ?? "");
  }
  return values;
}

test("available() resolves the claude bin on PATH and misses a bare name that is not there", async () => {
  const dir = await tempDir();
  const prev = process.env.PATH;
  process.env.PATH = dir;
  try {
    expect(await createClaudeRunner().available()).toBe(false);
    const stub = join(dir, "claude");
    await writeFile(stub, "#!/bin/sh\nexit 0\n");
    await chmod(stub, 0o755);
    expect(await createClaudeRunner().available()).toBe(true);
  } finally {
    process.env.PATH = prev;
  }
});

test("spawns in the worktree and records a successful run", async () => {
  const checkout = await tempDir();
  const worktree = await tempDir();
  const events: RunnerEvent[] = [];
  const runner = createClaudeRunner();
  const task = makeTask();

  expect(runner.lane).toBe("claude");
  const record = await runner.run(task, makeContext(worktree, checkout, new AbortController().signal, { extraArgs: [fakeClaude, "DUMP_ARGV"] }), (event) => {
    events.push(event);
  });

  expect(record.status).toBe("succeeded");
  expect(record.lane).toBe("claude");
  expect(record.summary).toBe("added burner-note.txt");
  expect(record.usage).toEqual({ input: 120, output: 40, cacheRead: 0, cacheWrite: 0 });
  expect(record.worktreePath).toBe(worktree);
  expect(events.some((event) => event.type === "usage" && event.usage.input === 120 && event.usage.output === 40)).toBe(true);
  expect(events.filter((event) => event.type === "done")).toEqual([
    { type: "done", runId: "run-1", status: "succeeded", summary: "added burner-note.txt", error: undefined },
  ]);

  expect(await readFile(join(worktree, "burner-note.txt"), "utf8")).toBe("burner was here");
  await expect(readFile(join(checkout, "burner-note.txt"), "utf8")).rejects.toThrow();

  const dumped = JSON.parse(await readFile(join(worktree, "burner-argv.json"), "utf8")) as { argv: string[]; cwd: string };
  expect(dumped.cwd).toBe(await realpath(worktree));
  expect(dumped.argv.slice(1)).toEqual([
    fakeClaude,
    "DUMP_ARGV",
    "-p",
    prompt,
    "--permission-mode",
    "acceptEdits",
    "--max-turns",
    "30",
  ]);
  expect(dumped.argv.some((arg) => arg.includes("dangerously-skip-permissions"))).toBe(false);
  expect(dumped.argv.some((arg) => /\bgit\s+push\b/.test(arg))).toBe(false);
});

test("strips --dangerously-skip-permissions from extra args", async () => {
  const checkout = await tempDir();
  const worktree = await tempDir();
  const record = await createClaudeRunner().run(
    makeTask(),
    makeContext(worktree, checkout, new AbortController().signal, {
      extraArgs: [fakeClaude, "--dangerously-skip-permissions", "DUMP_ARGV"],
    }),
    () => {},
  );

  expect(record.status).toBe("succeeded");
  const dumped = JSON.parse(await readFile(join(worktree, "burner-argv.json"), "utf8")) as { argv: string[] };
  expect(dumped.argv.includes("--dangerously-skip-permissions")).toBe(false);
  expect(dumped.argv.slice(1)).toEqual([
    fakeClaude,
    "DUMP_ARGV",
    "-p",
    prompt,
    "--permission-mode",
    "acceptEdits",
    "--max-turns",
    "30",
  ]);
});

test("a bin path ending in fake-claude.mjs is launched with node", async () => {
  const checkout = await tempDir();
  const worktree = await tempDir();
  const record = await createClaudeRunner().run(
    makeTask(),
    makeContext(worktree, checkout, new AbortController().signal, {
      bin: fakeClaude,
      extraArgs: ["DUMP_ARGV"],
    }),
    () => {},
  );

  expect(record.status).toBe("succeeded");
  const dumped = JSON.parse(await readFile(join(worktree, "burner-argv.json"), "utf8")) as { argv: string[]; cwd: string };
  expect(dumped.cwd).toBe(await realpath(worktree));
  expect(dumped.argv[0]).toBe(process.execPath);
  expect(dumped.argv.slice(1)).toEqual([
    fakeClaude,
    "DUMP_ARGV",
    "-p",
    prompt,
    "--permission-mode",
    "acceptEdits",
    "--max-turns",
    "30",
  ]);
  expect(dumped.argv.includes("--output-format")).toBe(false);
});

test("real claude argv adds stream-json and tool flags and never git push", async () => {
  const checkout = await tempDir();
  const worktree = await tempDir();
  const binDir = await tempDir();
  const bin = join(binDir, "claude-bin.mjs");
  await copyFile(fakeClaude, bin);
  await chmod(bin, 0o755);

  const record = await createClaudeRunner().run(
    makeTask(),
    makeContext(worktree, checkout, new AbortController().signal, {
      bin,
      maxTurns: 12,
      model: "sonnet",
      extraArgs: ["DUMP_ARGV"],
      allowedTools: ["Read", "Bash(git push:*)", "Edit"],
      disallowedTools: ["Bash(git push:*)", "Bash(rm -rf:*)"],
    }),
    () => {},
  );

  expect(record.status).toBe("succeeded");
  const dumped = JSON.parse(await readFile(join(worktree, "burner-argv.json"), "utf8")) as { argv: string[]; cwd: string };
  expect(dumped.cwd).toBe(await realpath(worktree));
  expect(dumped.argv.slice(2)).toEqual([
    "DUMP_ARGV",
    "-p",
    prompt,
    "--permission-mode",
    "acceptEdits",
    "--max-turns",
    "12",
    "--output-format",
    "stream-json",
    "--verbose",
    "--allowedTools",
    "Read",
    "--allowedTools",
    "Edit",
    "--disallowedTools",
    "Bash(git push:*)",
    "--disallowedTools",
    "Bash(rm -rf:*)",
    "--model",
    "sonnet",
  ]);
  expect(flagValues(dumped.argv, "--allowedTools").some((tool) => /\bgit\s+push\b/i.test(tool))).toBe(false);
  expect(dumped.argv.some((arg) => arg.includes("dangerously-skip-permissions"))).toBe(false);
});

test("result usage replaces per-message usage", async () => {
  const checkout = await tempDir();
  const worktree = await tempDir();
  const events: RunnerEvent[] = [];
  const record = await createClaudeRunner().run(
    makeTask(),
    makeContext(worktree, checkout, new AbortController().signal, { extraArgs: [fakeClaude, "MULTI"] }),
    (event) => events.push(event),
  );

  expect(record.status).toBe("succeeded");
  expect(record.summary).toBe("added burner-note.txt");
  expect(record.usage).toEqual({ input: 120, output: 40, cacheRead: 3, cacheWrite: 4, costUsd: 0.02 });
  const usage = events.filter((event) => event.type === "usage");
  expect(usage.map((event) => event.usage.input)).toEqual([10, 120]);
  expect(events.filter((event) => event.type === "tool").map((event) => event.tool)).toEqual(["Write"]);
});

test.each([
  ["LIMIT", "usage limit reached"],
  ["RATE", "rate limit"],
  ["HIT", "hit your limit"],
  ["TOO_MANY", "status 429"],
])("marks %s output limited and does not retry", async (word, message) => {
  const checkout = await tempDir();
  const worktree = await tempDir();
  const events: RunnerEvent[] = [];
  const record = await createClaudeRunner().run(
    makeTask(),
    makeContext(worktree, checkout, new AbortController().signal, { extraArgs: [fakeClaude, word] }),
    (event) => events.push(event),
  );

  expect(record.status).toBe("limited");
  expect(record.error).toBe(message);
  expect(events.filter((event) => event.type === "limit")).toEqual([{ type: "limit", runId: "run-1", message }]);
  expect(events.filter((event) => event.type === "done")).toHaveLength(1);
  await expect(readFile(join(worktree, "burner-note.txt"), "utf8")).rejects.toThrow();
});

test("nonzero exit is failed", async () => {
  const checkout = await tempDir();
  const worktree = await tempDir();
  const record = await createClaudeRunner().run(
    makeTask(),
    makeContext(worktree, checkout, new AbortController().signal, { extraArgs: [fakeClaude, "FAIL"] }),
    () => {},
  );

  expect(record.status).toBe("failed");
  expect(record.error).toContain("claude exited 1");
  expect(record.error).toContain("fake-claude failed");
});

test("AbortSignal kills the child", async () => {
  const checkout = await tempDir();
  const worktree = await tempDir();
  const controller = new AbortController();
  const pending = createClaudeRunner().run(
    makeTask(),
    makeContext(worktree, checkout, controller.signal, { extraArgs: [fakeClaude, "HANG"] }),
    () => {},
  );
  setTimeout(() => controller.abort(), 40);
  const record = await pending;
  expect(record.status).toBe("cancelled");
  expect(record.error).toBe("aborted");
});

test("a hung child is killed on the run timeout", async () => {
  const checkout = await tempDir();
  const worktree = await tempDir();
  const record = await createClaudeRunner().run(
    makeTask(),
    makeContext(worktree, checkout, new AbortController().signal, { extraArgs: [fakeClaude, "HANG"] }, 1 / 60),
    () => {},
  );
  expect(record.status).toBe("failed");
  expect(record.error).toBe("claude timed out");
});

test("refuses a relative cwd and a missing bin", async () => {
  const checkout = await tempDir();
  const worktree = await tempDir();
  const runner = createClaudeRunner();
  const relative = await runner.run(makeTask(), makeContext("relative-worktree", checkout, new AbortController().signal), () => {});
  expect(relative.status).toBe("failed");
  expect(relative.error).toMatch(/absolute worktree/);

  const missing = await runner.run(
    makeTask(),
    makeContext(worktree, checkout, new AbortController().signal, { bin: join(worktree, "missing-claude"), extraArgs: [] }),
    () => {},
  );
  expect(missing.status).toBe("failed");
  expect(missing.error).toBeTruthy();
  await expect(readFile(join(checkout, "burner-note.txt"), "utf8")).rejects.toThrow();
});

test("redacts secret-looking text in the prompt argv", async () => {
  const checkout = await tempDir();
  const worktree = await tempDir();
  await createClaudeRunner().run(
    makeTask("ship it sk-abc123456789"),
    makeContext(worktree, checkout, new AbortController().signal, { extraArgs: [fakeClaude, "DUMP_ARGV"] }),
    () => {},
  );
  const dumped = JSON.parse(await readFile(join(worktree, "burner-argv.json"), "utf8")) as { argv: string[] };
  expect(dumped.argv.some((arg) => arg.includes("sk-abc123456789"))).toBe(false);
  expect(dumped.argv).toContain("ship it [redacted]");
});
