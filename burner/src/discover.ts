import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { readdir, readFile, stat } from "node:fs/promises";
import type { Dirent } from "node:fs";
import { basename, extname, join, relative, resolve, sep } from "node:path";
import type { BurnerConfig, ProjectInfo } from "./types.js";
import { SECRET_FILE_RE, slug } from "./util.js";

const DAY_MS = 24 * 60 * 60 * 1000;
const MAX_TODO_FILES = 2000;
const MAX_TEXT_BYTES = 1_000_000;
const LANG_ORDER = ["typescript", "javascript", "python", "rust", "go"] as const;

const EXT_LANG: Record<string, (typeof LANG_ORDER)[number]> = {
  ".ts": "typescript",
  ".tsx": "typescript",
  ".mts": "typescript",
  ".cts": "typescript",
  ".js": "javascript",
  ".jsx": "javascript",
  ".mjs": "javascript",
  ".cjs": "javascript",
  ".py": "python",
  ".pyi": "python",
  ".rs": "rust",
  ".go": "go",
};

const TEXT_EXT = new Set<string>([
  ...Object.keys(EXT_LANG),
  ".java",
  ".kt",
  ".kts",
  ".swift",
  ".rb",
  ".php",
  ".c",
  ".h",
  ".cc",
  ".cpp",
  ".hpp",
  ".cs",
  ".scala",
  ".vue",
  ".svelte",
  ".html",
  ".htm",
  ".css",
  ".scss",
  ".less",
  ".md",
  ".markdown",
  ".rst",
  ".json",
  ".yml",
  ".yaml",
  ".toml",
  ".sh",
  ".bash",
  ".zsh",
  ".sql",
  ".graphql",
  ".gql",
  ".xml",
  ".txt",
  ".ini",
  ".cfg",
  ".conf",
]);

const BINARY_EXT = new Set<string>([
  ".png",
  ".jpg",
  ".jpeg",
  ".gif",
  ".webp",
  ".ico",
  ".bmp",
  ".pdf",
  ".zip",
  ".gz",
  ".tgz",
  ".bz2",
  ".7z",
  ".rar",
  ".wasm",
  ".woff",
  ".woff2",
  ".ttf",
  ".eot",
  ".otf",
  ".mp3",
  ".mp4",
  ".mov",
  ".avi",
  ".webm",
  ".ogg",
  ".exe",
  ".dll",
  ".so",
  ".dylib",
  ".bin",
  ".lockb",
  ".sqlite",
  ".db",
  ".pyc",
  ".class",
  ".o",
  ".a",
  ".jar",
]);

type PackageJson = {
  scripts?: Record<string, unknown>;
  dependencies?: Record<string, unknown>;
  devDependencies?: Record<string, unknown>;
  peerDependencies?: Record<string, unknown>;
  optionalDependencies?: Record<string, unknown>;
};

type PackageManager = ProjectInfo["packageManager"];

const TODO_RE = /\b(?:TODO|FIXME|HACK)\b/g;

function skipDir(name: string): boolean {
  return name.startsWith(".") || name === "node_modules" || name.toLowerCase() === "library";
}

function isInside(parent: string, child: string): boolean {
  const p = resolve(parent);
  const c = resolve(child);
  if (c === p) return true;
  const prefix = p.endsWith(sep) ? p : p + sep;
  return c.startsWith(prefix);
}

function relPosix(root: string, full: string): string {
  const rel = relative(root, full);
  return sep === "/" ? rel : rel.split(sep).join("/");
}

function isSecret(rel: string): boolean {
  return SECRET_FILE_RE.test(rel);
}

function isLockfile(name: string): boolean {
  const n = name.toLowerCase();
  return (
    n.endsWith(".lock") ||
    n.endsWith("-lock.yaml") ||
    n.endsWith("-lock.yml") ||
    n.endsWith("-lock.json") ||
    n === "package-lock.json" ||
    n === "go.sum" ||
    n === "bun.lockb"
  );
}

function hasNamedFile(files: Iterable<string>, name: string): boolean {
  const lower = name.toLowerCase();
  for (const file of files) if (file.toLowerCase() === lower) return true;
  return false;
}

function hasDep(pkg: PackageJson, name: string): boolean {
  return [pkg.dependencies, pkg.devDependencies, pkg.peerDependencies, pkg.optionalDependencies].some(
    (group) => group != null && Object.prototype.hasOwnProperty.call(group, name),
  );
}

