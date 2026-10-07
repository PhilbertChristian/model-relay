import { spawn as nodeSpawn, type ChildProcess, type SpawnOptions } from "node:child_process";
import type { RunContext, RunRecord, RunStatus, Runner, RunnerEvent, Task } from "../types.js";
import { emptyUsage, nowIso, truncate } from "../util.js";

const BIN = "orca";

// Env names that would hand the child a credential or an auth socket.
const SECRET_ENV_RE = /(api[_-]?key|token|secret|password|passwd|credential|auth)/i;
const LIMIT_RE = /rate limit|usage-limit|usage limit/i;
const ASSIGNED_SECRET_RE =
  /((?:api[_-]?key|token|secret|password|authorization)\s*[=:]\s*)(\S+)/gi;

function safeEnv(env: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const out: NodeJS.ProcessEnv = {};
  for (const [key, value] of Object.entries(env)) {
    if (SECRET_ENV_RE.test(key)) continue;
    out[key] = value;
  }
  return out;
}

function redact(text: string): string {
  return text.replace(ASSIGNED_SECRET_RE, "$1[redacted]");
}

function isLimited(text: string): boolean {
  return LIMIT_RE.test(text);
}

function limitMessage(text: string): string {
  const line = text.split(/\r?\n/).find((l) => isLimited(l));
  return (line ?? "rate limit").trim().slice(0, 500);
}

export function createOrcaRunner(spawnImpl?: typeof import("node:child_process").spawn): Runner {
  const spawn = spawnImpl ?? nodeSpawn;

  return {
    lane: "orca",

    async available() {
      if (spawnImpl) return true;
      return process.env.BURNER_ORCA === "1";
    },

    async run(task: Task, ctx: RunContext, emit: (e: RunnerEvent) => void): Promise<RunRecord> {
      const startedAt = nowIso();
      const base = {
        id: ctx.runId,
        taskId: task.id,
        projectId: task.projectId,
        lane: "orca" as const,
        title: task.title,
        branch: ctx.branch,
        worktreePath: ctx.cwd,
        startedAt,
        usage: emptyUsage(),
      };

      const deliver = (status: RunStatus, extra: Partial<RunRecord> = {}): RunRecord => {
        const rec: RunRecord = { ...base, status, endedAt: nowIso(), ...extra };
        emit({
          type: "done",
          runId: ctx.runId,
          status,
          summary: rec.summary,
          error: rec.error,
        });
        return rec;
      };

      if (!ctx.config.orca.enabled) {
        return deliver("failed", { error: "orca lane disabled" });
      }

      if (ctx.signal.aborted) {
        return deliver("cancelled", { error: "aborted" });
      }

      const options: SpawnOptions = {
        cwd: ctx.cwd,
        env: safeEnv(process.env),
        shell: false,
        stdio: ["ignore", "pipe", "pipe"],
      };

      let child: ChildProcess;
      try {
        // Prompt is one argv element. No credentials, no extra flags.
        child = spawn(BIN, ["--no-input", task.prompt], options);
      } catch (err) {
        const error = err instanceof Error ? err.message : String(err);
        return deliver("failed", { error });
      }

      return await new Promise<RunRecord>((resolve) => {
        let settled = false;
        let sawLimit = false;
        let timedOut = false;
        let buf = "";
        let pending = "";

        const finish = (status: RunStatus, extra: Partial<RunRecord> = {}) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          ctx.signal.removeEventListener("abort", onAbort);
          resolve(deliver(status, extra));
        };

        const summaryOf = () => {
          const text = redact(buf).trim();
          return text ? truncate(text, 2000) : undefined;
        };

        const pushText = (chunk: Buffer | string) => {
          const text = chunk.toString();
          buf += text;
          pending += text;
          const parts = pending.split(/\r?\n/);
          pending = parts.pop() ?? "";
          for (const line of parts) {
            if (line.length) emit({ type: "log", runId: ctx.runId, line: redact(line) });
          }
          if (!sawLimit && isLimited(buf)) {
            sawLimit = true;
            emit({ type: "limit", runId: ctx.runId, message: limitMessage(buf) });
          }
        };

        const onAbort = () => {
          try {
            child.kill("SIGTERM");
          } catch {
            /* already exited */
          }
        };

        const timer = setTimeout(
          () => {
            timedOut = true;
            onAbort();
          },
          Math.max(1, ctx.config.runTimeoutMin) * 60_000,
        );
        timer.unref();

        child.stdout?.on("data", pushText);
        child.stderr?.on("data", pushText);
        ctx.signal.addEventListener("abort", onAbort, { once: true });

        child.on("error", (err) => {
          const summary = summaryOf();
          if (sawLimit || isLimited(buf)) {
            finish("limited", { summary, error: limitMessage(buf) });
            return;
          }
          finish("failed", { summary, error: err.message });
        });

        child.on("close", (code) => {
          if (pending.length) {
            emit({ type: "log", runId: ctx.runId, line: redact(pending) });
            pending = "";
          }
          const summary = summaryOf();
          if (sawLimit || isLimited(buf)) {
            finish("limited", { summary, error: limitMessage(buf) });
            return;
          }
          if (ctx.signal.aborted) {
            finish("cancelled", { summary, error: "aborted" });
            return;
          }
          if (timedOut) {
            finish("failed", { summary, error: "orca timed out" });
            return;
          }
          if (code === 0) finish("succeeded", { summary });
          else finish("failed", { summary, error: `orca exited ${code ?? "unknown"}` });
        });
      });
    },
  };
}
