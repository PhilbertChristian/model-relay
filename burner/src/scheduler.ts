import { mkdir, realpath } from "node:fs/promises";
import { homedir } from "node:os";
import { join, resolve } from "node:path";
import { budgetSnapshot } from "./budget.js";
import { discoverProjects } from "./discover.js";
import type { Bus } from "./events.js";
import { mineIdeas } from "./ideas.js";
import { enrichTask } from "./monid.js";
import { planTasks } from "./planner.js";
import type {
  BurnerConfig,
  BurnerState,
  Idea,
  LaneName,
  ProjectInfo,
  RunRecord,
  Runner,
  RunnerEvent,
  Task,
  TokenUsage,
} from "./types.js";
import { emptyUsage, newId, nowIso, sleep, totalTokens } from "./util.js";
import { createBurnerWorktree, diffstat } from "./worktree.js";

const TICK_MS = 300;
const RATE_WINDOW_MS = 15 * 60 * 1000;
const MAX_TIMEOUT_MS = 2_147_483_647;

type Pace = "auto" | "boost" | "calm";

// Paces discover → plan → launch. In-flight runs are tracked, not awaited as a fleet.
export class Scheduler {
  private readonly cfg: BurnerConfig;
  private readonly bus: Bus;
  private readonly runners: Runner[];
  private readonly tokensUsed: () => number;
  private readonly dataDir: string;

  private _state: BurnerState = "idle";
  private generation = 0;
  private stopController = new AbortController();
  private queue: Task[] = [];
  private projects = new Map<string, ProjectInfo>();
  private runningCount = 0;
  private readonly inflight = new Set<Promise<void>>();
  private readonly limitedLanes = new Set<LaneName>();
  private limited = false;
  private limitResetsAt: string | undefined;
  private pace: Pace = "auto";
  private boostExtra = 0;
  private readonly usageByRun = new Map<string, number>();
  private samples: { at: number; tokens: number }[] = [];

  constructor(opts: {
    cfg: BurnerConfig;
    bus: Bus;
    runners: Runner[];
    tokensUsed: () => number;
    dataDir: string;
  }) {
    this.cfg = opts.cfg;
    this.bus = opts.bus;
    this.runners = opts.runners;
    this.tokensUsed = opts.tokensUsed;
    this.dataDir = opts.dataDir;
  }

  get state(): BurnerState {
    return this._state;
  }

  start(): void {
    if (this._state === "burning" || this._state === "paused" || this._state === "limited") return;
    const previous = this.stopController;
    this.generation += 1;
    this.stopController = new AbortController();
    previous.abort(new Error("restart"));
    this.queue = [];
    this.projects.clear();
    this.limited = false;
    this.limitedLanes.clear();
    this.limitResetsAt = undefined;
    this.usageByRun.clear();
    this.samples = [];
    this._state = "burning";
    this.bus.emit({ type: "status", state: "burning" });
    void this.runLoop(this.generation);
  }

  pause(): void {
    if (this._state !== "burning" && this._state !== "limited") return;
    this._state = "paused";
    this.bus.emit({ type: "status", state: "paused" });
  }

  resume(): void {
    if (this._state !== "paused") return;
    this._state = "burning";
    this.bus.emit({ type: "status", state: "burning" });
  }

  stop(): void {
    const already = this._state === "stopped" && this.stopController.signal.aborted;
    this._state = "stopped";
    if (!this.stopController.signal.aborted) this.stopController.abort(new Error("stopped"));
    if (!already) this.bus.emit({ type: "status", state: "stopped" });
  }

  boost(): void {
    this.pace = "boost";
    this.boostExtra = Math.min(4, this.boostExtra + 2);
  }

  calm(): void {
    this.pace = "calm";
    this.boostExtra = 0;
  }

  private alive(gen: number): boolean {
    return gen === this.generation && !this.stopController.signal.aborted && this._state !== "stopped";
  }

  private async runLoop(gen: number): Promise<void> {
    try {
      await this.prepare(gen);
      while (this.alive(gen)) {
        const halt = await this.tick(gen);
        if (halt || !this.alive(gen)) break;
        try {
          await sleep(TICK_MS, this.stopController.signal);
        } catch {
          break;
        }
      }
    } catch (err) {
      if (!this.alive(gen)) return;
      const message = err instanceof Error ? err.message : "scheduler failed";
      this.markStopped(message);
    }
  }

  private async prepare(gen: number): Promise<void> {
    const projects = await discoverProjects(this.cfg);
    if (!this.alive(gen)) return;
    for (const project of projects) {
      this.projects.set(project.id, project);
      this.bus.emit({ type: "project", project });
    }

    let ideas: Idea[] = [];
    if (!this.cfg.demo) {
      try {
        ideas = await mineIdeas(join(homedir(), ".claude", "projects"));
      } catch {
        ideas = [];
      }
      if (!this.alive(gen)) return;
      for (const idea of ideas) this.bus.emit({ type: "idea", idea });
    }

    if (!this.alive(gen)) return;
    this.queue = planTasks(projects, ideas)
      .slice()
      .sort((a, b) => b.priority - a.priority);
    for (const task of this.queue) this.bus.emit({ type: "task", task });
  }

