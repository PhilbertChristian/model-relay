import { describe, expect, it } from "vitest";
import { enrichTask, monidStatus } from "../src/monid.js";
import type { Task } from "../src/types.js";

const base = { apiKey: "test-key", baseUrl: "https://api.monid.ai", enabled: true };

function task(over: Partial<Task> = {}): Task {
  return {
    id: "t1",
    projectId: "p1",
    kind: "docs",
    title: "Add docs",
    prompt: "Write a README for the widget.",
    priority: 1,
    estTokens: 100,
    context: ["existing"],
    createdAt: "2026-01-01T00:00:00.000Z",
    ...over,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("monid", () => {
  it("returns the same task and reports disabled without calling fetch", async () => {
    let calls = 0;
    const fetchImpl = (async () => {
      calls += 1;
      return jsonResponse({ notes: ["nope"] });
    }) as typeof fetch;

    const t = task();
    const off = await enrichTask(t, { ...base, enabled: false }, fetchImpl);
    const noKey = await enrichTask(t, { ...base, apiKey: undefined }, fetchImpl);
    expect(off).toBe(t);
    expect(noKey).toBe(t);
    expect(off.context).toEqual(["existing"]);
    expect(await monidStatus({ ...base, enabled: false }, fetchImpl)).toEqual({
      ok: false,
      message: "monid disabled",
    });
    expect(await monidStatus({ ...base, apiKey: "" }, fetchImpl)).toEqual({
      ok: false,
      message: "monid disabled",
    });
    expect(calls).toBe(0);
  });

  it("appends up to 3 short redacted notes and does not mutate the input", async () => {
    const long = `note-${"n".repeat(600)}`;
    const secret = "prefix sk-live-abc123 ghp_zzzzzzzz Bearer super.secret suffix";
    const calls: { url: string; init?: RequestInit }[] = [];
    const prompt = `sk-should-not-send ${"p".repeat(1200)}`;
    const t = task({
      prompt,
      context: ["existing", "DO_NOT_SEND_FILE"],
    });

    const fetchImpl = (async (input: Parameters<typeof fetch>[0], init?: RequestInit) => {
      calls.push({ url: String(input), init });
      return jsonResponse({
        notes: ["alpha", secret, long, "fourth-dropped", 12],
      });
    }) as typeof fetch;

    const out = await enrichTask(t, { ...base, baseUrl: "https://api.monid.ai/" }, fetchImpl);

    expect(calls).toHaveLength(1);
    expect(calls[0]?.url).toBe("https://api.monid.ai/v1/research");
    expect(calls[0]?.init?.method).toBe("POST");
    const headers = new Headers(calls[0]?.init?.headers);
    expect(headers.get("Authorization")).toBe("Bearer test-key");
    expect(headers.get("Content-Type")).toBe("application/json");
    const body = JSON.parse(String(calls[0]?.init?.body)) as { query: string };
    expect(Object.keys(body)).toEqual(["query"]);
    const prefix = prompt.slice(0, 1000).replace(/\bsk-\S*/g, "[redacted]");
    expect(body.query).toBe(`Add docs\n${prefix}`);
    expect(prompt.slice(0, 1000)).toContain("sk-should-not-send");
    expect(body.query).not.toContain("sk-should-not-send");
    expect(body.query.length).toBeLessThan(`Add docs\n${prompt.slice(0, 1000)}`.length);
    expect(body.query).not.toContain("DO_NOT_SEND_FILE");
    expect(body.query).not.toContain("existing");

    expect(out).not.toBe(t);
    expect(t.context).toEqual(["existing", "DO_NOT_SEND_FILE"]);
    expect(out.context).toEqual([
      "existing",
      "DO_NOT_SEND_FILE",
      "alpha",
      "prefix [redacted] [redacted] [redacted] suffix",
      long.slice(0, 500),
    ]);
    expect(out.context[4]?.length).toBe(500);
    expect(out.context.join("\n")).not.toMatch(/sk-|ghp_|Bearer\s+\S+/);
  });

  it("does not retry on 429 and leaves the task unchanged", async () => {
    let calls = 0;
    const fetchImpl = (async () => {
      calls += 1;
      return new Response(JSON.stringify({ error: "rate limit" }), {
        status: 429,
        headers: { "Retry-After": "1" },
      });
    }) as typeof fetch;

    const t = task();
    const out = await enrichTask(t, base, fetchImpl);
    expect(out).toBe(t);
    expect(out.context).toEqual(["existing"]);
    expect(calls).toBe(1);

    const status = await monidStatus(base, fetchImpl);
    expect(status).toEqual({ ok: false, message: "limited" });
    expect(calls).toBe(2);
  });

  it("treats 402 and a rate-limit body as limited, once each", async () => {
    let calls = 0;
    const fetchImpl = (async () => {
      calls += 1;
      return new Response("payment required", { status: 402 });
    }) as typeof fetch;
    const t = task();
    expect(await enrichTask(t, base, fetchImpl)).toBe(t);
    expect(calls).toBe(1);

    const rateBody = (async () => new Response('{"error":"rate-limit"}', { status: 200 })) as typeof fetch;
    expect(await enrichTask(t, base, rateBody)).toBe(t);
    expect(await monidStatus(base, rateBody)).toEqual({ ok: false, message: "limited" });
  });

  it("returns the original task on network errors and reports the error string", async () => {
    const fetchImpl = (async () => {
      throw new Error("socket hang up");
    }) as typeof fetch;
    const t = task();
    await expect(enrichTask(t, base, fetchImpl)).resolves.toBe(t);
    await expect(monidStatus(base, fetchImpl)).resolves.toEqual({ ok: false, message: "socket hang up" });
  });
});
