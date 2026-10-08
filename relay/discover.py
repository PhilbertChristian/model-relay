"""Find your repos and the work already written down in them, for burn-week.

    discover_projects(roots) -> [ProjectInfo] best first: recent commits, TODOs, a test command, work in progress
    inspect_project(repo)    -> git facts, languages, test command, harvested TODOs and a score for one repo
    harvest_todos(repo)      -> TODO/FIXME/XXX comments and unchecked `- [ ]` plan items, phrased as tasks
    generic_tasks(project)   -> safe, reviewable improvements for repos with little written down
    to_plan_md(projects)     -> a Relay plan (docs/Relay-Plan-Format.md) to review before burning it
    report(projects)         -> compact colored table

Read-only: it lists directories, reads small text files and runs only read-only git (log, status,
branch --show-current, config --get-regexp). It never opens a file that contracts.is_secret_file matches,
and every task string goes through contracts.redact().
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from fnmatch import fnmatch
from urllib.parse import urlsplit

from . import ui
from .contracts import Idea, ProjectInfo, is_secret_file, redact

DATA_DIR = "~/.relay-burn"               # burn-week's own worktrees live here: never a project
MAX_TASK = 160
MAX_FILE_BYTES = 512_000                 # bigger "text" files are generated, vendored or data
MAX_FILES = 4000                         # files listed per repo, shallowest first
WALK_SKIP = {"node_modules", "bower_components", "vendor", "vendors", "third_party", "third-party", "build", "dist",
             "out", "target", "site-packages", "__pycache__", "venv", "virtualenv", "Pods", "Carthage", "DerivedData",
             "coverage", "htmlcov"}
SCAN_SKIP = WALK_SKIP | {"fixtures", "testdata", "test-data", "__snapshots__", "snapshots", "examples", "samples"}
HOME_SKIP = {"Library", "Applications", "Movies", "Music", "Pictures"}   # ~/Library & co. hold no repos worth a walk

EXT_LANG = {".py": "python", ".pyi": "python", ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript",
            ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".rs": "rust",
            ".go": "go", ".rb": "ruby", ".java": "java", ".kt": "kotlin", ".swift": "swift", ".c": "c", ".h": "c",
            ".cc": "c++", ".cpp": "c++", ".hpp": "c++", ".cs": "c#", ".php": "php", ".scala": "scala", ".ex": "elixir",
            ".exs": "elixir", ".hs": "haskell", ".lua": "lua", ".dart": "dart", ".zig": "zig", ".clj": "clojure",
            ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".vue": "vue", ".svelte": "svelte"}
MANIFEST_LANG = {"package.json": "javascript", "tsconfig.json": "typescript", "pyproject.toml": "python",
                 "setup.py": "python", "requirements.txt": "python", "Cargo.toml": "rust", "go.mod": "go",
                 "Gemfile": "ruby", "pom.xml": "java", "build.gradle": "java", "build.gradle.kts": "kotlin",
                 "Package.swift": "swift", "mix.exs": "elixir", "composer.json": "php", "pubspec.yaml": "dart"}
MD_EXT = {".md", ".markdown", ".mdx"}
PROSE_EXT = MD_EXT | {".txt", ".rst", ".org"}
TEXT_EXT = set(EXT_LANG) | PROSE_EXT | {".toml", ".yml", ".yaml", ".cfg", ".ini", ".html", ".css", ".scss", ".sql",
                                         ".graphql", ".proto", ".tf", ".gradle", ".cmake", ".el", ".vim"}
TEXT_NAMES = {"Makefile", "Dockerfile", "Justfile", "Rakefile", "Gemfile", "Procfile"}
GENERATED = (".min.js", ".min.css", ".map", ".d.ts", ".pb.go", "_pb2.py", ".lock", "-lock.json", "-lock.yaml")
PLAN_NAMES = {"plan", "todo", "todos", "roadmap", "tasks", "backlog", "next", "ideas", "notes"}
NOT_PLANS = re.compile(r"^(changelog|changes|history|contributing|code_of_conduct|security|license|licence|authors|burn|"
                       r"morning)\b|template", re.I)
SKIP_MODULES = {"__init__", "__main__", "setup", "conftest", "manage", "wsgi", "asgi", "settings", "version", "_version",
                "constants", "types"}
VERBS = frozenset("""add adjust allow apply audit avoid build bump cache call change check clean cleanup clear close collapse
combine configure connect consider convert copy cover create debounce debug dedupe delete deprecate describe detect disable
document drop emit enable ensure escape expand explain explore export expose extend extract fetch figure filter find finish fix
flatten format generate get guard handle harden hide hook implement import improve include inline install integrate introduce
investigate keep limit lint load log look make merge migrate mock move normalize open optimize parse patch persist pin plan
polish port prevent print profile protect prune publish read record redact reduce refactor refresh register reject release
remove rename render reorder replace report reset resolve restore retry return reuse revert review revisit rewrite rotate run
sanitize save scan schedule send separate serialize set setup ship show simplify skip sort speed split start stop store stream
strip support switch sync tag test throttle tidy track trim truncate try tune unify update upgrade use validate verify warn
watch wire wrap write""".split())

TEST_RE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*$|_(test|spec)\.\w+$|\.(test|spec)\.\w+$|Tests?\.\w+$")
TODO_RE = re.compile(r"(#+|//+|/\*+|<!--|--|;+|%+|^\s*\*+)\s*@?\b(TODO|FIXME|XXX)\b(?:\([^)]*\))?\s*[:\-–—]?\s*(.*)")
MD_TODO_RE = re.compile(r"^\s*(?:[-*+]\s+)?(?:\*\*)?(TODO|FIXME)(?:\*\*)?\s*[:\-–—]\s*(.+)")
CHECKBOX_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\[ \]\s+(.+)")
SETUP_TESTS = {
    "python": "Set up a unittest suite (tests/test_*.py, standard library only) with a smoke test for the main module",
    "javascript": "Add a node:test smoke test for the main module and a package.json `test` script so `npm test` runs it",
    "typescript": "Add a node:test smoke test for the main module and a package.json `test` script so `npm test` runs it",
    "rust": "Add #[cfg(test)] unit tests for the core functions so `cargo test` checks more than compilation",
    "go": "Add table-driven tests for the core package so `go test ./...` covers its main paths",
}


# --------------------------------------------------------------------------- small helpers
def _key(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _fit(text: str, suffix: str = "", n: int = MAX_TASK) -> str:
    """One redacted line of at most n chars; trims `text` at a word boundary and keeps `suffix` whole."""
    text, suffix = redact(" ".join(str(text).split())), redact(suffix)
    room = n - len(suffix)
    if len(text) > room:
        cut = text[:max(room - 1, 1)]
        if cut.rfind(" ") > room // 2:
            cut = cut[:cut.rfind(" ")]
        text = cut.rstrip(" ,;:.-") + "…"
    return (text + suffix)[:n]


def _cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


def _ago(ts: float, now: float | None = None) -> str:
    if not ts:
        return "never"
    s = max(0.0, (now or time.time()) - ts)
    for unit, size in (("y", 31_536_000), ("w", 604_800), ("d", 86_400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"{int(s // size)}{unit} ago"
    return "just now"


def _inside(path: str, parent: str) -> bool:
    return bool(path and parent) and (path == parent or path.startswith(parent.rstrip("/") + "/"))


def _read_text(path: str, limit: int | None = None) -> str | None:
    """A small UTF-8 text file, or None for secret-looking names, symlinks, big files, binaries and errors."""
    if is_secret_file(path) or os.path.islink(path):
        return None
    limit = limit or MAX_FILE_BYTES
    try:
        if os.path.getsize(path) > limit:
            return None
        with open(path, "rb") as f:
            data = f.read(limit + 1)
    except OSError:
        return None
    return None if b"\x00" in data[:8192] else data.decode("utf-8", errors="replace")


def _json(path: str):
    try:
        return json.loads(_read_text(path) or "null")
    except ValueError:
        return None


_GIT_LOCATION = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR", "GIT_NAMESPACE"}


def _git(repo: str, *args: str) -> str:
    """stdout of a read-only git command in `repo`, "" on any failure. Anything that could write is refused."""
    if not (args[0] in ("log", "status", "rev-parse")
            or args[:2] in (("branch", "--show-current"), ("config", "--get"), ("config", "--get-regexp"))):
        raise ValueError(f"discover runs read-only git only, not: git {' '.join(args)}")
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
    try:   # GIT_OPTIONAL_LOCKS=0: `status` must not refresh (write) the user's index
        r = subprocess.run(["git", "-C", repo, "-c", "core.fsmonitor=false", *args], capture_output=True,
                           encoding="utf-8", errors="replace", timeout=20, env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def _clean_url(url: str) -> str:
    """A remote URL without credentials: drops user:token@ (any http(s) user is often a token) and ?query."""
    url = url.strip()
    if "://" in url:
        try:
            u = urlsplit(url)
            host = (u.hostname or "") + (f":{u.port}" if u.port else "")
        except ValueError:
            return redact(re.sub(r"//[^/]*@", "//", url.split("?")[0]))
        user = f"{u.username}@" if u.username and not u.password and "ssh" in u.scheme else ""
        url = f"{u.scheme}://{user}{host}{u.path}"
    return redact(url)


def _remote(repo: str) -> str | None:
    urls = dict(line.split(None, 1) for line in _git(repo, "config", "--get-regexp", r"^remote\..*\.url$").splitlines()
                if " " in line)
    url = urls.get("remote.origin.url") or next(iter(urls.values()), None)
    return _clean_url(url) if url else None


def _walk(root: str, skip: set[str] = SCAN_SKIP, max_files: int = MAX_FILES):
    """Repo-relative paths of regular files, shallowest first and in a stable order. Skips hidden, vendored, build,
    fixture and secret-looking directories, virtualenvs and symlinks."""
    queue, n = deque([""]), 0
    while queue:
        rel = queue.popleft()
        try:
            with os.scandir(os.path.join(root, rel)) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            r = f"{rel}/{e.name}" if rel else e.name
            try:
                if e.is_symlink():
                    continue
                if e.is_dir():
                    if not (e.name.startswith(".") or e.name in skip or is_secret_file(e.name)
                            or os.path.exists(os.path.join(e.path, "pyvenv.cfg"))):
                        queue.append(r)
                elif e.is_file():
                    n += 1
                    if n > max_files:
                        return
                    yield r
            except OSError:
                continue


# --------------------------------------------------------------------------- languages and tests
def _languages(files: list[str]) -> list[str]:
    n = Counter(EXT_LANG[ext] for ext in (os.path.splitext(f)[1].lower() for f in files) if ext in EXT_LANG)
    n.update(MANIFEST_LANG[f] for f in files if f in MANIFEST_LANG)      # bare names: top-level manifests only
    if n["typescript"] and n["javascript"] * 4 <= n["typescript"]:
        n.pop("javascript", None)                                         # a TS repo's few .js config files
    total = sum(n.values())
    return [lang for lang, k in n.most_common() if k * 20 >= total][:4]


def _scripts(root: str, top: set[str]) -> dict:
    pkg = _json(os.path.join(root, "package.json")) if "package.json" in top else None
    scripts = pkg.get("scripts") if isinstance(pkg, dict) else None
    return scripts if isinstance(scripts, dict) else {}


def _uses_pytest(root: str, files: list[str], py_tests: list[str]) -> bool:
    if any(os.path.basename(f) in ("pytest.ini", "conftest.py") for f in files):
        return True
    for name, needle in (("pyproject.toml", "pytest"), ("setup.cfg", "[tool:pytest]"), ("tox.ini", "[pytest]"),
                         ("requirements-dev.txt", "pytest"), ("requirements.txt", "pytest")):
        if name in files and needle in (_read_text(os.path.join(root, name)) or ""):
            return True
    return any(re.search(r"^\s*(import pytest|from pytest\b)", _read_text(os.path.join(root, f)) or "", re.M)
               for f in py_tests[:5])


def _test_cmd(root: str, files: list[str], langs: list[str]) -> str | None:
    """The command that runs this repo's tests, preferring its main language's convention."""
    top = {f for f in files if "/" not in f}
    cands: dict[str, str] = {}
    script = _scripts(root, top).get("test")
    if isinstance(script, str) and script.strip() and "no test specified" not in script:
        runner = ("pnpm" if "pnpm-lock.yaml" in top else "yarn" if "yarn.lock" in top
                  else "bun run" if top & {"bun.lock", "bun.lockb"} else "npm")
        cands["javascript"] = cands["typescript"] = f"{runner} test"
    py_tests = [f for f in files if re.fullmatch(r"test\w*\.py|\w+_test\.py", os.path.basename(f))]
    if py_tests:
        if _uses_pytest(root, files, py_tests):
            cands["python"] = "python3 -m pytest -q"
        else:
            pat = "" if any(os.path.basename(f).startswith("test") for f in py_tests) else ' -p "*_test.py"'
            d = next((d for d in ("tests", "test") if any(f.startswith(d + "/") for f in py_tests)), None)
            cands["python"] = (f"python3 -m unittest discover -s {d}{pat}" if d and f"{d}/__init__.py" not in files
                               else f"python3 -m unittest discover{pat}")
    for name, lang, cmd in (("Cargo.toml", "rust", "cargo test"), ("go.mod", "go", "go test ./..."),
                            ("Package.swift", "swift", "swift test"), ("mix.exs", "elixir", "mix test"),
                            ("pom.xml", "java", "mvn -q test"), ("gradlew", "java", "./gradlew test"),
                            ("gradlew", "kotlin", "./gradlew test")):
        if name in top:
            cands.setdefault(lang, cmd)
    if "Gemfile" in top and any(f.startswith("spec/") for f in files):
        cands["ruby"] = "bundle exec rspec"
    for lang in langs:
        if lang in cands:
            return cands[lang]
    if cands:
        return next(iter(cands.values()))
    if "Makefile" in top and re.search(r"^test\s*:", _read_text(os.path.join(root, "Makefile")) or "", re.M):
        return "make test"
    return None


def _score(p: ProjectInfo, now: float | None = None) -> float:
    """Recent commits (100 within a day, linear to 0 at 60 days) + 3 per TODO (max 12) + 15 for a test command
    (agents can prove their work) + 5 for uncommitted changes (you're active there)."""
    age = ((now or time.time()) - p.last_commit_ts) / 86400 if p.last_commit_ts else None
    recency = 0.0 if age is None else 100.0 if age <= 1 else max(0.0, 100 * (60 - age) / 59)
    return round(recency + 3 * min(len(p.todos), 12) + 15 * bool(p.test_cmd) + 5 * p.dirty, 1)


# --------------------------------------------------------------------------- discovery
def _match(path: str, patterns) -> bool:
    """`path` matches a repo name (case-insensitive), a glob on the name (`*-old`) or a path (`~/code/archive`)."""
    name = os.path.basename(path).lower()
    for pat in patterns or []:
        pat = str(pat).strip()
        if not pat:
            continue
        if "/" in pat or pat.startswith("~"):
            full = os.path.expanduser(pat)
            if fnmatch(path, full) if any(ch in pat for ch in "*?[") else _inside(path, os.path.realpath(full)):
                return True
        elif fnmatch(name, pat.lower()):
            return True
    return False


def _is_dir(e: os.DirEntry) -> bool:
    try:
        return e.is_dir(follow_symlinks=False)
    except OSError:
        return False


def _find_repos(root: str, max_depth: int, skip, data: str) -> list[str]:
    """Directories holding a .git directory, depth-first in name order. A .git *file* (linked worktree, submodule)
    is not a project, so burn-week's own worktrees never come back as repos."""
    home = os.path.realpath(os.path.expanduser("~"))
    out, stack = [], [(root, 0)]
    while stack:
        d, depth = stack.pop()
        if _inside(d, data):
            continue
        git = os.path.join(d, ".git")
        if d != home and os.path.isdir(git):
            out.append(d)
            continue
        if (os.path.exists(git) and d != home) or depth >= max_depth:
            continue
        try:
            with os.scandir(d) as it:
                subs = sorted(e.name for e in it if _is_dir(e))
        except OSError:
            continue
        for name in reversed(subs):
            sub = os.path.join(d, name)
            if not (name.startswith(".") or name in WALK_SKIP or (d == home and name in HOME_SKIP) or _match(sub, skip)):
                stack.append((sub, depth + 1))
    return out


def _inspect_safe(path: str) -> ProjectInfo:
    try:
        return inspect_project(path)
    except Exception:     # one odd repo must not sink the scan
        return ProjectInfo(path=path, name=os.path.basename(path))


def discover_projects(roots: list[str], max_depth: int = 3, limit: int = 20, include: list[str] | None = None,
                      exclude: list[str] | None = None, skip: list[str] | None = None, *,
                      data_dir: str = DATA_DIR) -> list[ProjectInfo]:
    """Git repos under `roots` (missing roots are skipped), inspected and sorted by score, best first.
    include/exclude/skip take repo names, globs or paths: include keeps only matches, exclude drops matches, skip
    also prunes the walk. Hidden, vendored and build dirs, ~/Library and `data_dir` are never walked."""
    data = os.path.realpath(os.path.expanduser(data_dir))
    found: list[str] = []
    for root in roots or []:
        r = os.path.realpath(os.path.expanduser(str(root)))
        if os.path.isdir(r):
            found += _find_repos(r, max_depth, skip, data)
    repos = [r for r in dict.fromkeys(found) if (not include or _match(r, include)) and not _match(r, exclude)]
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(repos)))) as pool:
        projects = list(pool.map(_inspect_safe, repos))
    projects.sort(key=lambda p: (-p.score, -p.last_commit_ts, p.path))
    return projects if limit is None else projects[:max(0, limit)]


