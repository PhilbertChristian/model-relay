import { spawn, type ChildProcess } from "node:child_process";
import { accessSync, constants, existsSync } from "node:fs";
import { delimiter, isAbsolute, join } from "node:path";
import { DEFAULT_CONFIG } from "../config.js";
import type { RunContext, RunRecord, RunStatus, Runner, RunnerEvent, Task, TokenUsage } from "../types.js";
import { addUsage, emptyUsage, nowIso, truncate } from "../util.js";

const LIMIT_RE = /rate limit|usage limit|hit your limit|429/i;
const SUMMARY_MAX = 2000;

function resolveBin(bin: string): boolean {
  if (!bin) return false;
  if (isAbsolute(bin) || bin.includes("/") || bin.includes("\\")) return existsSync(bin);
  for (const dir of (process.env.PATH ?? "").split(delimiter)) {
    if (!dir) continue;
    try {
      accessSync(join(dir, bin), constants.X_OK);
      return true;
    } catch {
      /* next PATH entry */
    }
  }
  return false;
}

function isDangerousFlag(arg: string): boolean {
  return arg === "--dangerously-skip-permissions" || arg.startsWith("--dangerously-skip-permissions=");
}

function grantsGitPush(tool: string): boolean {
  return /\bgit\s+push\b/i.test(tool);
}

// Drop flags we must never forward. Do not invent a git push allow.
function sanitizeExtra(extra: string[]): string[] {
  const out: string[] = [];
  for (let i = 0; i < extra.length; i++) {
    const arg = extra[i] ?? "";
    if (isDangerousFlag(arg)) continue;
    if (arg === "git" && extra[i + 1] === "push") {
      i++;
      continue;
    }
    if (/\bgit\s+push\b/i.test(arg) && !arg.startsWith("--")) continue;
    out.push(arg);
  }
  return out;
}

function isFixture(bin: string, extraArgs: string[]): boolean {
  if (bin.endsWith("fake-claude.mjs") || extraArgs.some((arg) => arg.endsWith("fake-claude.mjs"))) return true;
  const base = bin.split(/[/\\]/).pop() ?? bin;
  // Tests set claude.bin to process.execPath and extraArgs to [absolute path of fake-claude.mjs].
  return bin === process.execPath || base === "node";
}

function redact(text: string): string {
  return text
    .replace(/\bsk-[A-Za-z0-9_\-]{8,}\b/g, "[redacted]")
    .replace(/\bghp_[A-Za-z0-9]{8,}\b/g, "[redacted]")
    .replace(/\bgithub_pat_[A-Za-z0-9_]{8,}\b/g, "[redacted]")
    .replace(/\bxox[baprs]-[A-Za-z0-9-]{8,}\b/g, "[redacted]")
    .replace(/\bAKIA[0-9A-Z]{16}\b/g, "[redacted]");
}

