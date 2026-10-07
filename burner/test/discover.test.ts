import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, relative, resolve } from "node:path";
import { promisify } from "node:util";
import { afterEach, describe, expect, it } from "vitest";
import { discoverProjects, inspectProject } from "../src/discover.js";
import type { BurnerConfig, ProjectInfo } from "../src/types.js";
import { slug } from "../src/util.js";

const exec = promisify(execFile);
const DAY_MS = 24 * 60 * 60 * 1000;
const temps: string[] = [];

afterEach(async () => {
  await Promise.all(temps.splice(0).map((dir) => rm(dir, { recursive: true, force: true })));
});

async function tempDir(): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(), "burner-discover-"));
  temps.push(dir);
  return dir;
}

function cfg(partial: Partial<BurnerConfig> & Pick<BurnerConfig, "roots" | "dataDir">): BurnerConfig {
  return {
    roots: partial.roots,
    include: partial.include ?? [],
    exclude: partial.exclude ?? [],
    maxDepth: partial.maxDepth ?? 4,
    concurrency: { min: 1, max: 1 },
    weeklyTokenTarget: 1,
    resetDay: 0,
    resetHour: 0,
    stopAtPct: 99,
    runTimeoutMin: 1,
    lanes: { claude: false, agent37: false, orca: false, mock: false },
    claude: {
      bin: "claude",
      maxTurns: 1,
      permissionMode: "default",
      allowedTools: [],
      disallowedTools: [],
      extraArgs: [],
    },
    agent37: { baseUrl: "http://127.0.0.1", template: "t", maxInstances: 1 },
    monid: { baseUrl: "http://127.0.0.1", enabled: false },
    orca: { enabled: false },
    dataDir: partial.dataDir,
    port: 0,
    demo: false,
  };
}

async function git(dir: string, args: string[], env: NodeJS.ProcessEnv = process.env): Promise<string> {
  const { stdout } = await exec("git", args, { cwd: dir, env });
  return stdout;
}

