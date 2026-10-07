import { execFile } from "node:child_process";
import { access, mkdir, realpath } from "node:fs/promises";
import { basename, dirname, isAbsolute, relative, resolve, sep } from "node:path";
import { slug } from "./util.js";

// Burner worktrees only. Never checkout, switch, stash, reset, or commit in the
// user's repo, and never push or talk to a remote. Branch refs are created with
// `git worktree add -b` and deleted afterwards only when named burner/*.

const BURNER_BRANCH = /^burner\/[a-zA-Z0-9._-]+$/;

export async function createBurnerWorktree(
  repoPath: string,
  taskId: string,
  dataDir: string,
): Promise<{ path: string; branch: string }> {
  const repo = resolve(repoPath);
  const safeId = sanitizeTaskId(taskId);
  const branch = `burner/${safeId}`;
  const projectSlug = slug(basename(repo));
  if (!projectSlug) throw new Error("repo path has no usable project name");

  const projectRoot = resolve(dataDir, "worktrees", projectSlug);
  const dest = resolve(projectRoot, safeId);
  assertInside(projectRoot, dest);

  if (await pathExists(dest)) {
    await assertRealInside(projectRoot, dest);
    const current = (await git(dest, ["rev-parse", "--abbrev-ref", "HEAD"])).trim();
    if (current !== branch) {
      throw new Error(`worktree path exists on ${current || "an unknown branch"}, expected ${branch}`);
    }
    return { path: dest, branch };
  }

  await mkdir(dirname(dest), { recursive: true });
  await git(repo, ["worktree", "add", "-b", branch, dest, "HEAD"]);

  const current = (await git(dest, ["rev-parse", "--abbrev-ref", "HEAD"])).trim();
  if (current !== branch) throw new Error(`worktree came up on ${current}, expected ${branch}`);
  return { path: dest, branch };
}

export async function diffstat(
  worktreePath: string,
): Promise<{ filesChanged: number; insertions: number; deletions: number }> {
  const out = await git(resolve(worktreePath), ["diff", "--shortstat", "HEAD"]);
  return parseShortstat(out);
}

export async function removeBurnerWorktree(repoPath: string, worktreePath: string): Promise<void> {
  const repo = resolve(repoPath);
  const wt = resolve(worktreePath);
  if (wt === repo) throw new Error("refusing to remove the main checkout");
  if (await samePath(repo, wt)) throw new Error("refusing to remove the main checkout");

  let branch = "";
  try {
    branch = (await git(wt, ["rev-parse", "--abbrev-ref", "HEAD"])).trim();
  } catch {
    branch = (await branchForWorktree(repo, wt)) ?? "";
  }

  await git(repo, ["worktree", "remove", "--force", wt]);

  // Delete only burner/<id>. main and master never match isBurnerBranch.
  if (isBurnerBranch(branch)) await git(repo, ["branch", "-D", branch]);
}

function sanitizeTaskId(taskId: string): string {
  const safe = taskId.replace(/[^a-zA-Z0-9._-]/g, "");
  if (!safe || safe === "." || safe === ".." || safe.includes("..")) {
    throw new Error("taskId sanitizes to an empty or unsafe path segment");
  }
  return safe;
}

function isBurnerBranch(name: string): boolean {
  return name !== "main" && name !== "master" && BURNER_BRANCH.test(name);
}

function assertInside(parent: string, child: string) {
  const rel = relative(resolve(parent), resolve(child));
  if (!rel || rel === ".." || rel.startsWith(`..${sep}`) || isAbsolute(rel)) {
    throw new Error("refusing worktree outside data directory");
  }
}

async function assertRealInside(parent: string, child: string) {
  const base = await realpath(parent).catch(() => resolve(parent));
  const target = await realpath(child);
  assertInside(base, target);
}

async function samePath(a: string, b: string): Promise<boolean> {
  try {
    return (await realpath(a)) === (await realpath(b));
  } catch {
    return false;
  }
}

