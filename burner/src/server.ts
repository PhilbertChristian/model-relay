// Local dashboard: JSON state, SSE, and control. Binds 127.0.0.1 only.
import { readFile } from "node:fs/promises";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { join } from "node:path";
import type { ControlAction } from "./types.js";

const CONTROL_ACTIONS: readonly ControlAction[] = ["start", "pause", "resume", "stop", "boost", "calm"];

const FALLBACK_HTML = `<!doctype html>
<html lang="en">
<meta charset="utf-8">
<title>BurnerAgent</title>
<h1>BurnerAgent</h1>
</html>
`;

const HEARTBEAT_MS = 15_000;
const MAX_BODY = 1_048_576;

export function startServer(opts: {
  port: number;
  getState: () => unknown;
  subscribe: (fn: (event: unknown) => void) => () => void;
  onControl: (action: "start" | "pause" | "resume" | "stop" | "boost" | "calm") => void;
  webDir?: string;
}): Promise<{ url: string; close: () => Promise<void> }> {
  const sseCleanups = new Set<() => void>();

  const server = createServer((req, res) => {
    void handle(req, res, opts, sseCleanups).catch((err) => {
      if (res.writableEnded || res.destroyed || res.headersSent) {
        res.destroy();
        return;
      }
      const status = statusOf(err);
      sendText(res, status, status === 413 ? "payload too large" : "internal error");
    });
  });

  server.on("clientError", (_err, socket) => {
    socket.destroy();
  });

  return new Promise((resolve, reject) => {
    const onListenError = (err: Error) => {
      server.off("error", onListenError);
      reject(err);
    };
    server.on("error", onListenError);
    server.listen(opts.port, "127.0.0.1", () => {
      server.off("error", onListenError);
      const addr = server.address();
      if (!addr || typeof addr === "string") {
        reject(new Error("server did not bind a TCP port"));
        return;
      }
      let closing: Promise<void> | undefined;
      resolve({
        url: `http://127.0.0.1:${addr.port}`,
        close: () => {
          if (closing) return closing;
          closing = new Promise<void>((resClose, rejClose) => {
            for (const cleanup of [...sseCleanups]) cleanup();
            server.close((err) => (err ? rejClose(err) : resClose()));
            server.closeAllConnections();
          });
          return closing;
        },
      });
    });
  });
}

async function handle(
  req: IncomingMessage,
  res: ServerResponse,
  opts: {
    getState: () => unknown;
    subscribe: (fn: (event: unknown) => void) => () => void;
    onControl: (action: ControlAction) => void;
    webDir?: string;
  },
  sseCleanups: Set<() => void>,
): Promise<void> {
  const method = req.method ?? "GET";
  const path = pathnameOf(req);

  if (method === "GET" && path === "/api/state") {
    sendJson(res, 200, opts.getState());
    return;
  }
  if (method === "GET" && path === "/api/events") {
    serveEvents(req, res, opts.subscribe, sseCleanups);
    return;
  }
  if (method === "POST" && path === "/api/control") {
    await serveControl(req, res, opts.onControl);
    return;
  }
  if (method === "GET" && path === "/") {
    await sendStatic(res, opts.webDir, "index.html", "text/html; charset=utf-8", FALLBACK_HTML);
    return;
  }
  if (method === "GET" && path === "/styles.css") {
    await sendStatic(res, opts.webDir, "styles.css", "text/css; charset=utf-8");
    return;
  }
  if (method === "GET" && path === "/app.js") {
    await sendStatic(res, opts.webDir, "app.js", "text/javascript; charset=utf-8");
    return;
  }
  sendText(res, 404, "not found");
}