async function initRepo(dir: string, date?: string): Promise<void> {
  await mkdir(dir, { recursive: true });
  await git(dir, ["init", "-b", "main"]);
  await git(dir, ["config", "user.email", "dev@example.com"]);
  await git(dir, ["config", "user.name", "Burner Test"]);
  const env = date
    ? { ...process.env, GIT_AUTHOR_DATE: date, GIT_COMMITTER_DATE: date }
    : process.env;
  await git(dir, ["add", "-A"], env);
  await git(dir, ["-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "-m", "init"], env);
}

function daysAgo(days: number): string {
  return new Date(Date.now() - days * DAY_MS).toISOString().replace(/\.\d{3}Z$/, "Z");
}

function bonuses(info: Pick<ProjectInfo, "todoCount" | "hasTests" | "readmeExcerpt">): number {
  return Math.min(info.todoCount, 30) * 2 + (info.hasTests ? 10 : 0) + (info.readmeExcerpt ? 5 : 0);
}

function expectedRecency(iso: string | null): number {
  if (!iso) return 0;
  const ageDays = (Date.now() - Date.parse(iso)) / DAY_MS;
  if (ageDays <= 1) return 100;
  if (ageDays >= 60) return 0;
  return (100 * (60 - ageDays)) / 59;
}

describe("inspectProject", () => {
  it("builds an id from the slug and sha1 of the absolute path", async () => {
    const root = await tempDir();
    const dir = join(root, "My App");
    await mkdir(dir, { recursive: true });
    await writeFile(join(dir, "README.md"), "hello");
    await initRepo(dir);

    const info = await inspectProject(relative(process.cwd(), dir));
    const abs = resolve(dir);
    expect(info.path).toBe(abs);
    expect(info.name).toBe("My App");
    expect(info.id).toBe(`${slug("My App")}-${createHash("sha1").update(abs).digest("hex").slice(0, 4)}`);
    expect(info.id).toMatch(/^my-app-[0-9a-f]{4}$/);
  });

  it("reads commit time, cleanliness, readme, tests, and score", async () => {
    const dir = await tempDir();
    await writeFile(join(dir, "README.md"), `${"x".repeat(1000)}`);
    await mkdir(join(dir, "tests"));
    await writeFile(join(dir, "tests", "a.ts"), "export const n = 1;\n");
    await writeFile(join(dir, "main.ts"), "// TODO: one\n// FIXME: two\n// HACK: three\n");
    await writeFile(
      join(dir, "package.json"),
      JSON.stringify({ name: "demo", scripts: { test: "vitest run" }, devDependencies: { typescript: "5.0.0" } }),
    );
    await initRepo(dir);

    const info = await inspectProject(dir);
    const logged = (await git(dir, ["log", "-1", "--format=%cI"])).trim();
    expect(info.lastCommitAt).toBe(logged);
    expect(info.dirty).toBe(false);
    expect(info.readmeExcerpt).toBe("x".repeat(800));
    expect(info.hasTests).toBe(true);
    expect(info.todoCount).toBe(3);
    expect(info.languages).toEqual(["typescript"]);
    expect(info.packageManager).toBe("npm");
    expect(info.testCommand).toBe("vitest run");
    expect(info.score).toBe(100 + bonuses(info));

    await writeFile(join(dir, "extra.ts"), "export {}\n");
    const dirty = await inspectProject(dir);
    expect(dirty.dirty).toBe(true);
    expect(dirty.lastCommitAt).toBe(info.lastCommitAt);
  });

  it("returns null commit metadata when the repo has no commits", async () => {
    const dir = await tempDir();
    await git(dir, ["init", "-b", "main"]);
    const info = await inspectProject(dir);
    expect(info.lastCommitAt).toBeNull();
    expect(info.dirty).toBe(false);
    expect(info.score).toBe(0);
  });

  it("decays recency from 100 inside a day toward 0 after 60 days", async () => {
    const recent = await tempDir();
    await writeFile(join(recent, "README.md"), "now");
    await initRepo(recent);
    const recentInfo = await inspectProject(recent);
    expect(recentInfo.score - bonuses(recentInfo)).toBe(100);

    const mid = await tempDir();
    await writeFile(join(mid, "README.md"), "mid");
    await initRepo(mid, daysAgo(30));
    const midInfo = await inspectProject(mid);
    expect(midInfo.score - bonuses(midInfo)).toBeCloseTo(expectedRecency(midInfo.lastCommitAt), 3);

    const old = await tempDir();
    await writeFile(join(old, "README.md"), "old");
    await initRepo(old, daysAgo(90));
    const oldInfo = await inspectProject(old);
    expect(oldInfo.lastCommitAt).not.toBeNull();
    expect(oldInfo.score - bonuses(oldInfo)).toBe(0);
  });

  it("caps todo weight at 30 and ignores secrets, binaries, and node_modules", async () => {
    const dir = await tempDir();
    const todos = Array.from({ length: 40 }, () => "TODO").join("\n");
    await writeFile(join(dir, "main.ts"), `${todos}\n`);
    await writeFile(join(dir, ".env"), "TODO FIXME HACK TOKEN=secret\n");
    await writeFile(join(dir, "credentials.json"), '{ "TODO": "FIXME" }\n');
    await writeFile(join(dir, "secrets.yml"), "HACK: true\n");
    await writeFile(join(dir, "id_rsa"), "TODO private\n");
    await writeFile(join(dir, "blob.ts"), Buffer.from("TODO\0FIXME"));
    await writeFile(join(dir, "pic.png"), Buffer.from("TODO"));
    await mkdir(join(dir, "node_modules", "leftpad"), { recursive: true });
    await writeFile(join(dir, "node_modules", "leftpad", "index.js"), "TODO\n".repeat(50));
    await mkdir(join(dir, "config"), { recursive: true });
    await writeFile(join(dir, "config", ".env.local"), "TODO=1\n");

    const info = await inspectProject(dir);
    expect(info.todoCount).toBe(40);
    expect(info.score - (info.lastCommitAt ? 100 : 0) - (info.readmeExcerpt ? 5 : 0) - (info.hasTests ? 10 : 0)).toBe(60);
  });

  it("stops counting TODOs after 2000 text files", async () => {
    const dir = await tempDir();
    const src = join(dir, "src");
    await mkdir(src);
    const batch = 100;
    for (let start = 0; start < 2000; start += batch) {
      await Promise.all(
        Array.from({ length: batch }, (_, offset) => {
          const i = start + offset;
          return writeFile(join(src, `f${String(i).padStart(4, "0")}.ts`), "TODO\n");
        }),
      );
    }
    await writeFile(join(src, "zzz.ts"), "FIXME\n");
    const info = await inspectProject(dir);
    expect(info.todoCount).toBe(2000);
  }, 20_000);

  it("detects languages, package managers, and test commands", async () => {
    const js = await tempDir();
    await writeFile(join(js, "package.json"), JSON.stringify({ name: "js" }));
    await writeFile(join(js, "index.js"), "console.log(1)\n");
    await writeFile(join(js, "yarn.lock"), "# yarn\n");
    const jsInfo = await inspectProject(js);
    expect(jsInfo.languages).toEqual(["javascript"]);
    expect(jsInfo.packageManager).toBe("yarn");
    expect(jsInfo.testCommand).toBeNull();
    expect(jsInfo.hasTests).toBe(false);

    const both = await tempDir();
    await writeFile(join(both, "package.json"), JSON.stringify({ name: "both" }));
    await writeFile(join(both, "a.ts"), "export {}\n");
    await writeFile(join(both, "b.js"), "module.exports = {}\n");
    await writeFile(join(both, "pnpm-lock.yaml"), "lockfileVersion: 9\n");
    await mkdir(join(both, "test"));
    const bothInfo = await inspectProject(both);
    expect(bothInfo.languages).toEqual(["typescript", "javascript"]);
    expect(bothInfo.packageManager).toBe("pnpm");
    expect(bothInfo.hasTests).toBe(true);
    expect(bothInfo.testCommand).toBeNull();

    const bun = await tempDir();
    await writeFile(join(bun, "package.json"), JSON.stringify({ name: "bun-app", scripts: { test: "bun test" } }));
    await writeFile(join(bun, "bun.lock"), "{}\n");
    const bunInfo = await inspectProject(bun);
    expect(bunInfo.packageManager).toBe("bun");
    expect(bunInfo.testCommand).toBe("bun test");
    expect(bunInfo.languages).toEqual(["javascript"]);

    const py = await tempDir();
    await writeFile(join(py, "requirements.txt"), "pytest\n");
    await writeFile(join(py, "app.py"), "print('hi')\n");
    await mkdir(join(py, "tests"));
    const pyInfo = await inspectProject(py);
    expect(pyInfo.languages).toEqual(["python"]);
    expect(pyInfo.packageManager).toBe("pip");
    expect(pyInfo.hasTests).toBe(true);
    expect(pyInfo.testCommand).toBe("pytest");

    const poetry = await tempDir();
    await writeFile(join(poetry, "pyproject.toml"), "[tool.poetry]\nname = \"demo\"\n");
    await writeFile(join(poetry, "poetry.lock"), "# poetry\n");
    const poetryInfo = await inspectProject(poetry);
    expect(poetryInfo.languages).toEqual(["python"]);
    expect(poetryInfo.packageManager).toBe("poetry");
    expect(poetryInfo.testCommand).toBeNull();

    const uv = await tempDir();
    await writeFile(join(uv, "pyproject.toml"), "[project]\nname = \"demo\"\n");
    await writeFile(join(uv, "uv.lock"), "# uv\n");
    await writeFile(join(uv, "test_app.py"), "def test_ok():\n    assert True\n");
    const uvInfo = await inspectProject(uv);
    expect(uvInfo.languages).toEqual(["python"]);
    expect(uvInfo.packageManager).toBe("uv");
    expect(uvInfo.testCommand).toBe("pytest");

    const rust = await tempDir();
    await writeFile(join(rust, "Cargo.toml"), "[package]\nname = \"demo\"\nversion = \"0.1.0\"\n");
    await writeFile(join(rust, "src.rs"), "fn main() {}\n");
    const rustInfo = await inspectProject(rust);
    expect(rustInfo.languages).toEqual(["rust"]);
    expect(rustInfo.packageManager).toBe("cargo");
    expect(rustInfo.testCommand).toBe("cargo test");

    const go = await tempDir();
    await writeFile(join(go, "go.mod"), "module example.com/demo\n\ngo 1.22\n");
    await writeFile(join(go, "main.go"), "package main\n");
    const goInfo = await inspectProject(go);
    expect(goInfo.languages).toEqual(["go"]);
    expect(goInfo.packageManager).toBe("go");
    expect(goInfo.testCommand).toBe("go test");
  });
});

describe("discoverProjects", () => {
  it("skips missing roots, hidden dirs, Library, node_modules, .git internals, and dataDir", async () => {
    const root = await tempDir();
    const dataDir = join(root, "state");
    const ok = join(root, "ok");
    const hidden = join(root, ".secret", "hidden-repo");
    const library = join(root, "Library", "photos");
    const nestedModules = join(root, "node_modules", "leftpad");
    const insideData = join(dataDir, "private-repo");
    const deepGit = join(ok, ".git", "nested");

    for (const dir of [ok, hidden, library, nestedModules, insideData]) {
      await mkdir(dir, { recursive: true });
      await writeFile(join(dir, "README.md"), dir);
      await initRepo(dir);
    }
    await mkdir(join(deepGit, ".git"), { recursive: true });
    await writeFile(join(root, "not-a-dir"), "file");

    const projects = await discoverProjects(
      cfg({
        roots: [join(root, "does-not-exist"), root, join(root, "not-a-dir")],
        dataDir,
        maxDepth: 4,
      }),
    );

    expect(projects.map((p) => p.path)).toEqual([ok]);
    expect(projects.every((p) => !p.path.includes(`${join(".git", "")}`))).toBe(true);
  });

  it("applies maxDepth, include, and exclude on the directory name", async () => {
    const root = await tempDir();
    const shallow = join(root, "Alpha");
    const mid = join(root, "level1", "level2", "alphabet");
    const tooDeep = join(root, "a", "b", "c", "d", "deep-app");
    const beta = join(root, "skip-wrapper", "beta");
    for (const dir of [shallow, mid, tooDeep, beta]) {
      await mkdir(dir, { recursive: true });
      await writeFile(join(dir, "README.md"), "r");
      await initRepo(dir);
    }

    const depth = await discoverProjects(cfg({ roots: [root], dataDir: join(root, "data"), maxDepth: 3 }));
    expect(depth.map((p) => p.name).sort()).toEqual(["Alpha", "alphabet", "beta"]);

    const filtered = await discoverProjects(
      cfg({
        roots: [root],
        dataDir: join(root, "data"),
        maxDepth: 5,
        include: ["LPH"],
        exclude: ["beta"],
      }),
    );
    expect(filtered.map((p) => p.name).sort()).toEqual(["Alpha", "alphabet"]);

    const excludedPath = await discoverProjects(
      cfg({
        roots: [root],
        dataDir: join(root, "data"),
        maxDepth: 5,
        exclude: ["skip"],
      }),
    );
    expect(excludedPath.map((p) => p.name)).toContain("beta");
  });

  it("stops at a repo root and sorts by score descending", async () => {
    const root = await tempDir();
    const fresh = join(root, "fresh");
    const stale = join(root, "stale");
    const inner = join(fresh, "packages", "inner");
    await mkdir(fresh, { recursive: true });
    await writeFile(join(fresh, "README.md"), "fresh readme");
    await mkdir(join(fresh, "tests"));
    await writeFile(join(fresh, "main.ts"), Array.from({ length: 40 }, () => "TODO").join("\n"));
    await writeFile(join(fresh, "package.json"), JSON.stringify({ scripts: { test: "vitest" } }));
    await initRepo(fresh);

    await mkdir(inner, { recursive: true });
    await writeFile(join(inner, "README.md"), "inner");
    await initRepo(inner);

    await mkdir(stale, { recursive: true });
    await writeFile(join(stale, "README.md"), "stale");
    await initRepo(stale, daysAgo(90));

    const projects = await discoverProjects(cfg({ roots: [root], dataDir: join(root, "data"), maxDepth: 5 }));
    expect(projects.map((p) => p.name)).toEqual(["fresh", "stale"]);
    expect(projects[0]!.score).toBeGreaterThan(projects[1]!.score);
    expect(projects.map((p) => p.score)).toEqual([...projects.map((p) => p.score)].sort((a, b) => b - a));
    expect(projects[0]!.score).toBe(100 + 60 + 10 + 5);
    expect(projects[1]!.score).toBe(5);
  });

  it("finds a root that is itself a repo when maxDepth is 0, and not its children", async () => {
    const root = await tempDir();
    await writeFile(join(root, "README.md"), "root");
    await initRepo(root);
    const child = join(root, "child");
    await mkdir(child, { recursive: true });
    await writeFile(join(child, "README.md"), "child");
    await initRepo(child);

    const projects = await discoverProjects(cfg({ roots: [root], dataDir: join(tmpdir(), "burner-unused-data"), maxDepth: 0 }));
    expect(projects.map((p) => p.path)).toEqual([resolve(root)]);
  });
});
