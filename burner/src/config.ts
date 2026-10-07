import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import type { BurnerConfig } from "./types.js";

export type DeepPartial<T> = { [K in keyof T]?: T[K] extends object ? DeepPartial<T[K]> : T[K] };

const home = homedir();

export const DEFAULT_CONFIG: BurnerConfig = {
  roots: ["Projects", "projects", "code", "Code", "dev", "Developer", "src", "repos", "github", "Desktop", "Documents"].map(
    (d) => join(home, d),
  ),
  include: [],
  exclude: ["node_modules", ".Trash", "Library"],
  maxDepth: 3,
  concurrency: { min: 1, max: 8 },
  weeklyTokenTarget: 150_000_000,
  resetDay: 0,
  resetHour: 23,
  stopAtPct: 99,
  runTimeoutMin: 25,
  lanes: { claude: true, agent37: false, orca: false, mock: false },
  claude: {
    bin: "claude",
    maxTurns: 30,
    permissionMode: "acceptEdits",
    allowedTools: [
      "Read",
      "Edit",
      "Write",
      "Glob",
      "Grep",
      "Bash(npm test:*)",
      "Bash(npm run:*)",
      "Bash(npx vitest:*)",
      "Bash(npx tsc:*)",
      "Bash(pytest:*)",
      "Bash(python -m pytest:*)",
      "Bash(cargo test:*)",
      "Bash(go test:*)",
      "Bash(git status:*)",
      "Bash(git diff:*)",
      "Bash(git add:*)",
      "Bash(git commit:*)",
      "Bash(ls:*)",
    ],
    disallowedTools: ["Bash(git push:*)", "Bash(rm -rf:*)", "Bash(git checkout main:*)", "Bash(git reset --hard:*)"],
    extraArgs: [],
  },
  agent37: { baseUrl: "https://api.agent37.com", template: "agent37-claude-code", maxInstances: 3 },
  monid: { baseUrl: "https://api.monid.ai", enabled: false },
  orca: { enabled: false },
  dataDir: join(home, ".burner"),
  port: 3737,
  demo: false,
};

function deepMerge<T>(base: T, over: DeepPartial<T> | undefined): T {
  if (!over) return base;
  const out: any = Array.isArray(base) ? [...(base as any)] : { ...(base as any) };
  for (const [k, v] of Object.entries(over)) {
    if (v === undefined) continue;
    const b = (base as any)[k];
    out[k] = v && typeof v === "object" && !Array.isArray(v) && b && typeof b === "object" ? deepMerge(b, v as any) : v;
  }
  return out;
}

function fromEnv(env: NodeJS.ProcessEnv): DeepPartial<BurnerConfig> {
  const num = (v?: string) => (v && !Number.isNaN(Number(v)) ? Number(v) : undefined);
  const c: DeepPartial<BurnerConfig> = {};
  if (env.BURNER_ROOTS) c.roots = env.BURNER_ROOTS.split(":").filter(Boolean);
  if (env.BURNER_DATA_DIR) c.dataDir = env.BURNER_DATA_DIR;
  if (num(env.BURNER_PORT)) c.port = num(env.BURNER_PORT);
  if (num(env.BURNER_WEEKLY_TOKENS)) c.weeklyTokenTarget = num(env.BURNER_WEEKLY_TOKENS);
  if (num(env.BURNER_MAX_AGENTS)) c.concurrency = { max: num(env.BURNER_MAX_AGENTS) };
  if (env.BURNER_DEMO === "1") c.demo = true;
  if (env.AGENT37_API_KEY) {
    c.agent37 = { apiKey: env.AGENT37_API_KEY, ...(env.AGENT37_BASE_URL ? { baseUrl: env.AGENT37_BASE_URL } : {}) };
    c.lanes = { ...(c.lanes ?? {}), agent37: true };
  }
  if (env.MONID_API_KEY) {
    c.monid = { apiKey: env.MONID_API_KEY, enabled: true, ...(env.MONID_BASE_URL ? { baseUrl: env.MONID_BASE_URL } : {}) };
  }
  return c;
}

// Precedence: defaults < burner.config.json < .env / process env < explicit overrides.
export function loadConfig(overrides: DeepPartial<BurnerConfig> = {}, cwd = process.cwd()): BurnerConfig {
  const envPath = join(cwd, ".env");
  if (existsSync(envPath)) {
    try {
      process.loadEnvFile(envPath);
    } catch {
      /* malformed .env — ignore, env vars may still be set */
    }
  }
  let fileCfg: DeepPartial<BurnerConfig> = {};
  const cfgPath = join(cwd, "burner.config.json");
  if (existsSync(cfgPath)) fileCfg = JSON.parse(readFileSync(cfgPath, "utf8"));
  return deepMerge(deepMerge(deepMerge(DEFAULT_CONFIG, fileCfg), fromEnv(process.env)), overrides);
}
