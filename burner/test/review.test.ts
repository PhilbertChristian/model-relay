import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { afterEach, describe, expect, it } from "vitest";
import { buildReviewPlan } from "../src/review.js";

function git(repo: string, args: string[]) {
  const result = spawnSync("git", args, { cwd: repo, encoding: "utf8" });
  if (result.status !== 0) throw new Error(result.stderr || result.stdout);
}

describe("buildReviewPlan", () => {
  const dirs: string[] = [];

  afterEach(async () => {
    await Promise.all(dirs.splice(0).map((dir) => rm(dir, { recursive: true, force: true })));
  });

  it("plans the latest commit without reading file contents", async () => {
    const repo = await mkdtemp(join(tmpdir(), "burner-review-"));
    dirs.push(repo);
    git(repo, ["init", "-b", "main"]);
    await writeFile(join(repo, "app.ts"), "export const greeting = 'hello';\n");
    git(repo, ["add", "app.ts"]);
    git(repo, ["-c", "user.name=Burner", "-c", "user.email=demo@burner.local", "-c", "commit.gpgsign=false", "commit", "-m", "add greeting"]);

    const plan = buildReviewPlan(repo);
    expect(plan.scope).toBe("latest-commit");
    expect(plan.branch).toBe("main");
    expect(plan.files.map((file) => file.path)).toEqual(["app.ts"]);
    expect(plan.checks.some((check) => check.startsWith("app.ts:"))).toBe(true);
    expect(plan.checks.some((check) => check.includes("Do not push"))).toBe(true);
    expect(JSON.stringify(plan)).not.toContain("hello");
  });

  it("lists uncommitted files and refuses to open a secret file", async () => {
    const repo = await mkdtemp(join(tmpdir(), "burner-review-"));
    dirs.push(repo);
    const secret = "super-secret-token";
    git(repo, ["init", "-b", "main"]);
    await writeFile(join(repo, "README.md"), "# Demo\n");
    git(repo, ["add", "README.md"]);
    git(repo, ["-c", "user.name=Burner", "-c", "user.email=demo@burner.local", "-c", "commit.gpgsign=false", "commit", "-m", "readme"]);
    await writeFile(join(repo, ".env"), `API_KEY=${secret}\n`);
    await writeFile(join(repo, "app.ts"), "export const n = 1;\n");

    const plan = buildReviewPlan(repo);
    expect(plan.scope).toBe("uncommitted");
    const env = plan.files.find((file) => file.path === ".env");
    expect(env?.focus).toContain("Do not open");
    expect(JSON.stringify(plan)).not.toContain(secret);
    expect(plan.files.some((file) => file.path === "app.ts")).toBe(true);
  });

  it("reports a path that is not a repository", async () => {
    const dir = await mkdtemp(join(tmpdir(), "burner-review-"));
    dirs.push(dir);
    const plan = buildReviewPlan(dir);
    expect(plan.scope).toBe("empty");
    expect(plan.summary).toBe("Not a git repository.");
  });
});
