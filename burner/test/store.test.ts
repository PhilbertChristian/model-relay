import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { createBus } from "../src/events.js";
import { Store } from "../src/store.js";
import type { BudgetSnapshot, BurnerEvent, Idea, ProjectInfo, RunRecord, Task, TokenUsage } from "../src/types.js";

const roots: string[] = [];

afterEach(async () => {
  await Promise.all(roots.splice(0).map((dir) => rm(dir, { recursive: true, force: true })));
});

async function tempDir(): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(), "burner-store-"));
  roots.push(dir);
  return dir;
}

function usage(input: number, output = 0, cacheRead = 0, cacheWrite = 0, costUsd?: number): TokenUsage {
  return { input, output, cacheRead, cacheWrite, ...(costUsd !== undefined ? { costUsd } : {}) };
}

function project(id: string, name = id): ProjectInfo {
  return {
    id,
    name,
    path: `/tmp/${id}`,
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

function task(id: string, title = id): Task {
  return {
    id,
    projectId: "p1",
    kind: "tests",
    title,
    prompt: "do the work",
    priority: 1,
    estTokens: 10,
    context: [],
    createdAt: "2026-01-01T00:00:00.000Z",
  };
}

function run(id: string, tokens: TokenUsage, taskId = "t1"): RunRecord {
  return {
    id,
    taskId,
    projectId: "p1",
    lane: "mock",
    status: "running",
    title: id,
    usage: tokens,
  };
}

function idea(id: string, text = id): Idea {
  return { id, text, source: "sessions/a.jsonl", score: 1, status: "new" };
}

describe("Store", () => {
  it("starts from an empty idle snapshot", async () => {
    const store = new Store(await tempDir());
    expect(store.snapshot()).toEqual({
      state: "idle",
      concurrency: 1,
      projects: [],
      tasks: [],
      runs: [],
      logs: {},
      budget: null,
      integrations: {},
      ideas: [],
      feed: [],
      startedAt: null,
      demo: false,
    });
    expect(store.tokensUsed()).toBe(0);
  });

  it("applies project, run, and log events and counts tokens once per run", async () => {
    const bus = createBus();
    const store = new Store(await tempDir());
    store.attach(bus);

    bus.emit({ type: "project", project: project("p1", "alpha") });
    bus.emit({ type: "project", project: project("p1", "beta") });
    bus.emit({ type: "project", project: project("p2", "gamma") });

    bus.emit({ type: "task", task: task("t1", "first") });
    bus.emit({ type: "task", task: task("t1", "replaced") });
    bus.emit({ type: "task", task: task("t2", "second") });

    bus.emit({ type: "run", run: { ...run("r1", usage(10, 5, 1, 2, 9)), status: "queued" } });
    expect(store.tokensUsed()).toBe(18);
    bus.emit({ type: "run", run: run("r1", usage(10, 5, 1, 2, 9)) });
    expect(store.tokensUsed()).toBe(18);
    bus.emit({ type: "run", run: run("r2", usage(4, 0, 0, 0)) });
    bus.emit({ type: "run", run: { ...run("r1", usage(12, 5, 1, 2)), status: "succeeded" } });
    expect(store.tokensUsed()).toBe(24);

    bus.emit({ type: "runner", event: { type: "log", runId: "r1", line: "hello" } });
    bus.emit({ type: "runner", event: { type: "tool", runId: "r1", tool: "Bash" } });
    bus.emit({ type: "runner", event: { type: "usage", runId: "r1", usage: usage(500, 0, 0, 0) } });

    const budget: BudgetSnapshot = {
      windowStart: "2026-10-04T00:00:00.000Z",
      windowEnd: "2026-10-11T00:00:00.000Z",
      tokensUsed: 24,
      tokensTarget: 1000,
      pctUsed: 2.4,
      hoursLeft: 10,
      burnRatePerHour: 1,
      neededRatePerHour: 2,
      recommendedConcurrency: 2,
      limited: false,
    };
    bus.emit({ type: "budget", budget });
    bus.emit({ type: "status", state: "burning", message: "go" });
    bus.emit({ type: "concurrency", value: 3 });
    bus.emit({
      type: "integration",
      name: "monid",
      ok: true,
      message: "connected",
      ...({ apiKey: "sk-should-not-stick" } as object),
    } as BurnerEvent);
    bus.emit({ type: "idea", idea: idea("i1", "ship it") });
    bus.emit({ type: "idea", idea: idea("i1", "ship it revised") });
    bus.emit({ type: "feed", source: "burner", message: "started", level: "info" });

    store.addTokens(6);
    store.setDemo(true);

    const snap = store.snapshot();
    expect(snap.projects.map((p) => p.name)).toEqual(["beta", "gamma"]);
    expect(snap.tasks.map((t) => t.title)).toEqual(["first", "second"]);
    expect(snap.runs.map((r) => r.id)).toEqual(["r1", "r2"]);
    expect(snap.runs[0]?.status).toBe("succeeded");
    expect(snap.logs.r1).toEqual(["hello"]);
    expect(store.tokensUsed()).toBe(30);
    expect(snap.budget?.tokensTarget).toBe(1000);
    expect(snap.state).toBe("burning");
    expect(snap.concurrency).toBe(3);
    expect(snap.integrations.monid).toEqual({ ok: true, message: "connected" });
    expect(snap.ideas).toEqual([idea("i1", "ship it revised")]);
    expect(snap.feed).toHaveLength(1);
    expect(snap.feed[0]?.message).toBe("started");
    expect(snap.feed[0]?.at).toMatch(/^\d{4}-\d{2}-\d{2}T/);
    expect(snap.demo).toBe(true);
    expect(snap.startedAt).toBeNull();

    snap.projects.push(project("mutated"));
    expect(store.snapshot().projects).toHaveLength(2);
  });

  it("caps runs at 500, logs at 40, and feed at 100", async () => {
    const bus = createBus();
    const store = new Store(await tempDir());
    store.attach(bus);

    bus.emit({ type: "project", project: project("p1") });
    bus.emit({ type: "run", run: run("keep-me", usage(3, 1, 0, 0)) });
    bus.emit({ type: "runner", event: { type: "log", runId: "keep-me", line: "first" } });
    for (let i = 0; i < 40; i++) {
      bus.emit({ type: "runner", event: { type: "log", runId: "keep-me", line: `line-${i}` } });
    }
    for (let i = 0; i < 500; i++) {
      bus.emit({ type: "run", run: run(`n${i}`, usage(0)) });
    }
    for (let i = 0; i < 101; i++) {
      bus.emit({ type: "feed", source: "ideas", message: `feed-${i}`, level: i % 2 === 0 ? "info" : "warn" });
    }

    const snap = store.snapshot();
    expect(snap.projects).toHaveLength(1);
    expect(snap.runs).toHaveLength(500);
    expect(snap.runs[0]?.id).toBe("n499");
    expect(snap.runs[499]?.id).toBe("n0");
    expect(snap.runs.some((r) => r.id === "keep-me")).toBe(false);
    expect(snap.logs["keep-me"]).toHaveLength(40);
    expect(snap.logs["keep-me"]?.[0]).toBe("line-0");
    expect(snap.logs["keep-me"]?.[39]).toBe("line-39");
    expect(snap.feed).toHaveLength(100);
    expect(snap.feed[0]?.message).toBe("feed-1");
    expect(snap.feed[99]?.message).toBe("feed-100");
    expect(snap.feed[99]?.at).toEqual(expect.any(String));
    expect(store.tokensUsed()).toBe(4);
  });

  it("persists state.json without API keys and reloads it", async () => {
    const root = await tempDir();
    const dataDir = join(root, "nested", "data");
    const bus = createBus();
    const store = new Store(dataDir);
    store.attach(bus);

    const secret = "sk-test-secret-value";
    bus.emit({ type: "project", project: { ...project("p1", "alpha"), apiKey: secret } as ProjectInfo });
    bus.emit({ type: "run", run: run("r1", usage(8, 2, 0, 0)) });
    bus.emit({ type: "runner", event: { type: "log", runId: "r1", line: "booted" } });
    bus.emit({ type: "feed", source: "burner", message: "hi" });
    store.addTokens(5);
    store.setDemo(true);
    await store.flush();

    const disk = await readFile(join(dataDir, "state.json"), "utf8");
    expect(disk).not.toContain(secret);
    expect(disk).not.toContain("apiKey");
    const parsed = JSON.parse(disk) as { tokensUsed: number; demo: boolean; projects: ProjectInfo[] };
    expect(parsed.tokensUsed).toBe(15);
    expect(parsed.demo).toBe(true);
    expect(parsed.projects[0]?.name).toBe("alpha");

    const restored = new Store(dataDir);
    await restored.load();
    expect(restored.tokensUsed()).toBe(15);
    expect(restored.snapshot().demo).toBe(true);
    expect(restored.snapshot().projects[0]?.id).toBe("p1");
    expect(restored.snapshot().runs[0]?.id).toBe("r1");
    expect(restored.snapshot().logs.r1).toEqual(["booted"]);
    expect(restored.snapshot().feed[0]?.message).toBe("hi");

    // Same cumulative usage must not be counted again after reload.
    const again = createBus();
    const live = new Store(dataDir);
    await live.load();
    live.attach(again);
    again.emit({ type: "run", run: run("r1", usage(8, 2, 0, 0)) });
    again.emit({ type: "run", run: run("r1", usage(9, 2, 0, 0)) });
    expect(live.tokensUsed()).toBe(16);
  });

  it("restores only snapshot fields that exist and ignores bad files", async () => {
    const root = await tempDir();
    const partialDir = join(root, "partial");
    await mkdir(partialDir, { recursive: true });
    const runs = Array.from({ length: 501 }, (_, i) => run(`id-${i}`, usage(i === 0 ? 7 : 0)));
    const longLog = Array.from({ length: 50 }, (_, i) => `L${i}`);
    await writeFile(
      join(partialDir, "state.json"),
      JSON.stringify({
        state: "paused",
        concurrency: 4,
        runs,
        logs: { "id-0": longLog },
        integrations: { monid: { ok: false, message: "nope", apiKey: "sk-from-disk" }, other: { ok: true } },
        startedAt: "2026-10-07T00:00:00.000Z",
      }),
      "utf8",
    );

    const store = new Store(partialDir);
    store.addTokens(3);
    await store.load();
    const snap = store.snapshot();
    expect(snap.state).toBe("paused");
    expect(snap.concurrency).toBe(4);
    expect(snap.projects).toEqual([]);
    expect(snap.demo).toBe(false);
    expect(snap.budget).toBeNull();
    expect(snap.runs).toHaveLength(500);
    expect(snap.runs[0]?.id).toBe("id-0");
    expect(snap.runs.some((r) => r.id === "id-500")).toBe(false);
    expect(snap.logs["id-0"]).toHaveLength(40);
    expect(snap.logs["id-0"]?.[0]).toBe("L10");
    expect(snap.integrations).toEqual({ monid: { ok: false, message: "nope" } });
    expect(snap.startedAt).toBe("2026-10-07T00:00:00.000Z");
    expect(store.tokensUsed()).toBe(7);

    const missing = new Store(join(root, "does-not-exist"));
    await expect(missing.load()).resolves.toBeUndefined();
    expect(missing.snapshot().state).toBe("idle");

    const badDir = join(root, "bad");
    await mkdir(badDir, { recursive: true });
    await writeFile(join(badDir, "state.json"), "{", "utf8");
    const bad = new Store(badDir);
    await expect(bad.load()).resolves.toBeUndefined();
    expect(bad.snapshot().state).toBe("idle");
  });

  it("does not throw on malformed events", async () => {
    const bus = createBus();
    const store = new Store(await tempDir());
    store.attach(bus);
    bus.emit({ type: "status", state: "paused" });

    expect(() => {
      bus.emit(null as unknown as BurnerEvent);
      bus.emit({ type: "project" } as BurnerEvent);
      bus.emit({ type: "project", project: null } as unknown as BurnerEvent);
      bus.emit({ type: "task", task: { title: "no id" } } as unknown as BurnerEvent);
      bus.emit({ type: "run", run: { usage: { input: 1 } } } as unknown as BurnerEvent);
      bus.emit({ type: "runner" } as BurnerEvent);
      bus.emit({ type: "runner", event: { type: "log", runId: 4, line: 4 } } as unknown as BurnerEvent);
      bus.emit({ type: "budget", budget: null } as unknown as BurnerEvent);
      bus.emit({ type: "status", state: "nope" } as unknown as BurnerEvent);
      bus.emit({ type: "concurrency", value: Number.NaN });
      bus.emit({ type: "integration", name: "nope", ok: true, message: "x" } as unknown as BurnerEvent);
      bus.emit({ type: "idea", idea: {} } as BurnerEvent);
      bus.emit({ type: "feed" } as BurnerEvent);
      bus.emit({ type: "feed", source: "burner" } as BurnerEvent);
    }).not.toThrow();

    expect(store.snapshot().state).toBe("paused");
    expect(store.snapshot().projects).toEqual([]);
    expect(store.tokensUsed()).toBe(0);
  });
});