function serveEvents(
  req: IncomingMessage,
  res: ServerResponse,
  subscribe: (fn: (event: unknown) => void) => () => void,
  sseCleanups: Set<() => void>,
) {
  let done = false;
  const unsubscribe = subscribe((event) => {
    writeSse(res, `data: ${JSON.stringify(event) ?? "null"}\n\n`, cleanup);
  });

  const heartbeat = setInterval(() => {
    writeSse(res, ": heartbeat\n\n", cleanup);
  }, HEARTBEAT_MS);
  heartbeat.unref();

  function cleanup() {
    if (done) return;
    done = true;
    clearInterval(heartbeat);
    sseCleanups.delete(cleanup);
    try {
      unsubscribe();
    } catch {
      /* already removed */
    }
    if (!res.writableEnded && !res.destroyed) res.end();
  }

  sseCleanups.add(cleanup);
  // Response close covers client disconnect. Request "close" can fire as soon
  // as an empty GET body ends, which would drop the stream immediately.
  res.on("close", cleanup);
  req.socket?.on("close", cleanup);

  res.writeHead(200, {
    "content-type": "text/event-stream",
    "cache-control": "no-cache, no-transform",
    connection: "keep-alive",
    "x-accel-buffering": "no",
  });
  // writeHead alone can sit in the corked socket until the first body chunk.
  // Flush now so clients are not blocked until the 15s heartbeat.
  res.flushHeaders();
  res.socket?.setNoDelay(true);
  res.socket?.uncork();
}

async function serveControl(
  req: IncomingMessage,
  res: ServerResponse,
  onControl: (action: ControlAction) => void,
) {
  const raw = await readBody(req);
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    sendJson(res, 400, { error: "invalid json" });
    return;
  }
  const action =
    parsed && typeof parsed === "object" && "action" in parsed ? (parsed as { action: unknown }).action : undefined;
  if (!isControlAction(action)) {
    sendJson(res, 400, { error: "unknown action" });
    return;
  }
  onControl(action);
  res.writeHead(204, { "cache-control": "no-store" });
  res.end();
}

async function sendStatic(
  res: ServerResponse,
  webDir: string | undefined,
  name: string,
  contentType: string,
  fallback?: string,
) {
  if (webDir) {
    try {
      const body = await readFile(join(webDir, name));
      res.writeHead(200, {
        "content-type": contentType,
        "content-length": body.length,
        "cache-control": "no-cache",
      });
      res.end(body);
      return;
    } catch (err) {
      const code = (err as NodeJS.ErrnoException).code;
      if (code !== "ENOENT" && code !== "ENOTDIR" && code !== "EISDIR") throw err;
    }
  }
  if (fallback !== undefined) {
    sendText(res, 200, fallback, "text/html; charset=utf-8");
    return;
  }
  sendText(res, 404, "not found");
}

function writeSse(res: ServerResponse, chunk: string, cleanup: () => void) {
  if (res.writableEnded || res.destroyed) {
    cleanup();
    return;
  }
  try {
    res.write(chunk);
  } catch {
    cleanup();
  }
}

function isControlAction(value: unknown): value is ControlAction {
  return typeof value === "string" && (CONTROL_ACTIONS as readonly string[]).includes(value);
}

function pathnameOf(req: IncomingMessage): string {
  try {
    return new URL(req.url ?? "/", "http://127.0.0.1").pathname;
  } catch {
    return "/";
  }
}

function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let size = 0;
    const onData = (chunk: Buffer) => {
      size += chunk.length;
      if (size > MAX_BODY) {
        cleanup();
        reject(Object.assign(new Error("payload too large"), { status: 413 }));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    };
    const onEnd = () => {
      cleanup();
      resolve(Buffer.concat(chunks).toString("utf8"));
    };
    const onError = (err: Error) => {
      cleanup();
      reject(err);
    };
    const cleanup = () => {
      req.off("data", onData);
      req.off("end", onEnd);
      req.off("error", onError);
    };
    req.on("data", onData);
    req.on("end", onEnd);
    req.on("error", onError);
  });
}

function sendJson(res: ServerResponse, status: number, body: unknown) {
  sendText(res, status, JSON.stringify(body), "application/json; charset=utf-8");
}

function sendText(res: ServerResponse, status: number, body: string, contentType = "text/plain; charset=utf-8") {
  const payload = Buffer.from(body);
  res.writeHead(status, {
    "content-type": contentType,
    "content-length": payload.length,
    "cache-control": "no-store",
  });
  res.end(payload);
}

function statusOf(err: unknown): number {
  if (typeof err === "object" && err && "status" in err && typeof (err as { status: unknown }).status === "number") {
    return (err as { status: number }).status;
  }
  return 500;
}
