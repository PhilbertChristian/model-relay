import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { startServer } from "../src/server.js";

type Action = "start" | "pause" | "resume" | "stop" | "boost" | "calm";
type Listener = (event: unknown) => void;

// Local stand-in for Store: snapshot() for state, attach() to a tiny bus.
function createFake(initial: unknown) {
  let state = initial;
  const listeners = new Set<Listener>();
  return {
    snapshot: () => state,
    setState(next: unknown) {
      state = next;
    },
    attach(source: { on: (fn: Listener) => () => void }) {
      return source.on((event) => {
        for (const fn of [...listeners]) fn(event);
      });
    },
    subscribe(fn: Listener) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    size: () => listeners.size,
  };
}

function createBus() {
  const handlers = new Set<Listener>();
  return {
    on(fn: Listener) {
      handlers.add(fn);
      return () => handlers.delete(fn);
    },
    emit(event: unknown) {
      for (const fn of [...handlers]) fn(event);
    },
  };
}

async function waitFor(pred: () => boolean, ms = 1500) {
  const start = Date.now();
  while (!pred()) {
    if (Date.now() - start > ms) throw new Error("timed out");
    await new Promise((r) => setTimeout(r, 15));
  }
}

async function readUntil(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  pred: (text: string) => boolean,
  ms = 2000,
) {
  const dec = new TextDecoder();
  let text = "";
  const timer = setTimeout(() => {
    void reader.cancel().catch(() => {});
  }, ms);
  try {
    while (!pred(text)) {
      const { value, done } = await reader.read();
      if (done) break;
      if (value) text += dec.decode(value, { stream: true });
    }
  } finally {
    clearTimeout(timer);
  }
  return text;
}

describe("startServer", () => {
  const open: { close: () => Promise<void> }[] = [];

  afterEach(async () => {
    await Promise.all(open.splice(0).map((srv) => srv.close()));
  });

  it("serves state JSON, posts control, and closes", async () => {
    const actions: Action[] = [];
    const fake = createFake({ state: "idle", demo: true });
    const bus = createBus();
    fake.attach(bus);

    const srv = await startServer({
      port: 0,
      getState: () => fake.snapshot(),
      subscribe: (fn) => fake.subscribe(fn),
      onControl: (action) => {
        actions.push(action);
      },
    });
    open.push(srv);

    expect(srv.url).toMatch(/^http:\/\/127\.0\.0\.1:\d+$/);
    expect(srv.url.endsWith(":0")).toBe(false);

    const stateRes = await fetch(`${srv.url}/api/state`);
    expect(stateRes.status).toBe(200);
    expect(stateRes.headers.get("content-type")).toContain("application/json");
    expect(await stateRes.json()).toEqual({ state: "idle", demo: true });

    fake.setState({ state: "burning", concurrency: 2 });
    expect(await (await fetch(`${srv.url}/api/state`)).json()).toEqual({ state: "burning", concurrency: 2 });

    const ctrl = await fetch(`${srv.url}/api/control`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ action: "boost" }),
    });
    expect(ctrl.status).toBe(204);
    expect(await ctrl.text()).toBe("");
    expect(actions).toEqual(["boost"]);

    const bad = await fetch(`${srv.url}/api/control`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ action: "explode" }),
    });
    expect(bad.status).toBe(400);
    expect(actions).toEqual(["boost"]);

    const home = await fetch(srv.url + "/");
    expect(home.status).toBe(200);
    expect(home.headers.get("content-type")).toContain("text/html");
    expect(await home.text()).toContain("BurnerAgent");

    expect((await fetch(`${srv.url}/styles.css`)).status).toBe(404);
    expect((await fetch(`${srv.url}/app.js`)).status).toBe(404);

    const url = srv.url;
    await srv.close();
    await srv.close();
    await expect(fetch(`${url}/api/state`)).rejects.toThrow();
  });

  it("streams SSE events and unsubscribes when the client disconnects", async () => {
    const fake = createFake({ state: "idle" });
    const bus = createBus();
    fake.attach(bus);

    const srv = await startServer({
      port: 0,
      getState: () => fake.snapshot(),
      subscribe: (fn) => fake.subscribe(fn),
      onControl: () => {},
    });
    open.push(srv);

    const ac = new AbortController();
    const res = await fetch(`${srv.url}/api/events`, { signal: ac.signal });
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toContain("text/event-stream");
    await waitFor(() => fake.size() === 1);

    const event = { type: "status", state: "burning" };
    const frame = `data: ${JSON.stringify(event)}\n\n`;
    bus.emit(event);
    const reader = res.body!.getReader();
    const text = await readUntil(reader, (chunk) => chunk.includes(frame));
    expect(text).toContain(frame);

    ac.abort();
    await reader.cancel().catch(() => {});
    await waitFor(() => fake.size() === 0);
  });

  it("serves index.html, styles.css, and app.js from webDir", async () => {
    const dir = await mkdtemp(join(tmpdir(), "burner-web-"));
    try {
      await writeFile(join(dir, "index.html"), "<!doctype html><title>from-disk</title><p>BurnerAgent</p>");
      await writeFile(join(dir, "styles.css"), "body{color:#c60}");
      await writeFile(join(dir, "app.js"), "globalThis.burner=1");

      const srv = await startServer({
        port: 0,
        webDir: dir,
        getState: () => ({ ok: true }),
        subscribe: () => () => {},
        onControl: () => {},
      });
      open.push(srv);

      const html = await fetch(srv.url + "/");
      expect(await html.text()).toContain("from-disk");
      const css = await fetch(`${srv.url}/styles.css`);
      expect(css.headers.get("content-type")).toContain("text/css");
      expect(await css.text()).toContain("color:#c60");
      const js = await fetch(`${srv.url}/app.js`);
      expect(js.headers.get("content-type")).toContain("javascript");
      expect(await js.text()).toContain("globalThis.burner=1");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });
});
