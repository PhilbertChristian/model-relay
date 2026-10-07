import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, expect, test } from "vitest";
import { DEFAULT_CONFIG } from "../src/config.js";
import { createMockRunner } from "../src/runners/mock.js";
import type { ProjectInfo, RunContext, RunnerEvent, Task } from "../src/types.js";

const dirs: string[] = [];

afterEach(async () => {
  await Promise.all(dirs.splice(0).map((dir) => rm(dir, { recursive: true, force: true })));
});

async function tempCwd(): Promise<string> {
  const cwd = await mkdtemp(join(tmpdir(), "burner-mock-"));
  dirs.push(cwd);
  return cwd;
}

function fakeContext(cwd: string, signal: AbortSignal): RunContext {
  const project: ProjectInfo = {
    id: "demo-app",
    name: "demo-app",
    path: cwd,
    languages: ["typescript"],
    lastCommitAt: null,
    dirty: false,
    hasTests: false,
    todoCount: 0,
    packageManager: "npm",
    testCommand: null,
    readmeExcerpt: "",
    score: 1,
  };
  return {
    runId: "run-demo-1",
    project,
    cwd,
    branch: "burner/task-demo",
    signal,
    config: { ...DEFAULT_CONFIG, demo: true },
  };
}

function fakeTask(): Task {
  return {
    id: "task-demo",
    projectId: "demo-app",
    kind: "docs",
    title: "Write the demo note",
    prompt: "Leave a short DEMO.md so the dashboard looks alive.",
    priority: 1,
    estTokens: 3000,
    lane: "mock",
    context: [],
    createdAt: "2026-10-07T00:00:00.000Z",
  };
}

const tokens = (usage: { input: number; output: number; cacheRead: number; cacheWrite: number }) =>
  usage.input + usage.output + usage.cacheRead + usage.cacheWrite;

test("mock runner writes DEMO.md and succeeds with usage", async () => {
  const cwd = await tempCwd();
  const events: RunnerEvent[] = [];
  const runner = createMockRunner();
  const task = fakeTask();

  expect(runner.lane).toBe("mock");
  await expect(runner.available()).resolves.toBe(true);

  const record = await runner.run(task, fakeContext(cwd, new AbortController().signal), (event) => {
    events.push(event);
  });

  const usageEvents = events.filter((event) => event.type === "usage");
  expect(usageEvents.length).toBeGreaterThan(0);
  for (let i = 1; i < usageEvents.length; i++) {
    expect(tokens(usageEvents[i]!.usage)).toBeGreaterThan(tokens(usageEvents[i - 1]!.usage));
  }
  const spent = tokens(usageEvents.at(-1)!.usage);
  expect(spent).toBeGreaterThan(1000);
  expect(spent).toBeLessThan(20_000);

  expect(events.filter((event) => event.type === "log")).toHaveLength(8);
  expect(events.filter((event) => event.type === "tool").map((event) => event.tool)).toEqual(["read", "edit", "bash"]);

  expect(record.status).toBe("succeeded");
  expect(record.summary).toContain(task.title);
  expect(events.some((event) => event.type === "done" && event.status === "succeeded")).toBe(true);

  const demo = await readFile(join(cwd, "DEMO.md"), "utf8");
  expect(demo).toContain(task.title);
  expect(demo).toContain("No API keys");
});

test("mock runner cancels when the signal aborts", async () => {
  const cwd = await tempCwd();
  const events: RunnerEvent[] = [];
  const runner = createMockRunner({ tickMs: 30 });
  const controller = new AbortController();

  const record = await runner.run(fakeTask(), fakeContext(cwd, controller.signal), (event) => {
    events.push(event);
    controller.abort();
  });

  expect(record.status).toBe("cancelled");
  expect(events.some((event) => event.type === "done" && event.status === "cancelled")).toBe(true);
  await expect(readFile(join(cwd, "DEMO.md"), "utf8")).rejects.toThrow();
});