  private async tick(gen: number): Promise<boolean> {
    if (!this.alive(gen)) return true;
    this.refreshLimit();
    const tokensUsed = this.tokensForBudget();
    const snapshot = budgetSnapshot({
      cfg: this.cfg,
      tokensUsed,
      burnRatePerHour: this.observeRate(tokensUsed),
      limited: this.limited,
      limitResetsAt: this.limitResetsAt,
    });
    if (snapshot.pctUsed >= this.cfg.stopAtPct) {
      this.bus.emit({ type: "budget", budget: snapshot });
      this.bus.emit({ type: "concurrency", value: 0 });
      this.markStopped("reached stopAtPct");
      return true;
    }
    const concurrency = this.applyPace(snapshot.recommendedConcurrency);
    this.bus.emit({ type: "budget", budget: snapshot });
    this.bus.emit({ type: "concurrency", value: concurrency });
    if (this._state === "burning") await this.pump(concurrency);
    return !this.alive(gen);
  }

  private applyPace(base: number): number {
    if (!(base > 0)) return 0;
    const { min, max } = this.cfg.concurrency;
    if (this.pace === "calm") return min;
    if (this.pace === "boost") return Math.min(base + this.boostExtra, max + 4);
    return base;
  }

  private async pump(concurrency: number): Promise<void> {
    while (this._state === "burning" && this.runningCount < concurrency && this.queue.length > 0) {
      const runner = await this.pickRunner();
      if (this._state !== "burning") return;
      if (!runner) return;
      const task = this.queue[0];
      if (!task) return;
      const project = this.projects.get(task.projectId);
      if (!project) {
        this.queue.shift();
        continue;
      }
      this.queue.shift();
      this.launch(task, runner, project);
    }
  }

  private launch(task: Task, runner: Runner, project: ProjectInfo): void {
    this.runningCount += 1;
    const job = this.execute(task, runner, project);
    this.inflight.add(job);
    void job.finally(() => {
      this.runningCount = Math.max(0, this.runningCount - 1);
      this.inflight.delete(job);
    });
  }

  private candidates(): Runner[] {
    const enabled = this.runners.filter(
      (runner) => this.cfg.lanes[runner.lane] && !this.limitedLanes.has(runner.lane),
    );
    if (this.cfg.demo && this.cfg.lanes.mock) return enabled.filter((runner) => runner.lane === "mock");
    return enabled;
  }

  private async pickRunner(): Promise<Runner | null> {
    for (const runner of this.candidates()) {
      try {
        if (await runner.available()) return runner;
      } catch {
        // unavailable
      }
    }
    return null;
  }

  private async execute(task: Task, runner: Runner, project: ProjectInfo): Promise<void> {
    const runId = newId("run");
    const stopSignal = this.stopController.signal;
    const record: RunRecord = {
      id: runId,
      taskId: task.id,
      projectId: project.id,
      lane: runner.lane,
      status: "queued",
      title: task.title,
      startedAt: nowIso(),
      usage: emptyUsage(),
    };
    let resetsAt: string | undefined;

    try {
      let current = task;
      if (this.cfg.monid.enabled) current = await enrichTask(task, this.cfg.monid);
      if (stopSignal.aborted) {
        this.finish(record, "cancelled", "stopped");
        return;
      }

      const place = await this.workspace(runner, current, project);
      if (stopSignal.aborted) {
        this.finish(record, "cancelled", "stopped");
        return;
      }
      if (!place) {
        this.finish(record, "failed", "refusing to run in the project checkout");
        return;
      }

      record.branch = place.branch;
      record.worktreePath = place.cwd;
      this.emitRun(record);
      record.status = "running";
      this.emitRun(record);

      const timeout = new AbortController();
      const timer = setTimeout(() => timeout.abort(new Error("run timeout")), timeoutMs(this.cfg.runTimeoutMin));
      timer.unref();
      const signal = AbortSignal.any([stopSignal, timeout.signal]);
      try {
        const final = await runner.run(
          current,
          {
            runId,
            project,
            cwd: place.cwd,
            branch: place.branch,
            signal,
            config: this.cfg,
          },
          (event) => {
            if (event.type === "limit" && event.resetsAt) resetsAt = event.resetsAt;
            this.onRunnerEvent(record, event);
          },
        );

        let done = mergeRun(record, final, place);
        if (runner.lane !== "mock") {
          try {
            const stat = await diffstat(place.cwd);
            done = { ...done, ...stat };
          } catch {
            // runner-reported diff counts stand when git diff is unavailable
          }
        }
        this.noteUsage(done.id, done.usage);
        this.emitRun(done);
        record.status = done.status;
        if (done.status === "limited") this.onLimited(runner.lane, done.error, resetsAt);
      } finally {
        clearTimeout(timer);
      }
    } catch (err) {
      if (record.status === "queued" || record.status === "running") {
        const cancelled = stopSignal.aborted;
        this.finish(record, cancelled ? "cancelled" : "failed", err instanceof Error ? err.message : String(err));
      }
    }
  }