/** 100 within the last day, then linear to 0 at 60 days. */
function recencyScore(lastCommitAt: string | null): number {
  if (!lastCommitAt) return 0;
  const t = Date.parse(lastCommitAt);
  if (Number.isNaN(t)) return 0;
  const ageDays = (Date.now() - t) / DAY_MS;
  if (ageDays <= 1) return 100;
  if (ageDays >= 60) return 0;
  return (100 * (60 - ageDays)) / 59;
}

function projectScore(parts: Pick<ProjectInfo, "lastCommitAt" | "todoCount" | "hasTests" | "readmeExcerpt">): number {
  return (
    recencyScore(parts.lastCommitAt) +
    Math.min(parts.todoCount, 30) * 2 +
    (parts.hasTests ? 10 : 0) +
    (parts.readmeExcerpt ? 5 : 0)
  );
}

function projectId(name: string, abs: string): string {
  const hash = createHash("sha1").update(abs).digest("hex").slice(0, 4);
  return `${slug(name)}-${hash}`;
}

function blank(abs: string): ProjectInfo {
  const name = basename(abs);
  return {
    id: projectId(name, abs),
    name,
    path: abs,
    languages: [],
    lastCommitAt: null,
    dirty: false,
    hasTests: false,
    todoCount: 0,
    packageManager: null,
    testCommand: null,
    readmeExcerpt: "",
    score: 0,
  };
}

function nameAllowed(name: string, cfg: BurnerConfig): boolean {
  const n = name.toLowerCase();
  if (cfg.exclude.some((s) => s && n.includes(s.toLowerCase()))) return false;
  const includes = cfg.include.filter((s) => s);
  if (includes.length === 0) return true;
  return includes.some((s) => n.includes(s.toLowerCase()));
}

function git(cwd: string, args: readonly string[]): Promise<string | null> {
  const verb = args.find((a) => a === "log" || a === "status");
  if (!verb) return Promise.resolve(null);
  if (
    args.some((a) =>
      /^(push|checkout|switch|reset|clean|rebase|merge|commit|stash|restore|fetch|pull|remote)$/.test(a),
    )
  ) {
    return Promise.reject(new Error(`refusing git ${args.join(" ")}`));
  }
  return new Promise((resolveGit) => {
    execFile(
      "git",
      args,
      {
        cwd,
        timeout: 20_000,
        maxBuffer: 8 * 1024 * 1024,
        encoding: "utf8",
        env: { ...process.env, GIT_TERMINAL_PROMPT: "0" },
      },
      (err, stdout) => {
        if (err) resolveGit(null);
        else resolveGit(stdout);
      },
    );
  });
}

async function isGitRepo(dir: string): Promise<boolean> {
  try {
    return (await stat(join(dir, ".git"))).isDirectory();
  } catch {
    return false;
  }
}

async function readText(file: string, maxBytes: number): Promise<string | null> {
  try {
    const st = await stat(file);
    if (!st.isFile() || st.size > maxBytes) return null;
    return await readFile(file, "utf8");
  } catch {
    return null;
  }
}

async function readHead(file: string, maxChars: number): Promise<string> {
  try {
    const st = await stat(file);
    if (!st.isFile() || st.size === 0) return "";
    const buf = await readFile(file);
    if (buf.subarray(0, Math.min(buf.length, 8000)).includes(0)) return "";
    return buf.toString("utf8").slice(0, maxChars);
  } catch {
    return "";
  }
}

function parsePackageJson(text: string | null): PackageJson | null {
  if (!text) return null;
  try {
    const value = JSON.parse(text) as unknown;
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    return value as PackageJson;
  } catch {
    return null;
  }
}

function detectPackageManager(files: Set<string>, pyproject: string | null): PackageManager {
  const has = (name: string) => hasNamedFile(files, name);
  if (has("package.json")) {
    if (has("bun.lock") || has("bun.lockb")) return "bun";
    if (has("pnpm-lock.yaml")) return "pnpm";
    if (has("yarn.lock")) return "yarn";
    return "npm";
  }
  if (has("uv.lock")) return "uv";
  if (has("poetry.lock")) return "poetry";
  if (pyproject && /^\s*\[tool\.poetry\]/m.test(pyproject)) return "poetry";
  if (pyproject && /^\s*\[tool\.uv(?:\.|\])/m.test(pyproject)) return "uv";
  if (pyproject || [...files].some((name) => /^requirements.*\.txt$/i.test(name))) return "pip";
  if (has("Cargo.toml")) return "cargo";
  if (has("go.mod")) return "go";
  return null;
}