def inspect_project(path: str) -> ProjectInfo:
    """Read-only facts about one repo: git state, languages, test command, harvested TODOs and a score."""
    root = os.path.realpath(os.path.expanduser(path))
    p = ProjectInfo(path=root, name=os.path.basename(root) or root)
    ct = _git(root, "log", "-1", "--format=%ct").strip()
    p.last_commit_ts = float(ct) if ct.isdigit() else 0.0
    p.dirty = bool(_git(root, "status", "--porcelain").strip())
    p.branch = _git(root, "branch", "--show-current").strip()
    p.remote = _remote(root)
    files = list(_walk(root))
    p.languages = _languages(files)
    p.test_cmd = _test_cmd(root, files, p.languages)
    p.todos = _harvest(root, files, 12)
    p.score = _score(p)
    return p


# --------------------------------------------------------------------------- TODO harvest
def _in_string(line: str, pos: int) -> bool:
    q = ""
    for ch in line[:pos]:
        if q:
            q = "" if ch == q else q
        elif ch in "\"'`":
            q = ch
    return bool(q)


def _continued(body: str, opener: str, lines: list[str], i: int) -> str:
    """Join a TODO's continuation lines: `# TODO: split this once the` + `#   pricing rules settle`."""
    if opener == "<!--":
        return body
    lead = "*" if "*" in opener else opener.strip()
    for nxt in lines[i + 1:i + 3]:
        rest = nxt.strip()
        if body.rstrip().endswith((".", "!", ":", "*/")) or not rest.startswith(lead):
            break
        rest = rest[len(lead):].strip()
        if not rest[:1].islower():
            break
        body += " " + rest
    return body


