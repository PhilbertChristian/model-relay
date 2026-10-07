// Snapshot of burner state. Applies bus events in memory and persists state.json.
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import type { Bus } from "./events.js";
import type {
  BudgetSnapshot,
  BurnerEvent,
  BurnerState,
  Idea,
  IntegrationName,
  ProjectInfo,
  RunnerEvent,
  RunRecord,
  StoreSnapshot,
  Task,
  TokenUsage,
} from "./types.js";
import { nowIso, totalTokens } from "./util.js";

const MAX_RUNS = 500;
const MAX_LOGS = 40;
const MAX_FEED = 100;

const STATES: readonly BurnerState[] = ["idle", "burning", "paused", "limited", "stopped"];
const INTEGRATIONS: readonly IntegrationName[] = ["agent37", "monid", "orca"];
const FEED_LEVELS = new Set(["info", "success", "warn"]);

function initialSnapshot(): StoreSnapshot {
  return {
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
  };
}

function isState(value: unknown): value is BurnerState {
  return typeof value === "string" && (STATES as readonly string[]).includes(value);
}

function isIntegration(value: unknown): value is IntegrationName {
  return typeof value === "string" && (INTEGRATIONS as readonly string[]).includes(value);
}

function isPlain(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function finite(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function isApiKeyField(key: string): boolean {
  return key.replace(/[_-]/g, "").toLowerCase() === "apikey";
}

/** Run usage is cumulative per run id. Count only the increase so state-change repeats are not double-billed. */
function usageTotal(usage: unknown): number {
  if (!isPlain(usage)) return 0;
  return totalTokens({
    input: finite(usage.input),
    output: finite(usage.output),
    cacheRead: finite(usage.cacheRead),
    cacheWrite: finite(usage.cacheWrite),
  });
}

function copyRun(run: RunRecord): RunRecord {
  const usage: TokenUsage =
    run.usage && typeof run.usage === "object"
      ? { ...run.usage }
      : { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 };
  return { ...run, usage };
}

function stripSecrets(value: unknown): unknown {
  if (Array.isArray(value)) return value.map((item) => stripSecrets(item));
  if (!isPlain(value)) return value;
  const out: Record<string, unknown> = {};
  for (const [key, inner] of Object.entries(value)) {
    if (isApiKeyField(key)) continue;
    out[key] = stripSecrets(inner);
  }
  return out;
}

function normalizeLogs(raw: Record<string, unknown>): Record<string, string[]> {
  const logs: Record<string, string[]> = {};
  for (const [id, lines] of Object.entries(raw)) {
    if (!Array.isArray(lines)) continue;
    logs[id] = lines.filter((line): line is string => typeof line === "string").slice(-MAX_LOGS);
  }
  return logs;
}

export class Store {
  private snap: StoreSnapshot = initialSnapshot();
  private tokenCount = 0;
  /** High-water usage total already counted for each run id. */
  private usageByRun = new Map<string, number>();
  private stop: (() => void) | undefined;

  constructor(private readonly dataDir: string) {}

  attach(bus: Bus): void {
    this.stop?.();
    this.stop = bus.on((event) => {
      try {
        this.apply(event);
      } catch {
        // Malformed events are dropped.
      }
    });
  }

  snapshot(): StoreSnapshot {
    const s = this.snap;
    const integrations: StoreSnapshot["integrations"] = {};
    for (const [name, info] of Object.entries(s.integrations)) {
      if (info) integrations[name as IntegrationName] = { ...info };
    }
    return {
      state: s.state,
      concurrency: s.concurrency,
      projects: s.projects.slice(),
      tasks: s.tasks.slice(),
      runs: s.runs.slice(),
      logs: Object.fromEntries(Object.entries(s.logs).map(([id, lines]) => [id, lines.slice()])),
      budget: s.budget ? { ...s.budget } : null,
      integrations,
      ideas: s.ideas.slice(),
      feed: s.feed.map((item) => ({ ...item })),
      startedAt: s.startedAt,
      demo: s.demo,
    };
  }

  setDemo(demo: boolean): void {
    this.snap.demo = demo;
  }

  addTokens(n: number): void {
    if (typeof n !== "number" || !Number.isFinite(n)) return;
    this.tokenCount += n;
  }

  tokensUsed(): number {
    return this.tokenCount;
  }

  async load(): Promise<void> {
    let raw: string;
    try {
      raw = await readFile(this.stateFile(), "utf8");
    } catch {
      return;
    }
    try {
      this.restore(JSON.parse(raw) as unknown);
    } catch {
      // Missing or malformed state.json leaves the current snapshot in place.
    }
  }

  async flush(): Promise<void> {
    const body = stripSecrets({ ...this.snapshot(), tokensUsed: this.tokenCount });
    const json = `${JSON.stringify(body, null, 2)}\n`;
    await mkdir(this.dataDir, { recursive: true });
    await writeFile(this.stateFile(), json, "utf8");
  }

  private stateFile(): string {
    return join(this.dataDir, "state.json");
  }

  private apply(event: BurnerEvent): void {
    if (!event || typeof event !== "object" || typeof event.type !== "string") return;
    switch (event.type) {
      case "project":
        this.upsertProject(event.project);
        break;
      case "task":
        this.enqueueTask(event.task);
        break;
      case "run":
        this.upsertRun(event.run);
        break;
      case "runner":
        this.onRunner(event.event);
        break;
      case "budget":
        this.onBudget(event.budget);
        break;
      case "status":
        if (isState(event.state)) this.snap.state = event.state;
        break;
      case "concurrency":
        if (typeof event.value === "number" && Number.isFinite(event.value)) this.snap.concurrency = event.value;
        break;
      case "integration":
        this.onIntegration(event.name, event.ok, event.message);
        break;
      case "idea":
        this.upsertIdea(event.idea);
        break;
      case "feed":
        this.pushFeed(event);
        break;
      default: {
        const unexpected: never = event;
        void unexpected;
      }
    }
  }

  private upsertProject(project: ProjectInfo): void {
    if (!project || typeof project.id !== "string") return;
    this.snap.projects = replaceById(this.snap.projects, project);
  }

  private enqueueTask(task: Task): void {
    if (!task || typeof task.id !== "string") return;
    if (this.snap.tasks.some((entry) => entry?.id === task.id)) return;
    this.snap.tasks = [...this.snap.tasks, task];
  }

  private upsertRun(run: RunRecord): void {
    if (!run || typeof run !== "object" || typeof run.id !== "string" || !run.id) return;
    this.countRunUsage(run.id, run.usage);
    const rest = this.snap.runs.filter((entry) => entry?.id !== run.id);
    this.snap.runs = [copyRun(run), ...rest].slice(0, MAX_RUNS);
  }

  private countRunUsage(id: string, usage: unknown): void {
    if (!isPlain(usage)) return;
    const total = usageTotal(usage);
    const prev = this.usageByRun.get(id) ?? 0;
    if (total > prev) {
      this.tokenCount += total - prev;
      this.usageByRun.set(id, total);
    }
  }

  private onRunner(event: RunnerEvent | undefined): void {
    if (!event || event.type !== "log") return;
    this.appendLog(event.runId, event.line);
  }

  private appendLog(runId: unknown, line: unknown): void {
    if (typeof runId !== "string" || typeof line !== "string") return;
    const prev = this.snap.logs[runId] ?? [];
    this.snap.logs[runId] = [...prev, line].slice(-MAX_LOGS);
  }

  private onBudget(budget: BudgetSnapshot): void {
    if (isPlain(budget)) this.snap.budget = budget;
  }

  private onIntegration(name: unknown, ok: unknown, message: unknown): void {
    if (!isIntegration(name)) return;
    this.snap.integrations = {
      ...this.snap.integrations,
      [name]: { ok: Boolean(ok), message: typeof message === "string" ? message : "" },
    };
  }

  private upsertIdea(idea: Idea): void {
    if (!idea || typeof idea.id !== "string") return;
    this.snap.ideas = replaceById(this.snap.ideas, idea);
  }

  private pushFeed(event: Extract<BurnerEvent, { type: "feed" }>): void {
    if (typeof event.source !== "string" || typeof event.message !== "string") return;
    const rawAt = "at" in event ? (event as { at?: unknown }).at : undefined;
    const at = typeof rawAt === "string" && rawAt ? rawAt : nowIso();
    const row: StoreSnapshot["feed"][number] = { at, source: event.source, message: event.message };
    if (typeof event.level === "string" && FEED_LEVELS.has(event.level)) row.level = event.level;
    this.snap.feed = [...this.snap.feed, row].slice(-MAX_FEED);
  }

  private restore(data: unknown): void {
    if (!isPlain(data)) return;
    if (isState(data.state)) this.snap.state = data.state;
    if (typeof data.concurrency === "number" && Number.isFinite(data.concurrency)) this.snap.concurrency = data.concurrency;
    if (Array.isArray(data.projects)) this.snap.projects = data.projects as ProjectInfo[];
    if (Array.isArray(data.tasks)) this.snap.tasks = data.tasks as Task[];
    if (Array.isArray(data.runs)) {
      this.snap.runs = (data.runs as RunRecord[]).slice(0, MAX_RUNS);
      this.syncUsageIndex();
    }
    if (isPlain(data.logs)) this.snap.logs = normalizeLogs(data.logs);
    if ("budget" in data) {
      if (data.budget === null) this.snap.budget = null;
      else if (isPlain(data.budget)) this.snap.budget = data.budget as BudgetSnapshot;
    }
    if (isPlain(data.integrations)) this.snap.integrations = normalizeIntegrations(data.integrations);
    if (Array.isArray(data.ideas)) this.snap.ideas = data.ideas as Idea[];
    if (Array.isArray(data.feed)) this.snap.feed = normalizeFeed(data.feed);
    if ("startedAt" in data && (typeof data.startedAt === "string" || data.startedAt === null)) {
      this.snap.startedAt = data.startedAt;
    }
    if (typeof data.demo === "boolean") this.snap.demo = data.demo;
    if (typeof data.tokensUsed === "number" && Number.isFinite(data.tokensUsed)) {
      this.tokenCount = data.tokensUsed;
    } else if (Array.isArray(data.runs)) {
      let sum = 0;
      for (const n of this.usageByRun.values()) sum += n;
      this.tokenCount = sum;
    }
  }

  private syncUsageIndex(): void {
    this.usageByRun = new Map();
    for (const run of this.snap.runs) {
      if (!run || typeof run.id !== "string" || this.usageByRun.has(run.id)) continue;
      if (!isPlain(run.usage)) continue;
      this.usageByRun.set(run.id, usageTotal(run.usage));
    }
  }
}

function replaceById<T extends { id: string }>(items: T[], item: T): T[] {
  const idx = items.findIndex((entry) => entry?.id === item.id);
  if (idx < 0) return [...items, item];
  const next = items.slice();
  next[idx] = item;
  return next;
}

function normalizeIntegrations(raw: Record<string, unknown>): StoreSnapshot["integrations"] {
  const integrations: StoreSnapshot["integrations"] = {};
  for (const name of INTEGRATIONS) {
    const row = raw[name];
    if (!isPlain(row)) continue;
    integrations[name] = { ok: Boolean(row.ok), message: typeof row.message === "string" ? row.message : "" };
  }
  return integrations;
}

function normalizeFeed(raw: unknown[]): StoreSnapshot["feed"] {
  const feed: StoreSnapshot["feed"] = [];
  for (const item of raw.slice(-MAX_FEED)) {
    if (!isPlain(item) || typeof item.source !== "string" || typeof item.message !== "string") continue;
    const row: StoreSnapshot["feed"][number] = {
      at: typeof item.at === "string" && item.at ? item.at : nowIso(),
      source: item.source as StoreSnapshot["feed"][number]["source"],
      message: item.message,
    };
    if (typeof item.level === "string" && FEED_LEVELS.has(item.level)) {
      row.level = item.level as NonNullable<StoreSnapshot["feed"][number]["level"]>;
    }
    feed.push(row);
  }
  return feed;
}
