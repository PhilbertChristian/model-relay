import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import type { RunContext, RunRecord, Runner, RunnerEvent, Task } from "../types.js";
import { addUsage, emptyUsage, nowIso, sleep } from "../util.js";

type Beat =
  | { type: "log"; line: string }
  | { type: "tool"; tool: string; detail?: string }
  | { type: "usage"; add: ReturnType<typeof emptyUsage> }
  | { type: "write" };

const demoNote = (title: string) =>
  [`# ${title}`, "", "Mock lane demo. No API keys and no network. Paced so the dashboard looks alive.", ""].join(
    "\n",
  );

export function createMockRunner(opts?: { tickMs?: number }): Runner {
  const tickMs = opts?.tickMs ?? 40;

  return {
    lane: "mock",
    available: async () => true,
    async run(task: Task, ctx: RunContext, emit: (e: RunnerEvent) => void): Promise<RunRecord> {
      const startedAt = nowIso();
      let usage = emptyUsage();

      const finish = (
        status: "succeeded" | "cancelled" | "failed",
        summary?: string,
        error?: string,
        extra?: Pick<RunRecord, "filesChanged" | "insertions" | "deletions">,
      ): RunRecord => {
        emit({ type: "done", runId: ctx.runId, status, summary, error });
        return {
          id: ctx.runId,
          taskId: task.id,
          projectId: task.projectId,
          lane: "mock",
          status,
          title: task.title,
          branch: ctx.branch,
          worktreePath: ctx.cwd,
          startedAt,
          endedAt: nowIso(),
          usage: { ...usage },
          summary,
          error,
          ...extra,
        };
      };

      try {
        if (ctx.signal.aborted) return finish("cancelled", "Mock run cancelled.");

        const note = demoNote(task.title);
        const beats: Beat[] = [
          { type: "log", line: `Starting mock run: ${task.title}` },
          { type: "tool", tool: "read", detail: "task prompt" },
          { type: "log", line: "Read the task locally. No network." },
          { type: "usage", add: { input: 860, output: 48, cacheRead: 110, cacheWrite: 0 } },
          { type: "log", line: "Drafting DEMO.md." },
          { type: "tool", tool: "edit", detail: "DEMO.md" },
          { type: "write" },
          { type: "log", line: "Wrote DEMO.md." },
          { type: "usage", add: { input: 240, output: 820, cacheRead: 36, cacheWrite: 160 } },
          { type: "tool", tool: "bash", detail: "printf ok" },
          { type: "log", line: "Simulated bash check: ok." },
          { type: "log", line: "Tokens climbing through a few thousand." },
          { type: "usage", add: { input: 150, output: 410, cacheRead: 24, cacheWrite: 18 } },
          { type: "log", line: "Preparing the summary." },
          { type: "log", line: "Mock lane finished." },
        ];

        for (const beat of beats) {
          await sleep(tickMs, ctx.signal);
          if (beat.type === "log") {
            emit({ type: "log", runId: ctx.runId, line: beat.line });
          } else if (beat.type === "tool") {
            emit({ type: "tool", runId: ctx.runId, tool: beat.tool, detail: beat.detail });
          } else if (beat.type === "usage") {
            usage = addUsage(usage, beat.add);
            emit({ type: "usage", runId: ctx.runId, usage: { ...usage } });
          } else {
            await mkdir(ctx.cwd, { recursive: true });
            await writeFile(join(ctx.cwd, "DEMO.md"), note, "utf8");
          }
        }

        return finish("succeeded", `Wrote DEMO.md for ${task.title}.`, undefined, {
          filesChanged: 1,
          insertions: note.split("\n").filter((line) => line.length > 0).length,
          deletions: 0,
        });
      } catch (err) {
        if (ctx.signal.aborted) return finish("cancelled", "Mock run cancelled.");
        const error = err instanceof Error ? err.message : String(err);
        return finish("failed", undefined, error);
      }
    },
  };
}