function countTodos(text: string): number {
  return text.match(TODO_RE)?.length ?? 0;
}

async function scanTodos(file: string): Promise<number | null> {
  try {
    const st = await stat(file);
    if (!st.isFile() || st.size === 0 || st.size > MAX_TEXT_BYTES) return null;
    const buf = await readFile(file);
    if (buf.includes(0)) return null;
    return countTodos(buf.toString("utf8"));
  } catch {
    return null;
  }
}

type ScanState = {
  langs: Set<string>;
  todoCount: number;
  filesRead: number;
  hasTestDir: boolean;
  pyTestFiles: boolean;
  pytestConfig: boolean;
};

async function scanTree(root: string, state: ScanState): Promise<void> {
  const stack: string[] = [root];
  while (stack.length > 0) {
    const dir = stack.pop();
    if (!dir) break;
    let entries: Dirent[];
    try {
      entries = await readdir(dir, { withFileTypes: true });
    } catch {
      continue;
    }
    entries.sort((a, b) => a.name.localeCompare(b.name));
    const subdirs: string[] = [];
    for (const ent of entries) {
      if (ent.isSymbolicLink()) continue;
      const full = join(dir, ent.name);
      if (ent.isDirectory()) {
        if (skipDir(ent.name)) continue;
        const lower = ent.name.toLowerCase();
        if (lower === "test" || lower === "tests") state.hasTestDir = true;
        subdirs.push(full);
        continue;
      }
      if (!ent.isFile()) continue;
      const rel = relPosix(root, full);
      if (isSecret(rel)) continue;
      const ext = extname(ent.name).toLowerCase();
      const lang = EXT_LANG[ext];
      if (lang) state.langs.add(lang);
      const base = ent.name.toLowerCase();
      if (base === "pytest.ini" || base === "conftest.py") state.pytestConfig = true;
      if (/^test_.*\.py$/i.test(ent.name) || /_test\.py$/i.test(ent.name)) state.pyTestFiles = true;
      if (state.filesRead >= MAX_TODO_FILES) continue;
      if (!TEXT_EXT.has(ext) || BINARY_EXT.has(ext) || isLockfile(ent.name)) continue;
      const hits = await scanTodos(full);
      if (hits == null) continue;
      state.filesRead += 1;
      state.todoCount += hits;
    }
    for (let i = subdirs.length - 1; i >= 0; i -= 1) stack.push(subdirs[i]!);
  }
}

function findRootFile(files: Set<string>, pred: (name: string) => boolean): string | null {
  for (const name of files) if (pred(name)) return name;
  return null;
}

