// Shared contracts for BurnerAgent. Every module builds against these.
// Only the integrator changes this file — if you need a change, say so in your report.

export type LaneName = "claude" | "agent37" | "orca" | "mock";

export type ProjectInfo = {
  id: string; // stable slug, e.g. "my-app-3f2a"
  name: string; // directory name
  path: string; // absolute path to repo root
  languages: string[]; // e.g. ["typescript", "python"]
  lastCommitAt: string | null; // ISO
  dirty: boolean; // has uncommitted changes
  hasTests: boolean;
  todoCount: number; // TODO/FIXME/HACK occurrences (approx)
  packageManager: "npm" | "pnpm" | "yarn" | "bun" | "pip" | "poetry" | "uv" | "cargo" | "go" | null;
  testCommand: string | null;
  readmeExcerpt: string; // first ~800 chars, "" if none
  score: number; // priority, higher = more worth burning on
};

export type TaskKind =
  | "tests"
  | "todos"
  | "docs"
  | "refactor"
  | "bugs"
  | "types"
  | "security"
  | "perf"
  | "deps"
  | "custom";

export type Task = {
  id: string;
  projectId: string;
  kind: TaskKind;
  title: string; // short, shown in UI
  prompt: string; // full prompt given to the agent
  priority: number; // higher runs first
  estTokens: number; // rough estimate for pacing
  lane?: LaneName; // preferred lane
  context: string[]; // extra context snippets (e.g. from Monid)
  createdAt: string;
};

export type RunStatus = "queued" | "running" | "succeeded" | "failed" | "limited" | "cancelled";

export type TokenUsage = {
  input: number;
  output: number;
  cacheRead: number;
  cacheWrite: number;
  costUsd?: number;
};

export type RunRecord = {
  id: string;
  taskId: string;
  projectId: string;
  lane: LaneName;
  status: RunStatus;
  title: string; // copied from task for UI
  branch?: string; // burner/<task-id>
  worktreePath?: string;
  startedAt?: string;
  endedAt?: string;
  usage: TokenUsage;
  summary?: string; // agent's final message (truncated to ~2k chars)
  filesChanged?: number;
  insertions?: number;
  deletions?: number;
  error?: string;
};

export type RunnerEvent =
  | { type: "log"; runId: string; line: string }
  | { type: "tool"; runId: string; tool: string; detail?: string }
  | { type: "usage"; runId: string; usage: TokenUsage } // cumulative for the run
  | { type: "limit"; runId: string; message: string; resetsAt?: string }
  | { type: "done"; runId: string; status: RunStatus; summary?: string; error?: string };

export type RunContext = {
  runId: string;
  project: ProjectInfo;
  cwd: string; // isolated worktree path — NEVER the user's main checkout
  branch: string;
  signal: AbortSignal;
  config: BurnerConfig;
};

export interface Runner {
  lane: LaneName;
  available(): Promise<boolean>; // CLI installed / API key present
  // Must resolve (never reject) with a final RunRecord; emit events as it goes.
  run(task: Task, ctx: RunContext, emit: (e: RunnerEvent) => void): Promise<RunRecord>;
}

export type BudgetSnapshot = {
  windowStart: string; // ISO, start of current weekly window
  windowEnd: string; // ISO, next reset
  tokensUsed: number; // in window: local Claude logs + our runs
  tokensTarget: number; // configured estimate of weekly allowance
  pctUsed: number; // 0..100
  hoursLeft: number;
  burnRatePerHour: number; // recent observed
  neededRatePerHour: number; // to hit stopAtPct by windowEnd
  recommendedConcurrency: number;
  limited: boolean; // a lane reported a usage limit
  limitResetsAt?: string;
};

export type BurnerState = "idle" | "burning" | "paused" | "limited" | "stopped";

export type IntegrationName = "agent37" | "monid" | "orca";

// A missed/unfinished idea mined from past Claude chat transcripts.
export type Idea = {
  id: string;
  text: string; // one-line idea, secrets redacted
  summary?: string;
  source: string; // transcript file, relative to ~/.claude/projects
  projectPath?: string; // cwd of that session, if known
  sessionAt?: string; // ISO
  score: number; // higher = more promising
  status: "new" | "planned" | "done";
};

export type FeedSource = IntegrationName | "burner" | "claude" | "ideas";

export type BurnerEvent =
  | { type: "project"; project: ProjectInfo }
  | { type: "task"; task: Task }
  | { type: "run"; run: RunRecord } // emitted on every run state change
  | { type: "runner"; event: RunnerEvent }
  | { type: "budget"; budget: BudgetSnapshot }
  | { type: "status"; state: BurnerState; message?: string }
  | { type: "concurrency"; value: number }
  | { type: "integration"; name: IntegrationName; ok: boolean; message: string }
  | { type: "idea"; idea: Idea }
  | { type: "feed"; source: FeedSource; message: string; level?: "info" | "success" | "warn" };

// What GET /api/state returns and what the dashboard renders from.
export type StoreSnapshot = {
  state: BurnerState;
  concurrency: number;
  projects: ProjectInfo[];
  tasks: Task[]; // still queued
  runs: RunRecord[]; // most recent first, capped at 500
  logs: Record<string, string[]>; // runId -> last 40 log lines
  budget: BudgetSnapshot | null;
  integrations: Partial<Record<IntegrationName, { ok: boolean; message: string }>>;
  ideas: Idea[];
  feed: { at: string; source: FeedSource; message: string; level?: "info" | "success" | "warn" }[]; // last 100
  startedAt: string | null;
  demo: boolean;
};

export type ControlAction = "start" | "pause" | "resume" | "stop" | "boost" | "calm";

export type BurnerConfig = {
  roots: string[]; // where to look for projects
  include: string[]; // project-name substrings to include ([] = all)
  exclude: string[]; // project-name substrings to exclude
  maxDepth: number;
  concurrency: { min: number; max: number };
  weeklyTokenTarget: number; // estimated weekly allowance in tokens
  resetDay: number; // 0=Sun..6=Sat, local time
  resetHour: number; // 0..23 local
  stopAtPct: number; // stop burning at this % of target
  runTimeoutMin: number; // hard timeout per run
  lanes: Record<LaneName, boolean>;
  claude: {
    bin: string;
    model?: string;
    maxTurns: number;
    permissionMode: "acceptEdits" | "default" | "plan";
    allowedTools: string[];
    disallowedTools: string[];
    extraArgs: string[];
  };
  agent37: { apiKey?: string; baseUrl: string; template: string; maxInstances: number };
  monid: { apiKey?: string; baseUrl: string; enabled: boolean };
  orca: { enabled: boolean };
  dataDir: string; // state + worktrees live here
  port: number;
  demo: boolean;
};