async function pathExists(p: string): Promise<boolean> {
  try {
    await access(p);
    return true;
  } catch {
    return false;
  }
}

function parseShortstat(output: string): { filesChanged: number; insertions: number; deletions: number } {
  const text = output.trim();
  if (!text) return { filesChanged: 0, insertions: 0, deletions: 0 };
  const num = (re: RegExp) => {
    const m = re.exec(text);
    return m ? Number(m[1]) : 0;
  };
  return {
    filesChanged: num(/(\d+)\s+files?\s+changed/),
    insertions: num(/(\d+)\s+insertions?\(\+\)/),
    deletions: num(/(\d+)\s+deletions?\(-\)/),
  };
}

async function branchForWorktree(repo: string, worktreePath: string): Promise<string | undefined> {
  let porcelain = "";
  try {
    porcelain = await git(repo, ["worktree", "list", "--porcelain"]);
  } catch {
    return undefined;
  }
  const want = await realpath(worktreePath).catch(() => resolve(worktreePath));
  for (const block of porcelain.split(/\n\n+/)) {
    let wt = "";
    let branch = "";
    for (const line of block.split("\n")) {
      if (line.startsWith("worktree ")) wt = line.slice("worktree ".length).trim();
      else if (line.startsWith("branch ")) branch = line.slice("branch ".length).trim().replace(/^refs\/heads\//, "");
    }
    if (!wt) continue;
    const got = await realpath(wt).catch(() => resolve(wt));
    if (got === want) return branch;
  }
  return undefined;
}

function gitEnv(): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ...process.env, LC_ALL: "C", LANG: "C", GIT_TERMINAL_PROMPT: "0" };
  delete env.GIT_DIR;
  delete env.GIT_WORK_TREE;
  delete env.GIT_INDEX_FILE;
  delete env.GIT_PREFIX;
  return env;
}

// Explicit argv allowlist. Anything that would move the main checkout or touch a remote is refused.
function assertAllowed(args: string[]) {
  const [cmd, sub, ...rest] = args;
  const banned = new Set([
    "push",
    "pull",
    "fetch",
    "remote",
    "clone",
    "ls-remote",
    "checkout",
    "switch",
    "stash",
    "reset",
    "commit",
    "merge",
    "rebase",
    "clean",
    "restore",
    "submodule",
  ]);
  if (args.some((a) => banned.has(a))) throw new Error(`refusing git ${args.join(" ")}`);

  const okDiff = cmd === "diff" && sub === "--shortstat" && rest.length === 1 && rest[0] === "HEAD";
  const okRev = cmd === "rev-parse" && sub === "--abbrev-ref" && rest.length === 1 && rest[0] === "HEAD";
  const okList = cmd === "worktree" && sub === "list" && rest.length === 1 && rest[0] === "--porcelain";
  const okRemove = cmd === "worktree" && sub === "remove" && rest[0] === "--force" && rest.length === 2;
  const okAdd =
    cmd === "worktree" &&
    sub === "add" &&
    rest[0] === "-b" &&
    rest.length === 4 &&
    rest[3] === "HEAD" &&
    isBurnerBranch(rest[1] ?? "");
  const okDelete =
    cmd === "branch" && (sub === "-D" || sub === "--delete") && rest.length === 1 && isBurnerBranch(rest[0] ?? "");
  if (okDiff || okRev || okList || okRemove || okAdd || okDelete) return;
  throw new Error(`refusing git ${args.join(" ")}`);
}

function git(cwd: string, args: string[]): Promise<string> {
  assertAllowed(args);
  return new Promise((resolvePromise, reject) => {
    execFile("git", args, { cwd, encoding: "utf8", timeout: 30_000, env: gitEnv() }, (err, stdout, stderr) => {
      if (err) {
        reject(new Error(`git ${args.join(" ")} failed: ${String(stderr || err.message).trim()}`));
        return;
      }
      resolvePromise(String(stdout));
    });
  });
}