export async function inspectProject(path: string): Promise<ProjectInfo> {
  const abs = resolve(path);
  const name = basename(abs);
  let rootNames: string[];
  try {
    const entries = await readdir(abs, { withFileTypes: true });
    rootNames = [];
    for (const ent of entries) if (ent.isFile() && !ent.isSymbolicLink()) rootNames.push(ent.name);
  } catch {
    return blank(abs);
  }
  const rootFiles = new Set(rootNames);

  const pkgName = findRootFile(rootFiles, (n) => n.toLowerCase() === "package.json");
  const pyName = findRootFile(rootFiles, (n) => n.toLowerCase() === "pyproject.toml");
  const readmeName =
    findRootFile(rootFiles, (n) => n === "README.md") ??
    findRootFile(rootFiles, (n) => n.toLowerCase() === "readme.md");
  const tsconfig = hasNamedFile(rootFiles, "tsconfig.json");

  const state: ScanState = {
    langs: new Set(),
    todoCount: 0,
    filesRead: 0,
    hasTestDir: false,
    pyTestFiles: false,
    pytestConfig: false,
  };

  const gitArgs = ["-c", "safe.directory=*", "-c", "color.ui=false"] as const;
  const [pkgText, pyText, readmeExcerpt, logOut, statusOut] = await Promise.all([
    pkgName && !isSecret(pkgName) ? readText(join(abs, pkgName), MAX_TEXT_BYTES) : Promise.resolve(null),
    pyName && !isSecret(pyName) ? readText(join(abs, pyName), MAX_TEXT_BYTES) : Promise.resolve(null),
    readmeName && !isSecret(readmeName) ? readHead(join(abs, readmeName), 800) : Promise.resolve(""),
    git(abs, [...gitArgs, "log", "-1", "--format=%cI"]),
    git(abs, [...gitArgs, "status", "--porcelain"]),
    scanTree(abs, state),
  ]);

  const pkg = parsePackageJson(pkgText);
  const testScript = typeof pkg?.scripts?.test === "string" ? pkg.scripts.test.trim() : "";
  if (pkg && (hasDep(pkg, "typescript") || tsconfig || state.langs.has("typescript"))) state.langs.add("typescript");
  if (pkg && (state.langs.has("javascript") || !state.langs.has("typescript"))) state.langs.add("javascript");
  if (pyText != null || [...rootFiles].some((n) => /^requirements.*\.txt$/i.test(n))) state.langs.add("python");
  if (hasNamedFile(rootFiles, "uv.lock") || hasNamedFile(rootFiles, "poetry.lock")) state.langs.add("python");
  if (hasNamedFile(rootFiles, "Cargo.toml")) state.langs.add("rust");
  if (hasNamedFile(rootFiles, "go.mod")) state.langs.add("go");
  if (pyText && /\[tool\.pytest/i.test(pyText)) state.pytestConfig = true;

  const languages = LANG_ORDER.filter((lang) => state.langs.has(lang));
  const python = languages.includes("python");
  const hasGo = hasNamedFile(rootFiles, "go.mod");
  const hasCargo = hasNamedFile(rootFiles, "Cargo.toml");
  const hasTests = state.hasTestDir || testScript.length > 0;

  let testCommand: string | null = null;
  if (testScript) testCommand = testScript;
  else if (python && (state.hasTestDir || state.pytestConfig || state.pyTestFiles)) testCommand = "pytest";
  else if (hasGo) testCommand = "go test";
  else if (hasCargo) testCommand = "cargo test";

  const commit = logOut?.trim() ?? "";
  const lastCommitAt = commit && !Number.isNaN(Date.parse(commit)) ? commit : null;
  const dirty = Boolean(statusOut && statusOut.trim());
  const todoCount = state.todoCount;
  const info: ProjectInfo = {
    id: projectId(name, abs),
    name,
    path: abs,
    languages,
    lastCommitAt,
    dirty,
    hasTests,
    todoCount,
    packageManager: detectPackageManager(rootFiles, pyText),
    testCommand,
    readmeExcerpt,
    score: 0,
  };
  info.score = projectScore(info);
  return info;
}

async function walkRoot(dir: string, depth: number, cfg: BurnerConfig, dataDir: string, out: string[]): Promise<void> {
  if (isInside(dataDir, dir)) return;
  let isDir = false;
  try {
    isDir = (await stat(dir)).isDirectory();
  } catch {
    return;
  }
  if (!isDir) return;

  if (await isGitRepo(dir)) {
    if (nameAllowed(basename(dir), cfg)) out.push(resolve(dir));
    return;
  }
  if (depth >= cfg.maxDepth) return;

  let entries: Dirent[];
  try {
    entries = await readdir(dir, { withFileTypes: true });
  } catch {
    return;
  }
  entries.sort((a, b) => a.name.localeCompare(b.name));
  for (const ent of entries) {
    if (!ent.isDirectory() || ent.isSymbolicLink()) continue;
    if (skipDir(ent.name)) continue;
    const child = join(dir, ent.name);
    if (isInside(dataDir, child)) continue;
    await walkRoot(child, depth + 1, cfg, dataDir, out);
  }
}

export async function discoverProjects(cfg: BurnerConfig): Promise<ProjectInfo[]> {
  const dataDir = resolve(cfg.dataDir);
  const found: string[] = [];
  const seen = new Set<string>();
  for (const root of cfg.roots) {
    const abs = resolve(root);
    if (seen.has(`root:${abs}`)) continue;
    seen.add(`root:${abs}`);
    try {
      const st = await stat(abs);
      if (!st.isDirectory()) continue;
    } catch {
      continue;
    }
    const before = found.length;
    await walkRoot(abs, 0, cfg, dataDir, found);
    for (let i = before; i < found.length; i += 1) seen.add(found[i]!);
  }

  const unique: string[] = [];
  const paths = new Set<string>();
  for (const dir of found) {
    if (paths.has(dir)) continue;
    paths.add(dir);
    unique.push(dir);
  }

  const projects: ProjectInfo[] = [];
  for (const dir of unique) projects.push(await inspectProject(dir));
  projects.sort((a, b) => b.score - a.score || a.path.localeCompare(b.path));
  return projects;
}
