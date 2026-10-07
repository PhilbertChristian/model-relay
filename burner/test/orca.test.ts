import { EventEmitter } from "node:events";
import type { ChildProcess } from "node:child_process";
import { afterEach, describe, expect, it } from "vitest";
import { DEFAULT_CONFIG } from "../src/config.js";
import { createOrcaRunner } from "../src/runners/orca.js";
import type { ProjectInfo, RunContext, RunnerEvent, Task } from "../src/types.js";

type SpawnFn = typeof import("node:child_process").spawn;

type SpawnCall = {
  command: string;
  args: readonly string[] | undefined;
  cwd?: string;
  env?: NodeJS.ProcessEnv;
  shell?: boolean;
};

const WORKTREE = "/tmp/burner/worktrees/proj/task1";
const CHECKOUT = "/tmp/user-checkout";

function project(): ProjectInfo {
  return {
    id: "proj",
    name: "proj",
    path: CHECKOUT,
    languages: ["typescript"],
    lastCommitAt: null,
    dirty: false,
    hasTests: true,
    todoCount: 0,
    packageManager: "npm",
    testCommand: "npm test",
    readmeExcerpt: "",
    score: 1,
  };
}

function task(): Task {
  return {
    id: "task1",
    projectId: "proj",
    kind: "tests",
    title: "add a unit test",
    prompt: "Write a unit test for slug.",
    priority: 1,
    estTokens: 1000,
    context: [],
    createdAt: "2026-01-01T00:00:00.000Z",
  };
}

function ctx(enabled: boolean): RunContext {
  return {
    runId: "run1",
    project: project(),
    cwd: WORKTREE,
    branch: "burner/task1",
    signal: new AbortController().signal,
    config: { ...DEFAULT_CONFIG, orca: { enabled } },
  };
}

function fakeChild(stdout: string, code: number): ChildProcess {
  const child = new EventEmitter() as ChildProcess;
  const out = new EventEmitter();
  const err = new EventEmitter();
  child.stdout = out as ChildProcess["stdout"];
  child.stderr = err as ChildProcess["stderr"];
  child.kill = () => true;
  queueMicrotask(() => {
    out.emit("data", stdout);
    child.emit("close", code);
  });
  return child;
}

function injectSpawn(stdout: string, code: number) {
  const calls: SpawnCall[] = [];
  const spawnImpl = ((
    command: string,
    args?: readonly string[],
    options?: { cwd?: string; env?: NodeJS.ProcessEnv; shell?: boolean },
  ) => {
    calls.push({ command, args, cwd: options?.cwd, env: options?.env, shell: options?.shell });
    return fakeChild(stdout, code);
  }) as SpawnFn;
  return { calls, spawnImpl };
}

describe("createOrcaRunner", () => {
  const prevOrca = process.env.BURNER_ORCA;
  const prevAgent37 = process.env.AGENT37_API_KEY;
  const prevMonid = process.env.MONID_API_KEY;

  afterEach(() => {
    if (prevOrca === undefined) delete process.env.BURNER_ORCA;
    else process.env.BURNER_ORCA = prevOrca;
    if (prevAgent37 === undefined) delete process.env.AGENT37_API_KEY;
    else process.env.AGENT37_API_KEY = prevAgent37;
    if (prevMonid === undefined) delete process.env.MONID_API_KEY;
    else process.env.MONID_API_KEY = prevMonid;
  });

  it("is available when a spawnImpl is injected or BURNER_ORCA=1", async () => {
    delete process.env.BURNER_ORCA;
    const { spawnImpl } = injectSpawn("done\n", 0);
    expect(await createOrcaRunner().available()).toBe(false);
    expect(await createOrcaRunner(spawnImpl).available()).toBe(true);
    process.env.BURNER_ORCA = "1";
    expect(await createOrcaRunner().available()).toBe(true);
    process.env.BURNER_ORCA = "0";
    expect(await createOrcaRunner().available()).toBe(false);
  });

  it("spawns orca in the worktree and succeeds on stdout done", async () => {
    process.env.AGENT37_API_KEY = "agent37-secret";
    process.env.MONID_API_KEY = "monid-secret";
    const { calls, spawnImpl } = injectSpawn("done\n", 0);
    const events: RunnerEvent[] = [];
    const prompt = task().prompt;

    const result = await createOrcaRunner(spawnImpl).run(task(), ctx(true), (e) => events.push(e));

    expect(result.status).toBe("succeeded");
    expect(result.lane).toBe("orca");
    expect(result.summary).toBe("done");
    expect(result.worktreePath).toBe(WORKTREE);
    expect(calls).toHaveLength(1);
    expect(calls[0].command).toBe("orca");
    expect(calls[0].args).toEqual(["--no-input", prompt]);
    expect(calls[0].cwd).toBe(WORKTREE);
    expect(calls[0].cwd).not.toBe(CHECKOUT);
    expect(calls[0].shell).toBe(false);
    expect(calls[0].env?.AGENT37_API_KEY).toBeUndefined();
    expect(calls[0].env?.MONID_API_KEY).toBeUndefined();
    expect(JSON.stringify(calls[0].args)).not.toContain("agent37-secret");
    expect(JSON.stringify(calls[0].args)).not.toContain("monid-secret");
    expect(events.some((e) => e.type === "log" && e.line === "done")).toBe(true);
    expect(events.some((e) => e.type === "done" && e.status === "succeeded")).toBe(true);
  });

  it("returns limited when stdout contains rate limit and does not retry", async () => {
    const { calls, spawnImpl } = injectSpawn("error: rate limit exceeded\n", 1);
    const events: RunnerEvent[] = [];

    const result = await createOrcaRunner(spawnImpl).run(task(), ctx(true), (e) => events.push(e));

    expect(result.status).toBe("limited");
    expect(calls).toHaveLength(1);
    expect(events.some((e) => e.type === "limit")).toBe(true);
    expect(events.some((e) => e.type === "done" && e.status === "limited")).toBe(true);
  });

  it("returns limited when the child prints a usage-limit line", async () => {
    const { calls, spawnImpl } = injectSpawn("usage-limit\n", 1);
    const result = await createOrcaRunner(spawnImpl).run(task(), ctx(true), () => {});
    expect(result.status).toBe("limited");
    expect(calls).toHaveLength(1);
  });

  it("fails closed when the orca lane is disabled and does not spawn", async () => {
    const { calls, spawnImpl } = injectSpawn("done\n", 0);
    process.env.BURNER_ORCA = "1";
    const result = await createOrcaRunner(spawnImpl).run(task(), ctx(false), () => {});
    expect(result).toMatchObject({ status: "failed", error: "orca lane disabled" });
    expect(calls).toHaveLength(0);
  });
});
