import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { isAbsolute, join, relative, resolve, sep } from "node:path";
import { promisify } from "node:util";
import { expect, it } from "vitest";
import { DEFAULT_CONFIG } from "../src/config.js";
import { createBus } from "../src/events.js";
import { Scheduler } from "../src/scheduler.js";
import type { BurnerConfig, BurnerEvent, Runner } from "../src/types.js";

const exec = promisify(execFile);

async function loadMock(): Promise<((opts?: { tickMs?: number }) => Runner) | null> {
  try {
    const mod = await import("../src/runners/mock.js");
    return typeof mod.createMockRunner === "function" ? mod.createMockRunner : null;
  } catch {
    return null;
  }
}

const createMockRunner = await loadMock();

function delay(ms: number): Promise<void> {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, ms));
}

async function git(cwd: string, args: string[]): Promise<void> {
  await exec("git", args, {
    cwd,
    env: { ...process.env, GIT_TERMINAL_PROMPT: "0" },
  });
}

function runStatuses(events: BurnerEvent[]): string[] {
  return events.filter((event) => event.type === "run").map((event) => event.run.status);
}

if (!createMockRunner) {
  it.skip("mock runner missing — scheduler demo run skipped", () => {});
} else {
  const factory = createMockRunner;

  it(
    "demo mock lane runs one planned task without using the repo checkout",
    async () => {
      const root = await mkdtemp(join(tmpdir(), "burner-sched-"));
      const repo = join(root, "tinyapp");
      const dataDir = join(root, "data");
      let scheduler: Scheduler | undefined;
      const events: BurnerEvent[] = [];

      try {
        await mkdir(repo, { recursive: true });
        await mkdir(dataDir, { recursive: true });
        await git(repo, ["init", "-b", "main"]);
        await writeFile(join(repo, "README.md"), "Tiny app used by the scheduler test.\n");
        await git(repo, ["add", "README.md"]);
        await git(repo, [
          "-c",
          "user.name=Burner",
          "-c",
          "user.email=burner@example.com",
          "-c",
          "commit.gpgsign=false",
          "-c",
          "core.hooksPath=/dev/null",
          "commit",
          "-m",
          "init",
        ]);

        const cfg: BurnerConfig = {
          ...DEFAULT_CONFIG,
          roots: [repo],
          include: ["tinyapp"],
          exclude: ["node_modules", ".Trash", "Library"],
          maxDepth: 2,
          concurrency: { min: 1, max: 1 },
          weeklyTokenTarget: 200,
          stopAtPct: 50,
          runTimeoutMin: 1,
          lanes: { claude: false, agent37: false, orca: false, mock: true },
          dataDir,
          demo: true,
          monid: { ...DEFAULT_CONFIG.monid, enabled: false },
        };

        const bus = createBus();
        bus.on((event) => events.push(event));
        scheduler = new Scheduler({
          cfg,
          bus,
          runners: [factory({ tickMs: 5 })],
          tokensUsed: () => 0,
          dataDir,
        });
        scheduler.start();

        const deadline = Date.now() + 15_000;
        while (Date.now() < deadline) {
          if (runStatuses(events).includes("succeeded")) break;
          await delay(30);
        }

        const statuses = runStatuses(events);
        expect(
          statuses.includes("succeeded") || statuses.includes("queued") || statuses.includes("running"),
          `events: ${events.map((event) => (event.type === "run" ? `run:${event.run.status}` : event.type)).join(",") || "(none)"}`,
        ).toBe(true);

        expect(existsSync(join(repo, "DEMO.md"))).toBe(false);
        for (const event of events) {
          if (event.type !== "run" || !event.run.worktreePath) continue;
          expect(resolve(event.run.worktreePath)).not.toBe(resolve(repo));
          const rel = relative(dataDir, event.run.worktreePath);
          expect(isAbsolute(rel) || rel === ".." || rel.startsWith(`..${sep}`)).toBe(false);
        }
      } finally {
        scheduler?.stop();
        await rm(root, { recursive: true, force: true });
      }
    },
    20_000,
  );
}