def _phrase(kind: str, text: str, where: str) -> tuple[str, str] | None:
    """(imperative task, dedupe key) for one note, or None when it says too little to act on."""
    t = re.sub(r"\s*(\*+/|-->)\s*$", "", " ".join(text.split())).replace("**", "").strip(" -–—:;,.")
    if len(re.findall(r"[A-Za-z]{2,}", t)) < 2:
        return None
    if kind == "plan" or t.split()[0].lower().strip("`'\"(") in VERBS:
        task = _fit(t[0].upper() + t[1:], f" ({where})")
    elif kind == "FIXME":
        task = _fit(f"Fix {where}: {t}")
    else:
        task = _fit(f"Resolve the {kind} in {where}: {t}")
    return task, _key(t)


def _todos_in(rel: str, text: str):
    """(rank, task, key) for one file. Rank: 0 plan-file checkbox, 1 TODO/FIXME, 2 other checkbox, 3 XXX."""
    name = os.path.basename(rel)
    stem, ext = os.path.splitext(name.lower())
    prose = ext in PROSE_EXT
    if ext in MD_EXT and NOT_PLANS.search(name):
        return
    plan = ext in MD_EXT and stem in PLAN_NAMES
    lines, fence, triple, n = text.splitlines(), False, "", 0
    for i, line in enumerate(lines):
        if ext == ".py":    # `# TODO` inside a multi-line string is embedded code (a template), not a comment
            quotes, inside = re.findall(r"\"\"\"|'''", line), bool(triple)
            for q in quotes:
                triple = q if not triple else "" if q == triple else triple
            if inside or quotes:
                continue
        if len(line) > 500:
            continue
        if prose and line.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        if fence:
            continue
        hit = None
        if prose and (m := CHECKBOX_RE.match(line)):
            hit = (0 if plan else 2), "plan", m.group(1), rel
        elif prose and (m := MD_TODO_RE.match(line)):
            hit = 1, m.group(1), m.group(2), f"{rel}:{i + 1}"
        elif (m := TODO_RE.search(line)) and (prose or not _in_string(line, m.start())):
            body = _continued(m.group(3), m.group(1), lines, i)
            hit = (3 if m.group(2) == "XXX" else 1), m.group(2), body, f"{rel}:{i + 1}"
        if hit and (got := _phrase(*hit[1:])):
            yield hit[0], got[0], got[1]
            n += 1
            if n >= (8 if plan else 3):     # a few per file, so one noisy file can't fill the list
                return


