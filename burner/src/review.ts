// A code review plan from git status and diff stats. Never reads file contents.
import { spawnSync } from "node:child_process";
import { basename } from "node:path";
import type { ReviewFile, ReviewPlan } from "./types.js";
import { SECRET_FILE_RE } from "./util.js";

const FILE_CAP = 12;

function git(repo: string, args: string[]): { ok: boolean; stdout: string } {
  const result = spawnSync("git", args, { cwd: repo, encoding: "utf8" });
  if (result.error || result.status !== 0) return { ok: false, stdout: "" };
  return { ok: true, stdout: result.stdout ?? "" };
}

function secretPath(path: string): boolean {
  const norm = path.replace(/\\/g, "/");
  SECRET_FILE_RE.lastIndex = 0;
  return SECRET_FILE_RE.test(norm);
}

function focusFor(path: string): string {
  if (secretPath(path)) return "Secret file. Do not open it. Confirm it is not part of the change.";
  if (/\.(test|spec)\./i.test(path)) return "Check the assertions fail on the old behavior.";
  if (/\.(ts|tsx|js|jsx|py|go|rs)$/i.test(path)) return "Check behavior, errors, and the public contract.";
  if (/\.md$/i.test(path)) return "Check the note matches the code it describes.";
  if (/(package\.json|Cargo\.toml|go\.mod|pyproject\.toml)$/i.test(path)) return "Check dependency changes are intentional.";
  return "Check the change stays inside this file's job.";
}

function parseNumstat(text: string): ReviewFile[] {
  const files: ReviewFile[] = [];
  for (const line of text.split("\n")) {
    const match = /^(\d+|-)\t(\d+|-)\t(.+)$/.exec(line);
    if (!match) continue;
    const raw = match[3] ?? "";
    const path = raw.includes(" => ") ? (raw.split(" => ").pop() ?? raw).trim() : raw.trim();
    if (!path) continue;
    files.push({
      path,
      additions: match[1] === "-" ? 0 : Number(match[1]),
      deletions: match[2] === "-" ? 0 : Number(match[2]),
      focus: focusFor(path),
    });
  }
  return files;
}

function untracked(status: string, seen: Set<string>): ReviewFile[] {
  const files: ReviewFile[] = [];
  for (const line of status.split("\n")) {
    if (!line.startsWith("?? ")) continue;
    const path = line.slice(3).trim();
    if (!path || seen.has(path)) continue;
    files.push({ path, additions: 0, deletions: 0, focus: focusFor(path) });
  }
  return files;
}

function emptyPlan(repo: string, summary: string): ReviewPlan {
  return { repo, branch: "", scope: "empty", summary, files: [], checks: [] };
}

export function buildReviewPlan(repoPath: string): ReviewPlan {
  const name = basename(repoPath) || repoPath;
  const inside = git(repoPath, ["rev-parse", "--is-inside-work-tree"]);
  if (!inside.ok || inside.stdout.trim() !== "true") return emptyPlan(name, "Not a git repository.");

  const branch = git(repoPath, ["rev-parse", "--abbrev-ref", "HEAD"]).stdout.trim() || "HEAD";
  const status = git(repoPath, ["status", "--porcelain"]).stdout;
  const dirty = status.trim().length > 0;
  const scope = dirty ? "uncommitted" : "latest-commit";
  const numstat = dirty
    ? git(repoPath, ["diff", "--numstat", "HEAD"]).stdout
    : git(repoPath, ["show", "--numstat", "--format=", "HEAD"]).stdout;
  const tracked = parseNumstat(numstat);
  const seen = new Set(tracked.map((file) => file.path));
  const files = [...tracked, ...(dirty ? untracked(status, seen) : [])].slice(0, FILE_CAP);
  if (files.length === 0 && !dirty) {
    return {
      repo: name,
      branch,
      scope: "empty",
      summary: `${name} on ${branch} has nothing to review yet.`,
      files: [],
      checks: [],
    };
  }
  const where = dirty ? "uncommitted changes" : "the latest commit";
  return {
    repo: name,
    branch,
    scope,
    summary: `${files.length} file${files.length === 1 ? "" : "s"} in ${where} on ${branch}.`,
    files,
    checks: [
      "No secrets, tokens, or credential files are in the change.",
      "Each behavior change has a test, or the plan says why not.",
      "The branch stays local. Do not push.",
      ...files.map((file) => `${file.path}: ${file.focus}`),
    ],
  };
}
