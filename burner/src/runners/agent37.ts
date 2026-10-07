import { loadConfig } from "../config.js";
import type { RunContext, RunRecord, RunStatus, Runner, RunnerEvent, Task, TokenUsage } from "../types.js";
import { emptyUsage, nowIso, truncate } from "../util.js";

// How many instances this process has created and not yet released.
let activeInstances = 0;

const LIMIT_RE = /rate[\s_-]*limit|quota|budget/i;
const SECRET_RE =
  /\b(?:sk-[A-Za-z0-9_\-]{8,}|sk_(?:live|test)_[A-Za-z0-9]{8,}|ghp_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}|xox[baprs]-[A-Za-z0-9-]{8,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z\-_]{20,})\b/g;

type Json = Record<string, unknown>;

const hasApiKey = (apiKey: string | undefined): apiKey is string =>
  typeof apiKey === "string" && apiKey.trim().length > 0;

function oneLine(value: string): string {
  return value.replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim();
}

function redact(value: string, apiKey: string): string {
  let out = value.replace(
    /-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----/g,
    "[redacted]",
  );
  out = out.replace(SECRET_RE, "[redacted]");
  out = out.replace(
    /\b(api[_-]?key|access[_-]?token|secret|password)\b(\s*[:=]\s*)\S+/gi,
    "$1$2[redacted]",
  );
  out = out.replace(/\bBearer\s+[A-Za-z0-9._\-]{8,}/gi, "Bearer [redacted]");
  if (apiKey.length >= 8) out = out.split(apiKey).join("[redacted]");
  return out;
}

