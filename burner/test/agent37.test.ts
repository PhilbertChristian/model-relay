import { afterEach, beforeEach, expect, test } from "vitest";
import { DEFAULT_CONFIG } from "../src/config.js";
import { createAgent37Runner } from "../src/runners/agent37.js";
import type { BurnerConfig, ProjectInfo, RunContext, RunnerEvent, Task } from "../src/types.js";

type Call = {
  url: string;
  method: string;
  authorization: string | null;
  body: unknown;
};

let savedKey: string | undefined;

beforeEach(() => {
  savedKey = process.env.AGENT37_API_KEY;
  delete process.env.AGENT37_API_KEY;
});

afterEach(() => {
  if (savedKey === undefined) delete process.env.AGENT37_API_KEY;
  else process.env.AGENT37_API_KEY = savedKey;
});

function jsonResponse(body: unknown, status = 200): Response {
  const text = typeof body === "string" ? body : JSON.stringify(body);
  return new Response(text, {
    status,
    headers: { "Content-Type": typeof body === "string" ? "text/plain" : "application/json" },
  });
}

function scripted(responses: Array<{ status?: number; body: unknown }>): { fetchImpl: typeof fetch; calls: Call[] } {
  const calls: Call[] = [];
  const fetchImpl: typeof fetch = async (input, init) => {
    const raw = init?.body;
    calls.push({
      url: String(input),
      method: init?.method ?? "GET",
      authorization: new Headers(init?.headers).get("authorization"),
      body: typeof raw === "string" ? JSON.parse(raw) : undefined,
    });
    const next = responses[calls.length - 1] ?? { body: {} };
    return jsonResponse(next.body, next.status ?? 200);
  };
  return { fetchImpl, calls };
}