def _harvest(root: str, files, limit: int) -> list[str]:
    found = []
    for rel in files:
        name = os.path.basename(rel)
        if is_secret_file(rel) or name.endswith(GENERATED) or not (
                os.path.splitext(name)[1].lower() in TEXT_EXT or name in TEXT_NAMES):
            continue
        for rank, task, key in _todos_in(rel, _read_text(os.path.join(root, rel)) or ""):
            found.append((rank, len(found), task, key))
        if len(found) >= limit * 6:
            break
    out, seen = [], set()
    for _, _, task, key in sorted(found):
        if key not in seen and len(out) < limit:
            seen.add(key)
            out.append(task)
    return out


def harvest_todos(path: str, limit: int = 12) -> list[str]:
    """TODO/FIXME/XXX comments and unchecked `- [ ]` items in markdown plans, as imperative tasks of at most 160
    chars with their file:line, redacted and deduped. Plan files (TODO.md, PLAN.md, ROADMAP.md...) come first.
    Secret-looking, binary, generated and big files are never opened."""
    root = os.path.realpath(os.path.expanduser(path))
    return _harvest(root, _walk(root), limit)


# --------------------------------------------------------------------------- generic tasks
def _modules(root: str, files: list[str], langs: list[str]) -> list[str]:
    """Non-test source modules of the main languages, largest first (tiny files skipped)."""
    exts = {e for e, lang in EXT_LANG.items() if lang in langs[:2]}
    sized = []
    for f in files:
        stem, ext = os.path.splitext(os.path.basename(f))
        if (ext.lower() not in exts or stem.lower() in SKIP_MODULES or TEST_RE.search(f) or f.endswith(GENERATED)
                or ".config." in f or f.startswith(("docs/", "scripts/", "bin/"))):
            continue
        try:
            size = os.path.getsize(os.path.join(root, f))
        except OSError:
            continue
        if size >= 400:
            sized.append((-size, f))
    return [f for _, f in sorted(sized)]