function shQuote(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

// Echo the title into a temp file and print the prompt length. The prompt itself
// may ride along, truncated. Nothing from disk, no repo upload, no transcripts.
function buildCommand(task: Task, apiKey: string): string {
  const title = oneLine(redact(task.title, apiKey));
  const prompt = truncate(oneLine(redact(task.prompt, apiKey)), 1500);
  const length = String(task.prompt.length);
  return [
    `printf '%s\\n' ${shQuote(title)} > /tmp/burner-task.txt`,
    `printf 'prompt_len=%s\\n' ${shQuote(length)}`,
    `printf '%s\\n' ${shQuote(prompt)}`,
  ].join(" && ");
}

function parseJson(raw: string): Json {
  if (!raw.trim()) return {};
  try {
    const value = JSON.parse(raw) as unknown;
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    return value as Json;
  } catch {
    return {};
  }
}

function limitProbe(status: number, raw: string): string {
  if (status === 429 || status === 402) return raw || `HTTP ${status}`;
  try {
    const parsed = JSON.parse(raw) as Json;
    const rest: Json = { ...parsed };
    delete rest.stdout;
    delete rest.stderr;
    delete rest.usage;
    return JSON.stringify(rest);
  } catch {
    return status >= 400 ? raw : "";
  }
}

function isLimited(status: number, raw: string): boolean {
  if (status === 429 || status === 402) return true;
  const probe = limitProbe(status, raw);
  return probe.length > 0 && LIMIT_RE.test(probe);
}

function limitMessage(status: number, raw: string): string {
  const detail = truncate(raw.replace(/\s+/g, " ").trim(), 240);
  if (status === 429 || status === 402) {
    return detail ? `agent37 limited: HTTP ${status} ${detail}` : `agent37 limited: HTTP ${status}`;
  }
  return `agent37 limited: ${detail || `HTTP ${status}`}`;
}

function readResetsAt(res: Response, raw: string): string | undefined {
  const parsed = parseJson(raw);
  for (const key of ["resetsAt", "resets_at", "reset_at", "retry_at"]) {
    const value = parsed[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  const header = res.headers.get("retry-after");
  if (!header) return undefined;
  const seconds = Number(header);
  if (Number.isFinite(seconds)) return new Date(Date.now() + seconds * 1000).toISOString();
  const when = new Date(header);
  return Number.isNaN(when.getTime()) ? undefined : when.toISOString();
}

function num(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function mergeUsage(usage: TokenUsage, json: Json): TokenUsage {
  const raw = json.usage;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return usage;
  const u = raw as Json;
  const input = num(u.input ?? u.input_tokens ?? u.prompt_tokens) ?? 0;
  const output = num(u.output ?? u.output_tokens ?? u.completion_tokens) ?? 0;
  const cacheRead = num(u.cacheRead ?? u.cache_read) ?? 0;
  const cacheWrite = num(u.cacheWrite ?? u.cache_write) ?? 0;
  const cost = num(u.costUsd ?? u.cost_usd);
  if (input === 0 && output === 0 && cacheRead === 0 && cacheWrite === 0 && cost === undefined) return usage;
  return {
    input: usage.input + input,
    output: usage.output + output,
    cacheRead: usage.cacheRead + cacheRead,
    cacheWrite: usage.cacheWrite + cacheWrite,
    ...(cost !== undefined || usage.costUsd !== undefined ? { costUsd: (usage.costUsd ?? 0) + (cost ?? 0) } : {}),
  };
}

function isAbort(err: unknown, signal: AbortSignal): boolean {
  return signal.aborted || (err instanceof Error && err.name === "AbortError");
}

function errorText(err: unknown): string {
  if (err instanceof Error && err.message) return err.message;
  return String(err);
}

export function createAgent37Runner(fetchImpl?: typeof fetch): Runner {
  const http: typeof fetch = fetchImpl ?? ((input, init) => globalThis.fetch(input, init));

  return {
    lane: "agent37",
    // Runner.available() receives no config. loadConfig() is the same object the
    // app uses; the key is whatever that object already has.
    async available() {
      try {
        const key = loadConfig().agent37.apiKey;
        return hasApiKey(key);
      } catch {
        return false;
      }
    },
    async run(task: Task, ctx: RunContext, emit: (e: RunnerEvent) => void): Promise<RunRecord> {
      const startedAt = nowIso();
      let usage = emptyUsage();
      let slot = false;
      let instanceId: string | undefined;
      const cfg = ctx.config.agent37;
      const apiKey = cfg.apiKey?.trim() ?? "";
      const baseUrl = (cfg.baseUrl || "https://api.agent37.com").replace(/\/+$/, "");
      const maxInstances = cfg.maxInstances;

      const finish = (
        status: Extract<RunStatus, "succeeded" | "failed" | "limited" | "cancelled">,
        summary?: string,
        error?: string,
      ): RunRecord => {
        emit({ type: "done", runId: ctx.runId, status, summary, error });
        return {
          id: ctx.runId,
          taskId: task.id,
          projectId: task.projectId,
          lane: "agent37",
          status,
          title: task.title,
          branch: ctx.branch,
          worktreePath: ctx.cwd,
          startedAt,
          endedAt: nowIso(),
          usage: { ...usage },
          ...(summary ? { summary } : {}),
          ...(error ? { error } : {}),
        };
      };

      const fail = (error: string) => finish("failed", undefined, truncate(redact(error, apiKey), 500));

      const authHeaders = (jsonBody: boolean): Record<string, string> => ({
        Authorization: `Bearer ${apiKey}`,
        ...(jsonBody ? { "Content-Type": "application/json" } : {}),
      });

      const request = async (path: string, method: string, body?: Json): Promise<Response> =>
        http(`${baseUrl}${path}`, {
          method,
          headers: authHeaders(body !== undefined),
          ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
          signal: ctx.signal,
        });

      try {
        if (ctx.signal.aborted) return finish("cancelled", undefined, "aborted");
        if (!hasApiKey(apiKey)) return fail("agent37 api key is empty");
        if (Number.isFinite(maxInstances) && activeInstances >= maxInstances) {
          return fail(`agent37 already has ${activeInstances} instances (max ${maxInstances})`);
        }

        activeInstances += 1;
        slot = true;
        emit({ type: "log", runId: ctx.runId, line: `Creating agent37 instance (${cfg.template}).` });

        const createdRes = await request("/v1/instances", "POST", {
          name: "burner",
          template: cfg.template,
          auto_sleep: true,
        });
        const createdRaw = await createdRes.text();
        if (isLimited(createdRes.status, createdRaw)) {
          const message = truncate(redact(limitMessage(createdRes.status, createdRaw), apiKey), 300);
          const resetsAt = readResetsAt(createdRes, createdRaw);
          emit({ type: "limit", runId: ctx.runId, message, ...(resetsAt ? { resetsAt } : {}) });
          return finish("limited", undefined, message);
        }
        if (!createdRes.ok) return fail(`agent37 create failed: HTTP ${createdRes.status}`);

        const created = parseJson(createdRaw);
        usage = mergeUsage(usage, created);
        if (typeof created.id !== "string" || !created.id) return fail("agent37 create returned no instance id");
        instanceId = created.id;

        emit({ type: "tool", runId: ctx.runId, tool: "exec", detail: instanceId });
        const execRes = await request(`/v1/instances/${encodeURIComponent(instanceId)}/exec`, "POST", {
          command: buildCommand(task, apiKey),
        });
        const execRaw = await execRes.text();
        if (isLimited(execRes.status, execRaw)) {
          const message = truncate(redact(limitMessage(execRes.status, execRaw), apiKey), 300);
          const resetsAt = readResetsAt(execRes, execRaw);
          emit({ type: "limit", runId: ctx.runId, message, ...(resetsAt ? { resetsAt } : {}) });
          return finish("limited", undefined, message);
        }
        if (!execRes.ok) return fail(`agent37 exec failed: HTTP ${execRes.status}`);

        const execBody = parseJson(execRaw);
        usage = mergeUsage(usage, execBody);
        if (usage.input || usage.output || usage.cacheRead || usage.cacheWrite || usage.costUsd) {
          emit({ type: "usage", runId: ctx.runId, usage: { ...usage } });
        }
        const stdout = typeof execBody.stdout === "string" ? execBody.stdout : "";
        const summary = stdout ? truncate(redact(stdout, apiKey), 2000) : undefined;
        if (summary) emit({ type: "log", runId: ctx.runId, line: summary });
        return finish("succeeded", summary);
      } catch (err) {
        if (isAbort(err, ctx.signal)) return finish("cancelled", undefined, "aborted");
        return fail(errorText(err));
      } finally {
        if (slot) activeInstances -= 1;
        if (instanceId && apiKey) {
          try {
            await http(`${baseUrl}/v1/instances/${encodeURIComponent(instanceId)}`, {
              method: "DELETE",
              headers: authHeaders(false),
            });
          } catch {
            // Cleanup must not reject the run.
          }
        }
      }
    },
  };
}