  private onRunnerEvent(record: RunRecord, event: RunnerEvent): void {
    this.bus.emit({ type: "runner", event });
    if (event.type === "usage") {
      record.usage = { ...event.usage };
      this.noteUsage(record.id, record.usage);
      this.emitRun(record);
    } else if (event.type === "limit") {
      record.error = event.message;
    } else if (event.type === "done") {
      // Terminal status comes from the resolved RunRecord so a late throw still emits failed.
      if (event.summary) record.summary = event.summary;
      if (event.error) record.error = event.error;
    }
  }

  private async workspace(
    runner: Runner,
    task: Task,
    project: ProjectInfo,
  ): Promise<{ cwd: string; branch: string } | null> {
    if (runner.lane === "mock") {
      const segment = taskSegment(task.id);
      const cwd = join(this.dataDir, "worktrees", "mock", segment);
      await mkdir(cwd, { recursive: true });
      if (await isCheckout(cwd, project.path)) return null;
      return { cwd, branch: `burner/${segment}` };
    }
    const created = await createBurnerWorktree(project.path, task.id, this.dataDir);
    if (await isCheckout(created.path, project.path)) return null;
    return { cwd: created.path, branch: created.branch };
  }

  private finish(record: RunRecord, status: RunRecord["status"], error: string): void {
    record.status = status;
    record.error = error;
    record.endedAt = nowIso();
    this.emitRun(record);
  }

  private onLimited(lane: LaneName, message?: string, resetsAt?: string): void {
    this.limitedLanes.add(lane);
    this.limited = true;
    if (resetsAt) this.limitResetsAt = resetsAt;
    if (this._state !== "burning") return;
    this._state = "limited";
    this.bus.emit({ type: "status", state: "limited", message: message ?? "lane limited" });
  }

  private refreshLimit(): void {
    if (!this.limitResetsAt) return;
    const at = Date.parse(this.limitResetsAt);
    if (!Number.isFinite(at) || at > Date.now()) return;
    this.limited = false;
    this.limitedLanes.clear();
    this.limitResetsAt = undefined;
    if (this._state === "limited") {
      this._state = "burning";
      this.bus.emit({ type: "status", state: "burning", message: "usage limit reset" });
    }
  }

  private markStopped(message: string): void {
    if (this._state === "stopped") return;
    this._state = "stopped";
    this.bus.emit({ type: "status", state: "stopped", message });
  }

  private emitRun(run: RunRecord): void {
    this.bus.emit({ type: "run", run: { ...run, usage: { ...run.usage } } });
  }

  private noteUsage(runId: string, usage: TokenUsage): void {
    const total = totalTokens(usage);
    const prev = this.usageByRun.get(runId) ?? 0;
    if (total >= prev) this.usageByRun.set(runId, total);
  }

  // Callbacks wired to the store already count emitted run usage. max() still
  // paces a callback that stays at 0 (tests) without billing the store twice.
  private tokensForBudget(): number {
    const reported = this.tokensUsed();
    const external = Number.isFinite(reported) ? Math.max(0, reported) : 0;
    let accrued = 0;
    for (const value of this.usageByRun.values()) accrued += value;
    return Math.max(external, accrued);
  }

  private observeRate(tokens: number): number {
    const now = Date.now();
    this.samples.push({ at: now, tokens });
    const horizon = now - RATE_WINDOW_MS;
    while (this.samples.length > 1 && this.samples[0]!.at < horizon) this.samples.shift();
    const first = this.samples[0]!;
    const dtMs = now - first.at;
    if (dtMs < 1000) return 0;
    return Math.max(0, (tokens - first.tokens) / (dtMs / 3_600_000));
  }
}

function timeoutMs(runTimeoutMin: number): number {
  const ms = (Number.isFinite(runTimeoutMin) ? Math.max(0, runTimeoutMin) : 0) * 60_000;
  return Math.min(ms, MAX_TIMEOUT_MS);
}

function taskSegment(taskId: string): string {
  const safe = taskId.replace(/[^a-zA-Z0-9._-]/g, "");
  if (!safe || safe === "." || safe === "..") return "task";
  return safe;
}

function mergeRun(
  record: RunRecord,
  final: RunRecord,
  place: { cwd: string; branch: string },
): RunRecord {
  const finalUsage = final.usage ?? record.usage;
  const usage = totalTokens(finalUsage) >= totalTokens(record.usage) ? finalUsage : record.usage;
  return {
    ...record,
    ...final,
    id: record.id,
    taskId: record.taskId,
    projectId: record.projectId,
    lane: record.lane,
    title: record.title,
    branch: final.branch ?? place.branch,
    worktreePath: final.worktreePath ?? place.cwd,
    status: final.status ?? "failed",
    usage: { ...usage },
    endedAt: final.endedAt ?? nowIso(),
  };
}

async function isCheckout(cwd: string, projectPath: string): Promise<boolean> {
  const left = resolve(cwd);
  const right = resolve(projectPath);
  if (left === right) return true;
  try {
    return (await realpath(left)) === (await realpath(right));
  } catch {
    return false;
  }
}