def _tested_names(root: str, files: list[str]) -> set[str]:
    """Module names the tests mention: test file stems (test_parser.py -> parser) plus names in import lines."""
    names = set()
    for f in [f for f in files if TEST_RE.search(f)][:60]:
        stem = os.path.splitext(os.path.basename(f))[0].lower()
        names.add(re.sub(r"^tests?_?|_?(tests?|spec)$|\.(test|spec)$", "", stem))
        for line in (_read_text(os.path.join(root, f)) or "").splitlines():
            s = line.strip()
            if s.startswith(("from ", "import ", "use ", "mod ")) or "require(" in s:
                names.update(w.lower() for w in re.findall(r"[A-Za-z_]\w*", s))
    names.discard("")
    return names


def _lint(root: str, top: set[str], lang: str) -> str | None:
    def has(name: str, needle: str) -> bool:
        return name in top and needle in (_read_text(os.path.join(root, name)) or "")
    if lang == "python":
        if top & {"ruff.toml", ".ruff.toml"} or has("pyproject.toml", "[tool.ruff"):
            return "ruff check ."
        if ".flake8" in top or has("setup.cfg", "[flake8]") or has("tox.ini", "[flake8]"):
            return "flake8"
    elif lang in ("javascript", "typescript"):
        if isinstance(_scripts(root, top).get("lint"), str):
            return "npm run lint"
        if lang == "typescript" and "tsconfig.json" in top:
            return "npx --no-install tsc --noEmit"
        if any(f.startswith((".eslintrc", "eslint.config")) for f in top):
            return "npx --no-install eslint ."
    elif lang == "go":
        return "go vet ./..."
    elif lang == "rust":
        return "cargo clippy"
    elif lang == "ruby" and ".rubocop.yml" in top:
        return "bundle exec rubocop"
    return None