function num(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function extractUsage(parsed: Record<string, unknown>): TokenUsage | null {
  const message = parsed.message;
  const raw =
    parsed.usage && typeof parsed.usage === "object"
      ? (parsed.usage as Record<string, unknown>)
      : message && typeof message === "object" && (message as Record<string, unknown>).usage &&
          typeof (message as Record<string, unknown>).usage === "object"
        ? ((message as Record<string, unknown>).usage as Record<string, unknown>)
        : null;
  if (!raw) return null;
  if (raw.input_tokens == null && raw.output_tokens == null) return null;
  const usage: TokenUsage = {
    input: num(raw.input_tokens),
    output: num(raw.output_tokens),
    cacheRead: num(raw.cache_read_input_tokens ?? raw.cache_read_tokens),
    cacheWrite: num(raw.cache_creation_input_tokens ?? raw.cache_write_tokens),
  };
  const cost = parsed.total_cost_usd ?? raw.cost_usd;
  if (typeof cost === "number") usage.costUsd = cost;
  return usage;
}

function collectTexts(node: unknown, out: string[], depth: number) {
  if (!node || depth > 8) return;
  if (Array.isArray(node)) {
    for (const item of node) collectTexts(item, out, depth + 1);
    return;
  }
  if (typeof node !== "object") return;
  const rec = node as Record<string, unknown>;
  if (rec.type === "text" && typeof rec.text === "string") out.push(rec.text);
  if (rec.message) collectTexts(rec.message, out, depth + 1);
  if (Array.isArray(rec.content)) collectTexts(rec.content, out, depth + 1);
}

function lastTextFrom(parsed: Record<string, unknown>, prev: string): string {
  if (typeof parsed.result === "string" && parsed.result.trim()) return parsed.result;
  const texts: string[] = [];
  collectTexts(parsed, texts, 0);
  const last = texts.findLast((text) => text.trim());
  return last ?? prev;
}

function collectTools(node: unknown, out: { tool: string; detail?: string }[], depth: number) {
  if (!node || depth > 8) return;
  if (Array.isArray(node)) {
    for (const item of node) collectTools(item, out, depth + 1);
    return;
  }
  if (typeof node !== "object") return;
  const rec = node as Record<string, unknown>;
  if (rec.type === "tool_use" && typeof rec.name === "string") {
    let detail: string | undefined;
    if (rec.input && typeof rec.input === "object") {
      const input = rec.input as Record<string, unknown>;
      const picked = input.file_path ?? input.path ?? input.command ?? input.pattern ?? input.query;
      if (typeof picked === "string") detail = truncate(redact(picked), 180);
    }
    out.push({ tool: rec.name, detail });
  }
  if (rec.message) collectTools(rec.message, out, depth + 1);
  if (Array.isArray(rec.content)) collectTools(rec.content, out, depth + 1);
}

function tryJson(line: string): Record<string, unknown> | null {
  const trimmed = line.trim();
  if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return null;
  try {
    const value = JSON.parse(trimmed) as unknown;
    if (Array.isArray(value)) return { content: value };
    if (value && typeof value === "object") return value as Record<string, unknown>;
    return null;
  } catch {
    return null;
  }
}

function killChild(child: ChildProcess) {
  try {
    child.kill("SIGTERM");
  } catch {
    /* already exited */
  }
  const timer = setTimeout(() => {
    try {
      child.kill("SIGKILL");
    } catch {
      /* already exited */
    }
  }, 400);
  timer.unref();
  child.once("close", () => clearTimeout(timer));
}

export function createClaudeRunner(): Runner {
  return {
    lane: "claude",

    async available() {
      return resolveBin(DEFAULT_CONFIG.claude.bin);
    },

    async run(task: Task, ctx: RunContext, emit: (e: RunnerEvent) => void): Promise<RunRecord> {
      const startedAt = nowIso();
      let usage = emptyUsage();
      let sawResultUsage = false;

      const deliver = (status: RunStatus, extra: Partial<RunRecord> = {}): RunRecord => {
        const rec: RunRecord = {
          id: ctx.runId,
          taskId: task.id,
          projectId: task.projectId,
          lane: "claude",
          status,
          title: task.title,
          branch: ctx.branch,
          worktreePath: ctx.cwd,
          startedAt,
          endedAt: nowIso(),
          usage: { ...usage },
          ...extra,
        };
        emit({ type: "done", runId: ctx.runId, status, summary: rec.summary, error: rec.error });
        return rec;
      };

      // Spawn only in the isolated worktree. Never fall back to the project checkout or process.cwd().
      if (!ctx.cwd || !isAbsolute(ctx.cwd)) {
        return deliver("failed", { error: "refusing to spawn without an absolute worktree cwd" });
      }
      if (ctx.signal.aborted) return deliver("cancelled", { error: "aborted" });

      const claude = ctx.config.claude;
      const mode = claude.permissionMode;
      const maxTurns = String(claude.maxTurns);
      let command = claude.bin;
      let extra = sanitizeExtra(claude.extraArgs);
      // If bin is the fixture script itself, run it with node. Tests instead set bin to
      // process.execPath and extraArgs to [absolute path of fake-claude.mjs].
      if (command.endsWith("fake-claude.mjs")) {
        extra = [command, ...extra];
        command = process.execPath;
      }

      // Tests set claude.bin to process.execPath and extraArgs to [absolute path of fake-claude.mjs].
      // spawn(bin, [...extraArgs, "-p", prompt, "--permission-mode", mode, "--max-turns", String(maxTurns)], { cwd })
      const args = [...extra, "-p", redact(task.prompt), "--permission-mode", mode, "--max-turns", maxTurns];
      const fixture = isFixture(claude.bin, claude.extraArgs);
      if (!fixture) {
        // Real Claude Code: stream-json with --print needs --verbose. Fake-claude just prints lines.
        args.push("--output-format", "stream-json", "--verbose");
        for (const tool of claude.allowedTools) {
          if (grantsGitPush(tool)) continue;
          args.push("--allowedTools", tool);
        }
        for (const tool of claude.disallowedTools) args.push("--disallowedTools", tool);
        if (claude.model) args.push("--model", claude.model);
      }
      if (args.some(isDangerousFlag)) {
        return deliver("failed", { error: "refusing --dangerously-skip-permissions" });
      }

      let child: ChildProcess;
      try {
        child = spawn(command, args, {
          cwd: ctx.cwd,
          shell: false,
          stdio: ["ignore", "pipe", "pipe"],
          signal: ctx.signal,
          env: process.env,
        });
      } catch (err) {
        const error = err instanceof Error ? err.message : String(err);
        if (ctx.signal.aborted || (err instanceof Error && err.name === "AbortError")) {
          return deliver("cancelled", { error: "aborted" });
        }
        return deliver("failed", { error });
      }

      return await new Promise<RunRecord>((resolve) => {
        let settled = false;
        let sawLimit = false;
        let limitMessage = "usage limit";
        let timedOut = false;
        let summaryText = "";
        let stderrText = "";

        const finish = (status: RunStatus, extra: Partial<RunRecord> = {}) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          ctx.signal.removeEventListener("abort", onAbort);
          resolve(deliver(status, extra));
        };

        const summary = () => {
          const text = redact(summaryText).trim();
          return text ? truncate(text, SUMMARY_MAX) : undefined;
        };

        const noteUsage = (parsed: Record<string, unknown>) => {
          const next = extractUsage(parsed);
          if (!next) return;
          if (parsed.type === "result") {
            usage = next;
            sawResultUsage = true;
          } else if (!sawResultUsage) {
            usage = addUsage(usage, next);
          } else {
            return;
          }
          emit({ type: "usage", runId: ctx.runId, usage: { ...usage } });
        };

        const onLine = (line: string, fromStderr: boolean) => {
          const clean = redact(line);
          if (fromStderr) stderrText += `${clean}\n`;
          emit({ type: "log", runId: ctx.runId, line: clean });
          // One look at the limit text, then stop. Do not retry and do not switch accounts.
          if (!sawLimit && LIMIT_RE.test(line)) {
            sawLimit = true;
            limitMessage = clean.trim().slice(0, 500);
            emit({ type: "limit", runId: ctx.runId, message: limitMessage });
          }
          const parsed = tryJson(line);
          if (!parsed) {
            if (!LIMIT_RE.test(line)) summaryText = clean;
            return;
          }
          summaryText = lastTextFrom(parsed, summaryText);
          noteUsage(parsed);
          const tools: { tool: string; detail?: string }[] = [];
          collectTools(parsed, tools, 0);
          for (const tool of tools) emit({ type: "tool", runId: ctx.runId, tool: tool.tool, detail: tool.detail });
        };

        const bind = (stream: NodeJS.ReadableStream | null, fromStderr: boolean) => {
          let pending = "";
          const flush = (rest: boolean) => {
            const parts = pending.split(/\r?\n/);
            const lines = rest ? parts : parts.slice(0, -1);
            pending = rest ? "" : (parts.at(-1) ?? "");
            for (const line of lines) {
              if (line.trim()) onLine(line, fromStderr);
            }
          };
          stream?.setEncoding("utf8");
          stream?.on("data", (chunk: string) => {
            pending += chunk;
            flush(false);
          });
          stream?.on("end", () => flush(true));
          return () => flush(true);
        };

        const flushOut = bind(child.stdout, false);
        const flushErr = bind(child.stderr, true);

        const onAbort = () => killChild(child);
        ctx.signal.addEventListener("abort", onAbort, { once: true });

        const timer = setTimeout(() => {
          timedOut = true;
          killChild(child);
        }, Math.max(1, ctx.config.runTimeoutMin * 60_000));
        timer.unref();

        child.on("error", (err) => {
          flushOut();
          flushErr();
          if (sawLimit) {
            finish("limited", { summary: summary() ?? truncate(limitMessage, SUMMARY_MAX), error: limitMessage });
            return;
          }
          if (ctx.signal.aborted || err.name === "AbortError") {
            finish("cancelled", { summary: summary(), error: "aborted" });
            return;
          }
          finish("failed", { summary: summary(), error: err.message });
        });

        child.on("close", (code) => {
          flushOut();
          flushErr();
          if (sawLimit) {
            finish("limited", { summary: summary() ?? truncate(limitMessage, SUMMARY_MAX), error: limitMessage });
            return;
          }
          if (ctx.signal.aborted) {
            finish("cancelled", { summary: summary(), error: "aborted" });
            return;
          }
          if (timedOut) {
            finish("failed", { summary: summary(), error: "claude timed out" });
            return;
          }
          if (code === 0) {
            finish("succeeded", { summary: summary() });
            return;
          }
          const detail = stderrText.trim();
          const error = detail
            ? truncate(redact(`claude exited ${code ?? "unknown"}: ${detail}`), SUMMARY_MAX)
            : `claude exited ${code ?? "unknown"}`;
          finish("failed", { summary: summary(), error });
        });
      });
    },
  };
}
