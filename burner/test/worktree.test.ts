import { execFile } from "node:child_process";
import { access, mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { createBurnerWorktree, diffstat, removeBurnerWorktree } from "../src/worktree.js";

const dirs: string[] = [];

afterEach(async () => {
  await Promise.all(dirs.splice(0).map((d) => rm(d, { recursive: true, force: true })));
});

function gitEnv(): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ...process.env, LC_ALL: "C", GIT_TERMINAL_PROMPT: "0" };
  delete env.GIT_DIR;
  delete env.GIT_WORK_TREE;
  delete env.GIT_INDEX_FILE;
  return env;
}

function git(cwd: string, args: string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(
      "git",
      args,
      {
        cwd,
        encoding: "utf8",
        env: gitEnv(),
      },
      (err, stdout, stderr) => {
        if (err) reject(new Error(String(stderr || err.message).trim() || err.message));
        else resolve(String(stdout).trim());
      },
    );
  });
}

async function initRepo(): Promise<{ root: string; repo: string; dataDir: string }> {
  const root = await mkdtemp(join(tmpdir(), "burner-wt-"));
  dirs.push(root);
  const repo = join(root, "My Repo");
  const dataDir = join(root, "data");
  await mkdir(repo);
  await git(repo, ["init", "-b", "main"]);
  await writeFile(join(repo, "README.md"), "# hello\n");
  await git(repo, ["add", "README.md"]);
  await git(repo, [
    "-c",
    "user.email=burner-test@example.com",
    "-c",
    "user.name=Burner Test",
    "commit",
    "-m",
    "init",
  ]);
  return { root, repo, dataDir };
}

describe("worktree", () => {
  it("creates a burner worktree from HEAD and removes it without touching the main checkout", async () => {
    const { repo, dataDir } = await initRepo();
    const headBefore = await git(repo, ["rev-parse", "HEAD"]);
    const branchBefore = await git(repo, ["rev-parse", "--abbrev-ref", "HEAD"]);
    expect(branchBefore).toBe("main");

    const created = await createBurnerWorktree(repo, "task_01", dataDir);
    expect(created.branch).toBe("burner/task_01");
    expect(created.path).toBe(join(dataDir, "worktrees", "my-repo", "task_01"));
    expect(await createBurnerWorktree(repo, "task_01", dataDir)).toEqual(created);

    expect(await diffstat(created.path)).toEqual({ filesChanged: 0, insertions: 0, deletions: 0 });
    expect(await git(created.path, ["rev-parse", "--abbrev-ref", "HEAD"])).toBe("burner/task_01");
    expect(await git(created.path, ["rev-parse", "HEAD"])).toBe(headBefore);

    await writeFile(join(created.path, "notes.txt"), "burner\n");
    await git(created.path, ["add", "notes.txt"]);
    expect(await diffstat(created.path)).toEqual({ filesChanged: 1, insertions: 1, deletions: 0 });

    expect(await git(repo, ["rev-parse", "HEAD"])).toBe(headBefore);
    expect(await git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])).toBe(branchBefore);
    expect(await git(repo, ["status", "--porcelain"])).toBe("");
    expect(await readFile(join(repo, "README.md"), "utf8")).toBe("# hello\n");
    await expect(access(join(repo, "notes.txt"))).rejects.toThrow();
    expect(await git(repo, ["remote"])).toBe("");

    await removeBurnerWorktree(repo, created.path);

    expect(await git(repo, ["rev-parse", "HEAD"])).toBe(headBefore);
    expect(await git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])).toBe("main");
    expect(await git(repo, ["branch", "--list", "main"])).toContain("main");
    expect(await git(repo, ["branch", "--list", "burner/task_01"])).toBe("");
    expect(await git(repo, ["worktree", "list"])).not.toContain("task_01");
    await expect(access(created.path)).rejects.toThrow();
    expect(await readFile(join(repo, "README.md"), "utf8")).toBe("# hello\n");
  });

  it("sanitizes task ids and keeps the worktree under dataDir", async () => {
    const { repo, dataDir } = await initRepo();
    const headBefore = await git(repo, ["rev-parse", "HEAD"]);

    const created = await createBurnerWorktree(repo, "Bad id/x!", dataDir);
    expect(created.branch).toBe("burner/Badidx");
    expect(created.path).toBe(join(dataDir, "worktrees", "my-repo", "Badidx"));
    expect(await git(repo, ["rev-parse", "HEAD"])).toBe(headBefore);
    expect(await git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])).toBe("main");

    await expect(createBurnerWorktree(repo, "..", dataDir)).rejects.toThrow(/unsafe/);
    await expect(createBurnerWorktree(repo, "../outside", dataDir)).rejects.toThrow(/unsafe/);
    expect(await git(repo, ["rev-parse", "HEAD"])).toBe(headBefore);
    expect(await git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])).toBe("main");

    await removeBurnerWorktree(repo, created.path);
    expect(await git(repo, ["branch", "--list", "main"])).toContain("main");
    expect(await git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])).toBe("main");
  });

  it("refuses to remove the main checkout", async () => {
    const { repo, dataDir } = await initRepo();
    const headBefore = await git(repo, ["rev-parse", "HEAD"]);
    await expect(removeBurnerWorktree(repo, repo)).rejects.toThrow(/main checkout/);
    expect(await git(repo, ["rev-parse", "HEAD"])).toBe(headBefore);
    expect(await git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])).toBe("main");
    expect(dataDir).toBeTruthy();
  });
});