def generic_tasks(p: ProjectInfo) -> list[str]:
    """Safe, reviewable improvements chosen from what the repo has (tests, linters, docs), most useful first:
    tests for untested modules, a test suite when there is none, lint fixes, a README, CI, a .gitignore."""
    files = list(_walk(p.path, max_files=2000)) if os.path.isdir(p.path) else []
    top = {f for f in files if "/" not in f}
    lang = (p.languages or [""])[0]
    tasks = []
    if p.test_cmd:
        tested = _tested_names(p.path, files)
        untested = [f for f in _modules(p.path, files, p.languages)
                    if os.path.splitext(os.path.basename(f))[0].lower() not in tested]
        tasks += [f"Add unit tests for {f} covering its main paths and edge cases; keep `{p.test_cmd}` passing"
                  for f in untested[:2]]
        tasks.append(f"Run `{p.test_cmd}` and fix any failing or flaky tests without weakening assertions")
    else:
        tasks.append(SETUP_TESTS.get(lang, "Add a minimal test suite with one smoke test and document how to run it"))
    if lint := _lint(p.path, top, lang):
        tasks.append(f"Fix the warnings reported by `{lint}` without changing behavior")
    if not any(f.lower().startswith("readme") for f in top):
        tasks.append(f"Write a README.md for {p.name}: what it does, how to set it up and run it, and how to test it")
    if lang == "python" and (mods := _modules(p.path, files, ["python"])):
        tasks.append(f"Add type hints and docstrings to the public functions in {mods[0]}")
    if p.test_cmd and "github.com" in (p.remote or "") and not os.path.isdir(os.path.join(p.path, ".github", "workflows")):
        tasks.append(f"Add a GitHub Actions workflow that runs `{p.test_cmd}` on pushes and pull requests")
    if lang and ".gitignore" not in top:
        tasks.append(f"Add a .gitignore for {lang} build output, caches and local env files")
    out, seen = [], set()
    for t in map(_fit, tasks):
        if _key(t) not in seen:
            seen.add(_key(t))
            out.append(t)
    return out


