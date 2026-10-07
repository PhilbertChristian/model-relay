import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { createServer as createNetServer } from "node:net";
import { homedir, tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { Bus } from "./events.js";
import type { BurnerConfig, Runner } from "./types.js";

const DEMO_PORTS = [3737, 3738] as const;
const DEMO_REPO_NAME = "demo-repo";

type ControlAction = "start" | "pause" | "resume" | "stop" | "boost" | "calm";

type StoreLike = {
  attach(bus: Bus): void;
  snapshot(): unknown;
  setDemo(demo: boolean): void;
  tokensUsed(): number;
  load(): Promise<void>;
  flush(): Promise<void>;
};

type SchedulerLike = {
  start(): void;
  pause(): void;
  resume(): void;
  stop(): void;
  boost(): void;
  calm(): void;
};

type Started = { url: string; close: () => Promise<void> };

// Non-literal specifier so unit tests can import this module without loading the fleet.
function dyn<T>(specifier: string): Promise<T> {
  return import(specifier) as Promise<T>;
}

export function parseArgs(argv: string[]): { cmd: string } {
  return { cmd: argv[2] ?? "" };
}

function runGit(cwd: string, args: string[]): void {
  const result = spawnSync("git", args, { cwd, encoding: "utf8" });
  if (result.status !== 0) {
    const detail = (result.stderr || result.stdout || "").trim();
    throw new Error(detail || `git ${args.join(" ")} failed`);
  }
}

// git init + one commit. user.name is set with git -c on the commit command only.
export async function seedDemoRepo(parent: string): Promise<string> {
  const repo = join(parent, DEMO_REPO_NAME);
  mkdirSync(join(repo, "src"), { recursive: true });
  writeFileSync(join(repo, "README.md"), "# Demo\n\nTiny repo for BurnerAgent.\n");
  writeFileSync(
    join(repo, "package.json"),
    `${JSON.stringify({ name: DEMO_REPO_NAME, private: true }, null, 2)}\n`,
  );
  writeFileSync(
    join(repo, "src", "app.ts"),
    '// TODO: tighten the greeting used by the demo dashboard\nexport const greeting = "hello";\n',
  );
  runGit(repo, ["init", "-b", "main"]);
  runGit(repo, ["add", "README.md", "package.json", "src/app.ts"]);
  runGit(repo, [
    "-c",
    "user.name=BurnerAgent",
    "-c",
    "user.email=demo@burner.local",
    "-c",
    "commit.gpgsign=false",
    "commit",
    "-m",
    "Initial README",
  ]);
  return repo;
}

function packageRoot(): string {
  return dirname(dirname(fileURLToPath(import.meta.url)));
}

function portAvailable(port: number): Promise<boolean> {
  return new Promise((resolvePort) => {
    const probe = createNetServer();
    probe.unref();
    probe.once("error", () => resolvePort(false));
    probe.listen(port, "127.0.0.1", () => {
      probe.close(() => resolvePort(true));
    });
  });
}

async function chooseDemoPort(): Promise<number> {
  for (const port of DEMO_PORTS) {
    if (await portAvailable(port)) return port;
  }
  throw new Error("demo ports 3737 and 3738 are both in use");
}

function isAddrInUse(err: unknown): boolean {
  return Boolean(err && typeof err === "object" && "code" in err && (err as { code?: unknown }).code === "EADDRINUSE");
}

function isMissingPath(err: unknown): boolean {
  if (!err || typeof err !== "object") return false;
  if ("code" in err && (err as { code?: unknown }).code === "ENOENT") return true;
  const message = err instanceof Error ? err.message : "";
  return /ENOENT|no such file or directory/i.test(message);
}

function printHelp(): void {
  console.log(`BurnerAgent

Usage: burner <command>

Commands:
  demo       Local dashboard with a mock lane. No API keys.
  run        Start from config and enable the configured lanes.
  discover   Print discovered projects (name, score, path).
  ideas      Print unfinished ideas from Claude transcripts.
  search     Search conversation transcripts. Usage: burner search <query>
  review     Print a code review plan. Usage: burner review [path]
`);
}

async function runnersFromConfig(cfg: BurnerConfig): Promise<Runner[]> {
  const runners: Runner[] = [];
  if (cfg.lanes.claude) {
    const mod = await dyn<{ createClaudeRunner: () => Runner }>("./runners/claude.js");
    runners.push(mod.createClaudeRunner());
  }
  if (cfg.lanes.agent37) {
    const mod = await dyn<{ createAgent37Runner: () => Runner }>("./runners/agent37.js");
    runners.push(mod.createAgent37Runner());
  }
  if (cfg.lanes.orca) {
    const mod = await dyn<{ createOrcaRunner: () => Runner }>("./runners/orca.js");
    runners.push(mod.createOrcaRunner());
  }
  if (cfg.lanes.mock) {
    const mod = await dyn<{ createMockRunner: (opts?: { tickMs?: number }) => Runner }>("./runners/mock.js");
    runners.push(mod.createMockRunner());
  }
  return runners;
}

async function serve(
  cfg: BurnerConfig,
  runners: Runner[],
  demoBanner: boolean,
  extras: { searchRoot?: string; reviewRoot?: string } = {},
): Promise<void> {
  const [{ Store }, events, schedulerMod, serverMod] = await Promise.all([
    dyn<{ Store: new (dataDir: string) => StoreLike }>("./store.js"),
    dyn<{ createBus: () => Bus }>("./events.js"),
    dyn<{
      Scheduler: new (opts: {
        cfg: BurnerConfig;
        bus: Bus;
        runners: Runner[];
        tokensUsed: () => number;
        dataDir: string;
      }) => SchedulerLike;
    }>("./scheduler.js"),
    dyn<{
      startServer: (opts: {
        port: number;
        getState: () => unknown;
        subscribe: (fn: (event: unknown) => void) => () => void;
        onControl: (action: ControlAction) => void;
        onSearch?: (query: string) => Promise<unknown>;
        onReview?: () => unknown;
        webDir?: string;
      }) => Promise<Started>;
    }>("./server.js"),
  ]);

  const bus = events.createBus();
  const store = new Store(cfg.dataDir);
  await store.load();
  store.setDemo(cfg.demo);
  store.attach(bus);

  const scheduler = new schedulerMod.Scheduler({
    cfg,
    bus,
    runners,
    tokensUsed: () => store.tokensUsed(),
    dataDir: cfg.dataDir,
  });

  const searchRoot = extras.searchRoot;
  const reviewRoot = extras.reviewRoot;
  const server = await serverMod.startServer({
    port: cfg.port,
    webDir: join(packageRoot(), "web"),
    getState: () => store.snapshot(),
    subscribe: (fn) => bus.on(fn),
    onControl: (action) => scheduler[action](),
    onSearch: async (query) => {
      if (!searchRoot) return [];
      const { searchConversations } = await dyn<{
        searchConversations: (root: string, query: string) => Promise<unknown>;
      }>("./ideas.js");
      return searchConversations(searchRoot, query);
    },
    onReview: async () => {
      if (!reviewRoot) return null;
      const { buildReviewPlan } = await dyn<{ buildReviewPlan: (path: string) => unknown }>("./review.js");
      return buildReviewPlan(reviewRoot);
    },
  });

  await store.flush();

  const line = demoBanner ? `BurnerAgent demo → ${server.url}` : `BurnerAgent → ${server.url}`;
  console.log(`\n${line}\n`);
  scheduler.start();

  const shutdown = () => {
    scheduler.stop();
    void server.close().finally(() => process.exit(0));
  };
  process.once("SIGINT", shutdown);
  process.once("SIGTERM", shutdown);
}

async function runDemo(): Promise<void> {
  const parent = mkdtempSync(join(tmpdir(), "burner-demo-"));
  // State is a sibling of the repo. discoverProjects skips paths inside dataDir.
  const dataDir = join(parent, "state");
  mkdirSync(dataDir, { recursive: true });
  const repoPath = await seedDemoRepo(parent);
  const transcripts = join(parent, "transcripts");
  mkdirSync(transcripts, { recursive: true });
  writeFileSync(
    join(transcripts, "demo.jsonl"),
    [
      JSON.stringify({
        type: "user",
        message: { role: "user", content: "Add a conversation search over past chats" },
        cwd: repoPath,
        timestamp: "2026-10-07T12:00:00.000Z",
      }),
      JSON.stringify({
        type: "assistant",
        message: {
          role: "assistant",
          content: [{ type: "text", text: "A code review plan should list each changed file and the check that proves it." }],
        },
        timestamp: "2026-10-07T12:01:00.000Z",
      }),
    ].join("\n") + "\n",
  );
  const port = await chooseDemoPort();

  const { loadConfig } = await dyn<{ loadConfig: (overrides?: object) => BurnerConfig }>("./config.js");
  const { createMockRunner } = await dyn<{ createMockRunner: (opts?: { tickMs?: number }) => Runner }>(
    "./runners/mock.js",
  );

  const cfg = loadConfig({
    demo: true,
    dataDir,
    roots: [parent],
    include: [basename(repoPath)],
    port,
    weeklyTokenTarget: 1_000_000_000,
    concurrency: { min: 1, max: 4 },
    lanes: { claude: false, agent37: false, orca: false, mock: true },
    monid: { enabled: false, apiKey: "" },
    agent37: { apiKey: "" },
    orca: { enabled: false },
  });
  const runners = [createMockRunner({ tickMs: 120 })];
  const extras = { searchRoot: transcripts, reviewRoot: repoPath };
  try {
    await serve(cfg, runners, true, extras);
  } catch (err) {
    if (port === 3737 && isAddrInUse(err)) {
      await serve({ ...cfg, port: 3738 }, runners, true, extras);
      return;
    }
    throw err;
  }
}

async function runLive(): Promise<void> {
  const { loadConfig } = await dyn<{ loadConfig: () => BurnerConfig }>("./config.js");
  const cfg = loadConfig();
  const runners = await runnersFromConfig(cfg);
  await serve(cfg, runners, false, {
    searchRoot: join(homedir(), ".claude", "projects"),
    reviewRoot: process.cwd(),
  });
}

async function runDiscover(): Promise<void> {
  const { loadConfig } = await dyn<{ loadConfig: () => BurnerConfig }>("./config.js");
  const { discoverProjects } = await dyn<{
    discoverProjects: (cfg: BurnerConfig) => Promise<{ name: string; score: number; path: string }[]>;
  }>("./discover.js");
  const projects = await discoverProjects(loadConfig());
  for (const project of projects) console.log(`${project.name}\t${project.score}\t${project.path}`);
}

async function runIdeas(): Promise<void> {
  const root = join(homedir(), ".claude", "projects");
  try {
    const { mineIdeas } = await dyn<{
      mineIdeas: (root: string, limit?: number) => Promise<{ text: string }[]>;
    }>("./ideas.js");
    const ideas = await mineIdeas(root);
    if (ideas.length === 0) {
      console.log("no transcripts");
      return;
    }
    for (const idea of ideas) console.log(idea.text);
  } catch (err) {
    if (isMissingPath(err)) {
      console.log("no transcripts");
      return;
    }
    throw err;
  }
}

async function runSearch(query: string): Promise<void> {
  if (query.length < 2) {
    console.error("usage: burner search <query>");
    process.exitCode = 1;
    return;
  }
  const root = join(homedir(), ".claude", "projects");
  const { searchConversations } = await dyn<{
    searchConversations: (root: string, query: string) => Promise<{ role: string; text: string; source: string }[]>;
  }>("./ideas.js");
  const hits = await searchConversations(root, query);
  if (hits.length === 0) {
    console.log("no matches");
    return;
  }
  for (const hit of hits) console.log(`${hit.role}\t${hit.source}\t${hit.text}`);
}

async function runReview(repo: string): Promise<void> {
  const { buildReviewPlan } = await dyn<{
    buildReviewPlan: (path: string) => {
      summary: string;
      checks: string[];
    };
  }>("./review.js");
  const plan = buildReviewPlan(repo);
  console.log(plan.summary);
  for (const check of plan.checks) console.log(`- ${check}`);
}

async function main(argv: string[]): Promise<void> {
  const { cmd } = parseArgs(argv);
  switch (cmd) {
    case "":
      printHelp();
      return;
    case "demo":
      await runDemo();
      return;
    case "run":
      await runLive();
      return;
    case "discover":
      await runDiscover();
      return;
    case "ideas":
      await runIdeas();
      return;
    case "search":
      await runSearch(argv.slice(3).join(" ").trim());
      return;
    case "review":
      await runReview(argv[3] ? resolve(argv[3]) : process.cwd());
      return;
    default:
      printHelp();
      process.exitCode = 1;
  }
}

function invokedAsCli(): boolean {
  const entry = process.argv[1];
  if (!entry) return false;
  return resolve(entry) === fileURLToPath(import.meta.url);
}

if (invokedAsCli()) {
  main(process.argv).catch((err: unknown) => {
    const message = err instanceof Error ? err.message : String(err);
    console.error(message);
    process.exit(1);
  });
}