function project(): ProjectInfo {
  return {
    id: "demo-app",
    name: "demo-app",
    path: "/tmp/demo-app",
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

function task(over: Partial<Task> = {}): Task {
  return {
    id: "task-1",
    projectId: "demo-app",
    kind: "tests",
    title: "it's a parser",
    prompt: "Add a parser. Ignore sk-live-THISISASECRET123456.",
    priority: 1,
    estTokens: 1000,
    lane: "agent37",
    context: ["transcript that must stay local"],
    createdAt: "2026-10-07T00:00:00.000Z",
    ...over,
  };
}

function context(
  over: Partial<BurnerConfig["agent37"]> = {},
  runId = "run-1",
  signal = new AbortController().signal,
): RunContext {
  return {
    runId,
    project: project(),
    cwd: "/tmp/burner-worktree",
    branch: "burner/task-1",
    signal,
    config: {
      ...DEFAULT_CONFIG,
      agent37: {
        ...DEFAULT_CONFIG.agent37,
        apiKey: "test-key",
        baseUrl: "https://api.agent37.com",
        template: "agent37-claude-code",
        maxInstances: 3,
        ...over,
      },
    },
  };
}

test("creates, execs, and deletes an instance with the bearer token", async () => {
  const { fetchImpl, calls } = scripted([{ body: { id: "inst_1" } }, { body: { stdout: "ok" } }, { body: {} }]);
  const events: RunnerEvent[] = [];
  const runner = createAgent37Runner(fetchImpl);
  const job = task();

  expect(runner.lane).toBe("agent37");
  const record = await runner.run(job, context(), (event) => events.push(event));

  expect(calls.map((call) => `${call.method} ${call.url}`)).toEqual([
    "POST https://api.agent37.com/v1/instances",
    "POST https://api.agent37.com/v1/instances/inst_1/exec",
    "DELETE https://api.agent37.com/v1/instances/inst_1",
  ]);
  for (const call of calls) expect(call.authorization).toBe("Bearer test-key");
  expect(calls[0]?.body).toEqual({ name: "burner", template: "agent37-claude-code", auto_sleep: true });

  const command = (calls[1]?.body as { command: string }).command;
  expect(command).toContain(" > /tmp/burner-task.txt");
  expect(command).toContain("prompt_len=");
  expect(command).toContain(String(job.prompt.length));
  expect(command).toContain(`'it'\\''s a parser'`);
  expect(command).not.toContain("sk-live-THISISASECRET123456");
  expect(command).not.toContain("transcript that must stay local");
  expect(command).not.toMatch(/\bcat\b/);
  expect(command).not.toMatch(/git\s+push/);
  expect(command).not.toContain("test-key");

  expect(record.status).toBe("succeeded");
  expect(record.lane).toBe("agent37");
  expect(record.summary).toBe("ok");
  expect(record.usage).toEqual({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 });
  expect(events.some((event) => event.type === "done" && event.status === "succeeded")).toBe(true);
  expect(events.some((event) => event.type === "limit")).toBe(false);
});

test("does not retry a 429 Response and reports limited", async () => {
  const calls: string[] = [];
  const fetchImpl: typeof fetch = async (input) => {
    calls.push(String(input));
    return new Response(JSON.stringify({ error: "rate limit exceeded" }), {
      status: 429,
      headers: { "Content-Type": "application/json" },
    });
  };
  const events: RunnerEvent[] = [];
  const runner = createAgent37Runner(fetchImpl);
  const record = await runner.run(task(), context(), (event) => events.push(event));

  expect(record.status).toBe("limited");
  expect(calls).toEqual(["https://api.agent37.com/v1/instances"]);
  expect(events.some((event) => event.type === "limit")).toBe(true);
  expect(events.some((event) => event.type === "done" && event.status === "limited")).toBe(true);
});

test("402 and quota or budget bodies are limited with a single request", async () => {
  for (const response of [
    jsonResponse({ error: "payment required" }, 402),
    jsonResponse("quota exceeded", 400),
    jsonResponse({ message: "over budget" }, 403),
  ]) {
    const calls: string[] = [];
    const fetchImpl: typeof fetch = async (input) => {
      calls.push(String(input));
      return response.clone();
    };
    const runner = createAgent37Runner(fetchImpl);
    const record = await runner.run(task(), context(), () => {});
    expect(record.status).toBe("limited");
    expect(calls).toHaveLength(1);
  }
});

test("available is true only when cfg.agent37.apiKey is non-empty", async () => {
  const runner = createAgent37Runner(async () => {
    throw new Error("no network");
  });
  await expect(runner.available()).resolves.toBe(false);
  process.env.AGENT37_API_KEY = "sk_live_testkey";
  await expect(runner.available()).resolves.toBe(true);
  process.env.AGENT37_API_KEY = "";
  await expect(runner.available()).resolves.toBe(false);
});

test("refuses a new instance when the module cap is taken", async () => {
  let release: () => void = () => {};
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const urls: string[] = [];
  const fetchImpl: typeof fetch = async (input, init) => {
    const url = String(input);
    urls.push(`${init?.method ?? "GET"} ${url}`);
    if (init?.method === "DELETE") return jsonResponse({});
    if (url.endsWith("/exec")) return jsonResponse({ stdout: "ok" });
    await gate;
    return jsonResponse({ id: "inst_1" });
  };
  const runner = createAgent37Runner(fetchImpl);
  const first = runner.run(task(), context({ maxInstances: 1 }, "run-a"), () => {});
  const second = await runner.run(task(), context({ maxInstances: 1 }, "run-b"), () => {});
  expect(second.status).toBe("failed");
  expect(urls).toEqual(["POST https://api.agent37.com/v1/instances"]);
  release();
  await expect(first).resolves.toMatchObject({ status: "succeeded" });
  const third = await runner.run(task(), context({ maxInstances: 1 }, "run-c"), () => {});
  expect(third.status).toBe("succeeded");
});

test("truncates exec stdout and keeps usage the API returns", async () => {
  const stdout = `${"a".repeat(2500)}TAIL`;
  const { fetchImpl } = scripted([
    { body: { id: "inst_1" } },
    { body: { stdout, usage: { input: 3, output: 4, cache_read: 1, cache_write: 2, cost_usd: 0.5 } } },
    { body: {} },
  ]);
  const events: RunnerEvent[] = [];
  const record = await createAgent37Runner(fetchImpl).run(task(), context(), (event) => events.push(event));
  expect(record.summary?.length).toBe(2000);
  expect(record.summary?.endsWith("…")).toBe(true);
  expect(record.summary).not.toContain("TAIL");
  expect(record.usage).toEqual({ input: 3, output: 4, cacheRead: 1, cacheWrite: 2, costUsd: 0.5 });
  expect(events.some((event) => event.type === "usage" && event.usage.input === 3)).toBe(true);
});

test("truncates the prompt inside the exec command", async () => {
  const prompt = `${"b".repeat(2000)}END`;
  const { fetchImpl, calls } = scripted([{ body: { id: "inst_1" } }, { body: { stdout: "ok" } }, { body: {} }]);
  await createAgent37Runner(fetchImpl).run(task({ prompt }), context(), () => {});
  const command = (calls[1]?.body as { command: string }).command;
  expect(command).toContain(String(prompt.length));
  expect(command).not.toContain("END");
});

test("uses the configured base URL and resolves when fetch throws", async () => {
  const { fetchImpl, calls } = scripted([{ body: { id: "inst_9" } }, { body: { stdout: "ok" } }, { body: {} }]);
  const record = await createAgent37Runner(fetchImpl).run(
    task(),
    context({ baseUrl: "https://cloud.example/api/", template: "tmpl-a" }),
    () => {},
  );
  expect(record.status).toBe("succeeded");
  expect(calls[0]?.url).toBe("https://cloud.example/api/v1/instances");
  expect(calls[0]?.body).toMatchObject({ template: "tmpl-a", auto_sleep: true });
  expect(calls[2]?.url).toBe("https://cloud.example/api/v1/instances/inst_9");

  const boom: typeof fetch = async () => {
    throw new Error("network down");
  };
  await expect(createAgent37Runner(boom).run(task(), context(), () => {})).resolves.toMatchObject({
    status: "failed",
    error: "network down",
  });
});

test("cancels before any request when the signal is already aborted", async () => {
  const calls: string[] = [];
  const fetchImpl: typeof fetch = async (input) => {
    calls.push(String(input));
    return jsonResponse({});
  };
  const controller = new AbortController();
  controller.abort();
  const record = await createAgent37Runner(fetchImpl).run(task(), context({}, "run-abort", controller.signal), () => {});
  expect(record.status).toBe("cancelled");
  expect(calls).toHaveLength(0);
});