# --------------------------------------------------------------------------- plan + report
def _notes(p: ProjectInfo) -> str:
    bits = [", ".join(p.languages[:3])] if p.languages else []
    bits += [f"on {p.branch}"] if p.branch else []
    bits += [f"last commit {_ago(p.last_commit_ts)}"] if p.last_commit_ts else []
    bits += ["uncommitted changes stay in your checkout (agents start from HEAD)"] if p.dirty else []
    return _fit(" · ".join(bits), n=200)


def _plan_tasks(p: ProjectInfo, ideas: list[Idea], n: int) -> list[str]:
    """TODOs and chat ideas, alternating, topped up with generic tasks; deduped, one line each."""
    picked, seen = [], set()

    def take(t: str) -> None:
        t, k = " ".join(str(t).split()), _key(str(t))
        if t and k not in seen and len(picked) < n:
            seen.add(k)
            picked.append(t)
    todos, notes = list(p.todos), [i.text for i in ideas]
    while (todos or notes) and len(picked) < n:
        for q in (todos, notes):
            if q:
                take(q.pop(0))
    if len(picked) < n:
        for t in generic_tasks(p):
            take(t)
    return picked


def to_plan_md(projects, ideas: dict[str, list[Idea]] | None = None, per_project: int = 3,
               title: str = "Burn week") -> str:
    """A Relay plan (docs/Relay-Plan-Format.md; relay.plan.parse reads it back): one `## ` section per repo with
    `dir:`, `test:`, `priority:` (list order) and `notes:`, then up to `per_project` `- [ ]` tasks. Ideas whose
    project_dir is the repo or inside it join its tasks; the rest are quoted up top, not queued."""
    projects, ideas = list(projects), ideas or {}
    names = Counter(p.name for p in projects)
    lines = [f"# {' '.join(str(title).split()) or 'Burn week'}", "",
             f"> Generated by `relay burn discover` on {time.strftime('%Y-%m-%d')}. "
             "Edit freely: only `- [ ]` items are queued.", ""]
    body, matched = [], set()
    for i, p in enumerate(projects, 1):
        dirs = [d for d in ideas if d and _inside(d, p.path)]
        matched.update(dirs)
        mine = sorted((x for d in dirs for x in ideas[d]), key=lambda x: -x.score)
        name = p.name if names[p.name] == 1 else f"{p.name} ({os.path.basename(os.path.dirname(p.path))})"
        body += [f"## {name}", f"dir: {p.path}"] + ([f"test: {p.test_cmd}"] if p.test_cmd else []) + [f"priority: {i}"]
        body += [f"notes: {n}"] if (n := _notes(p)) else []
        body += [""] + [f"- [ ] {t}" for t in _plan_tasks(p, mine, per_project)] + [""]
    loose = sorted((x for d, xs in ideas.items() if d not in matched for x in xs), key=lambda x: -x.score)[:10]
    if loose:
        lines += ["> Chat ideas that match no repo here (move one under a project as `- [ ]` to queue it):"]
        lines += [f"> - {_fit(x.text, n=200)}" for x in loose] + [""]
    return redact("\n".join(lines + body).rstrip() + "\n")


def report(projects: list[ProjectInfo]) -> str:
    """Compact colored table of discovered repos, best first."""
    if not projects:
        return ui.c("2", "no git repos found (check the roots; hidden, vendored and build dirs are skipped)")
    now = time.time()
    lines = [ui.c("1", f"{'#':>2}  {'repo':<22}{'score':>6}  {'last commit':<12}{'languages':<20}{'tests':<28}"
                       f"{'todos':>5}  branch")]
    for i, p in enumerate(projects, 1):
        tests = ui.c("32", f"{_cut(p.test_cmd, 26):<28}") if p.test_cmd else ui.c("2", f"{'none':<28}")
        branch = (p.branch or "detached") + ("*" if p.dirty else "")
        lines.append(f"{i:>2}  " + ui.c("1", f"{_cut(p.name, 21):<22}") + ui.c("36", f"{p.score:>6.0f}")
                     + f"  {_ago(p.last_commit_ts, now):<12}{_cut(', '.join(p.languages[:2]) or '-', 19):<20}" + tests
                     + f"{len(p.todos):>5}  " + (ui.c("33", branch) if p.dirty else branch))
    lines.append(ui.c("2", f"{len(projects)} repos · {sum(len(p.todos) for p in projects)} TODOs harvested · "
                           f"{sum(bool(p.test_cmd) for p in projects)} with a test command · * uncommitted changes"))
    return redact("\n".join(lines))
