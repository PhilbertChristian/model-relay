"""burn-week demo: a sandbox of small git projects, demo subscriptions and mock lanes, so `relay burn demo` shows
the whole swarm (pacing, worktrees, commits, dashboards, BURN.md) with no API keys and no network.

summary() and watch() are shared with `relay burn week`."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

from . import ui
from .contracts import redact

# name -> {path: content}. Believable repos with TODO/FIXME comments and a markdown plan the swarm can harvest.
PROJECTS: dict[str, dict[str, str]] = {
    "orbit-api": {
        "README.md": "# orbit-api\n\nJSON API behind the Orbit habit tracker. Standard library only.\n\n"
                     "    python3 -m orbit_api   # serves on :8080\n    make test\n",
        "pyproject.toml": '[project]\nname = "orbit-api"\nversion = "0.3.0"\nrequires-python = ">=3.10"\n',
        "Makefile": "test:\n\tpython3 -m unittest discover -s tests -q\n",
        ".gitignore": "__pycache__/\n.env\n",
        "orbit_api/__init__.py": '"""Orbit habit-tracker API."""\n__version__ = "0.3.0"\n',
        "orbit_api/app.py": '''"""Routes for the Orbit API."""
import json

from .store import Store

store = Store()


def health():
    return 200, {"status": "ok"}


def list_habits(page: int = 1, per_page: int = 20):
    items = store.all()
    # FIXME: fix the off-by-one that repeats the last habit at the top of page 2
    start = (page - 1) * per_page - (1 if page > 1 else 0)
    return 200, {"items": items[start:start + per_page], "page": page}


def create_habit(body: bytes):
    # TODO: validate habit names (non-empty, at most 80 characters) and return 422 otherwise
    return 201, store.add(json.loads(body or b"{}").get("name", ""))


def login(body: bytes):
    # TODO: add rate limiting to POST /login (5 attempts per minute per IP)
    return 501, {"error": "not implemented"}
''',
        "orbit_api/store.py": '''"""In-memory habit store."""
import itertools


class Store:
    # TODO: persist habits to SQLite so a restart keeps streaks
    def __init__(self):
        self._ids, self._items = itertools.count(1), []

    def add(self, name: str) -> dict:
        self._items.append({"id": next(self._ids), "name": name, "streak": 0})
        return self._items[-1]

    def all(self) -> list:
        return list(self._items)
''',
        "tests/__init__.py": "",
        "tests/test_app.py": '''import unittest

from orbit_api import app


class AppTest(unittest.TestCase):
    def test_health(self):
        self.assertEqual(app.health(), (200, {"status": "ok"}))

    def test_create_habit(self):
        status, item = app.create_habit(b'{"name": "read 20 pages"}')
        self.assertEqual((status, item["name"]), (201, "read 20 pages"))
''',
        "ROADMAP.md": "## Now\n- [x] Health endpoint\n- [ ] Add request logging middleware with a request id\n"
                      "- [ ] Serve an OpenAPI schema at /openapi.json\n\n## Later\n- [ ] Weekly streak summary endpoint\n",
    },
    "lumen-web": {
        "README.md": "# lumen-web\n\nPhoto journal front end (TypeScript + Vite).\n\n    npm run dev\n    npm test\n",
        "package.json": json.dumps({"name": "lumen-web", "version": "0.1.0", "private": True, "type": "module",
                                    "scripts": {"dev": "vite", "build": "tsc --noEmit && vite build", "test": "node --test"},
                                    "devDependencies": {"typescript": "^5.6.0", "vite": "^5.4.0"}}, indent=2) + "\n",
        "tsconfig.json": '{\n  "compilerOptions": {"target": "ES2022", "module": "ESNext", "strict": true, "allowJs": true}\n}\n',
        ".gitignore": "node_modules/\ndist/\n",
        "index.html": '<!doctype html>\n<title>Lumen</title>\n<div id="app"></div>\n<script type="module" src="/src/main.ts"></script>\n',
        "src/main.ts": '''import { renderHeader } from "./components/header";
import { formatDate } from "./lib/format.js";

// TODO: add a dark mode toggle that remembers the choice in localStorage
const app = document.querySelector<HTMLDivElement>("#app")!;
app.innerHTML = renderHeader("Lumen") + `<p>Updated ${formatDate(new Date())}</p>`;
''',
        "src/components/header.ts": '''export function renderHeader(title: string): string {
  // FIXME: keep the nav on one row below 380px instead of wrapping
  return `<header><h1>${title}</h1><nav><a href="/">Home</a> <a href="/gallery">Gallery</a></nav></header>`;
}
''',
        "src/lib/format.js": '''// @ts-check
/** @param {Date} d */
export function formatDate(d) {
  // TODO: use the reader's locale instead of hard-coding en-US
  return d.toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" });
}

/** @param {string} s */
export function slugify(s) {
  return s.toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}
''',
        "tests/format.test.mjs": '''import { test } from "node:test";
import assert from "node:assert/strict";
import { slugify } from "../src/lib/format.js";

test("slugify", () => assert.equal(slugify("  Hello, Lumen! "), "hello-lumen"));
''',
        "TODO.md": "- [ ] Lazy-load gallery images below the fold\n- [ ] Add a friendly 404 page\n"
                   "- [x] Header and nav\n- [ ] Add alt text to every gallery image\n",
    },
    "ferry": {
        "README.md": "# ferry\n\nCopy a directory tree somewhere else, quickly.\n\n    go run . --from src --to dst\n",
        "go.mod": "module github.com/example/ferry\n\ngo 1.18\n",
        "main.go": '''package main

import (
	"flag"
	"fmt"
	"os"
)

func main() {
	src := flag.String("from", ".", "directory to copy from")
	dst := flag.String("to", "", "directory to copy into")
	// TODO: add a --dry-run flag that prints what would be copied
	flag.Parse()
	if *dst == "" {
		fmt.Fprintln(os.Stderr, "ferry: --to is required")
		os.Exit(2)
	}
	n, err := Sync(*src, *dst)
	if err != nil {
		// FIXME: remove the half-written file when a copy fails
		fmt.Fprintln(os.Stderr, "ferry:", err)
		os.Exit(1)
	}
	fmt.Printf("ferried %d files\\n", n)
}
''',
        "sync.go": '''package main

import (
	"os"
	"path/filepath"
)

// Sync copies regular files from src into dst and returns how many it copied.
func Sync(src, dst string) (int, error) {
	n := 0
	// TODO: skip files whose size and modification time already match
	err := filepath.WalkDir(src, func(p string, d os.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		rel, _ := filepath.Rel(src, p)
		if err := copyFile(p, filepath.Join(dst, rel)); err != nil {
			return err
		}
		n++
		return nil
	})
	return n, err
}

func copyFile(from, to string) error {
	if err := os.MkdirAll(filepath.Dir(to), 0o755); err != nil {
		return err
	}
	b, err := os.ReadFile(from)
	if err != nil {
		return err
	}
	return os.WriteFile(to, b, 0o644)
}
''',
        "sync_test.go": '''package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestSyncCopiesFiles(t *testing.T) {
	src, dst := t.TempDir(), t.TempDir()
	os.WriteFile(filepath.Join(src, "a.txt"), []byte("hi"), 0o644)
	if n, err := Sync(src, dst); err != nil || n != 1 {
		t.Fatalf("Sync = %d, %v", n, err)
	}
}
''',
        "PLAN.md": "# ferry plan\n\n- [ ] Print a summary table (files, bytes, seconds) at the end\n"
                   "- [ ] Add a --exclude glob flag\n- [ ] Publish release binaries for macOS and Linux\n",
    },
    "atlas-docs": {
        "README.md": "# atlas-docs\n\nUser docs for Atlas, built with MkDocs.\n\n    mkdocs serve\n    make test   # link check\n",
        "mkdocs.yml": "site_name: Atlas Docs\nnav:\n  - Home: index.md\n  - Getting started: getting-started.md\n"
                      "  - API: api.md\ntheme:\n  name: material\n",
        "Makefile": "test:\n\tpython3 scripts/check_links.py\n",
        "scripts/check_links.py": '''"""Fail when a relative link in docs/ points at a missing page."""
import pathlib
import re
import sys

bad = [f"{p}: {t}" for p in pathlib.Path("docs").glob("*.md")
       for t in re.findall(r"\\]\\(([^)#:]+)\\)", p.read_text()) if not (p.parent / t).exists()]
print("\\n".join(bad) or "links ok")
sys.exit(1 if bad else 0)
''',
        "docs/index.md": "# Atlas\n\nAtlas keeps your team's runbooks in one place. Start with [Getting started](getting-started.md).\n",
        "docs/getting-started.md": "# Getting started\n\n1. Install the CLI.\n2. Run `atlas init` in your repo.\n\n"
                                   "<!-- TODO: add screenshots of the setup wizard -->\n",
        "docs/api.md": "# API\n\n<!-- FIXME: document the renamed /v2/exports endpoint (was /v1/export) -->\n"
                       "`GET /v1/runbooks` lists runbooks.\n",
        "TODO.md": "- [ ] Add a search page\n- [ ] Write an FAQ from the top support questions\n"
                   "- [ ] Add a changelog page\n",
    },
    "quill": {
        "README.md": "# quill\n\nStrip markdown down to plain text. A tiny Rust library.\n\n    cargo test\n",
        "Cargo.toml": '[package]\nname = "quill"\nversion = "0.2.0"\nedition = "2021"\n\n[dependencies]\n',
        ".gitignore": "target/\n",
        "src/lib.rs": '''//! quill: strip markdown down to plain text.

/// Render a markdown string as plain text.
pub fn render(md: &str) -> String {
    // TODO: support CRLF line endings
    md.lines().map(|l| l.trim_start_matches('#').trim()).collect::<Vec<_>>().join("\\n")
}

/// The first heading, if any.
pub fn title(md: &str) -> Option<&str> {
    // FIXME: return None when the first line is not a heading
    md.lines().next().map(|l| l.trim_start_matches('#').trim())
}

#[cfg(test)]
mod tests {
    #[test]
    fn strips_headings() {
        assert_eq!(super::render("# Hi\\nthere"), "Hi\\nthere");
    }
}
''',
        "ROADMAP.md": "## 0.3\n- [ ] Render bullet lists with a leading dash\n- [ ] Wrap output at a configurable width\n"
                      "- [ ] Publish 0.3.0 to crates.io\n",
    },
}
# Files that make relay.discover pick a test command needing this tool. Where the tool isn't installed they are left
# out, so the project has no test command ("not run") instead of a test run that can only fail.
TOOLED = {("lumen-web", "package.json"): "npm", ("ferry", "go.mod"): "go", ("quill", "Cargo.toml"): "cargo"}
GIT_CONFIG = {"user.name": "Burn Demo", "user.email": "demo@relay.invalid", "commit.gpgsign": "false",
              "tag.gpgsign": "false", "core.hooksPath": os.devnull}
DEMO_ROOT = "~/relay-burn-demo"   # what the sandbox path becomes in recorded rows


def _git(cwd: Path, *args: str, env: dict | None = None) -> str:
    """git with no inherited GIT_* (so a hook's GIT_DIR can't point us at a real repo) and no user config."""
    base = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    base.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0", **(env or {}))
    return subprocess.run(["git", *args], cwd=cwd, env=base, capture_output=True, text=True, check=True).stdout


def make_sandbox(root: str) -> dict:
    """Create the demo projects as git repos under <root>/projects: local identity, one backdated commit each.
    Returns {"root", "projects": [abs paths], "data_dir"}; data_dir is <root>/.relay-burn (worktrees, BURN.md)."""
    base = Path(root).expanduser().resolve()
    home = base / "projects"
    if home.exists() and any(home.iterdir()):
        raise FileExistsError(f"{home} is not empty: the demo builds its sandbox in a fresh directory")
    now, projects = time.time(), []
    for i, (name, files) in enumerate(PROJECTS.items()):
        path = home / name
        for rel, body in files.items():
            if (name, rel) in TOOLED and not shutil.which(TOOLED[name, rel]):
                continue
            (path / rel).parent.mkdir(parents=True, exist_ok=True)
            (path / rel).write_text(body)
        _git(path, "init", "-q", "-b", "main")
        for k, v in GIT_CONFIG.items():
            _git(path, "config", k, v)
        _git(path, "add", "-A")
        stamp = f"{int(now - (i + 1) * 5400)} +0000"     # staggered, so discover's recency order is stable
        _git(path, "commit", "-q", "-m", f"{name}: initial scaffold",
             env={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp})
        projects.append(str(path))
    data_dir = base / ".relay-burn"
    data_dir.mkdir(parents=True, exist_ok=True)
    return {"root": str(base), "projects": projects, "data_dir": str(data_dir)}


def demo_config(sandbox: dict, speed: float = 1.0) -> dict:
    """burner-week config for the sandbox: kind "demo" subscriptions (no keys, no network) and kind "mock" lanes
    that pose as the real ones. `speed` scales the mock lanes' pace (2.0 = twice as fast)."""
    h = 30.0                                                              # hours to the weekly reset

    def sub(name, lane, unit, used, limit):
        return {"name": name, "kind": "demo", "lane": lane, "unit": unit, "used": used, "limit": limit,
                "resets_in_hours": h}

    # agent_rate (units/hour one agent burns) is low so relay.pace wants every lane busy at the start.
    # Mock params (relay/lanes/mock.py): ~20 s a task at speed 1 (~20 tasks on 8 agents: a 60-90 s run), a fixed
    # seed so takes look alike, and outcomes per lane so codex is likely to hit its usage limit mid-run.
    def lane(name, flavor, subscription, agents, rate, done=0.86, blocked=0.08, failed=0.06, limited=0.0):
        return {"kind": "mock", "name": name, "as": flavor, "subscription": subscription, "max_agents": agents,
                "agent_rate": rate, "outcomes": {"done": done, "blocked": blocked, "failed": failed, "limited": limited}}

    return {
        "week": {"reset": "mon 09:00", "timezone": "UTC", "target_pct": 0.97},
        "roots": [str(Path(sandbox["root"]) / "projects")], "max_depth": 2, "max_projects": 12, "per_project": 4,
        "max_agents": 8, "data_dir": sandbox["data_dir"], "ideas": {"enabled": False}, "monid": {"enabled": False},
        "subscriptions": [sub("claude-max", "claude", "tokens", 312_000_000, 400_000_000),     # 78% used
                          sub("codex", "codex", "tokens", 60_000_000, 100_000_000),            # 60%
                          sub("agent37", "agent37", "usd", 11.40, 20.0),
                          sub("openai", "relay", "usd", 7.80, 20.0),
                          sub("monid", "monid", "credits", 420, 1000)],
        "lane_defaults": {"speed": speed, "seconds": 20, "seed": 1},
        "lanes": [lane("claude", "claude", "claude-max", 4, 500_000),
                  lane("codex", "codex", "codex", 2, 400_000, done=0.6, blocked=0.05, failed=0.05, limited=0.3),
                  lane("agent37", "agent37", "agent37", 2, 0.1),
                  lane("relay", "relay", "openai", 2, 0.1),
                  lane("orca", "orca", "claude-max", 2, 250_000, done=0.9, blocked=0.1, failed=0.0)],
    }


def _human(n) -> str:
    n = float(n or 0)
    return next((f"{n / d:.1f}{u}" for d, u in ((1e9, "B"), (1e6, "M"), (1e3, "k")) if n >= d), f"{n:.0f}")


def _ticker(row: dict) -> None:
    """One plain line per lifecycle event, for when the terminal dashboard is off or can't draw."""
    e, who = row.get("event"), f"[{row.get('agent', '-')}] {row.get('lane', '')}"
    if e == "swarm_start":
        lanes = ", ".join(str(x.get("name")) for x in row.get("lanes") or [])
        print(ui.c("1", f"swarm: {row.get('queue')} tasks, up to {row.get('max_agents')} agents on {lanes}"))
    elif e == "agent_start":
        print(ui.c("36", f"▶ {who:<14}") + f" {row.get('project')}: {row.get('text')}")
    elif e == "agent_end":
        st = row.get("status")
        mark = {"done": ui.c("32", "✓"), "blocked": ui.c("33", "!"), "limited": ui.c("35", "⏸")}.get(st, ui.c("31", "✗"))
        print(f"{mark} {who:<14} {row.get('project')}: {st}  {_human(row.get('tokens'))} tok  {row.get('diffstat') or ''}")
    elif e == "lane_limited":
        print(ui.c("35", f"⏸ lane {row.get('lane')} hit a usage limit: {row.get('reason', '')}; no more launches on it"))


def watch(bus, dash: bool = True, web_port: int | None = None) -> Callable[[], None]:
    """Start the web dashboard on web_port (prints its URL; it keeps serving until the process exits) and attach the
    terminal dashboard, or a plain ticker when dash is off or stdout is not a terminal. Returns stop()."""
    if web_port:
        from . import web
        try:
            srv = web.serve(port=web_port, events_path=str(bus.tel.path), bus=bus)
            print(ui.c("1;36", "web dashboard  ") + getattr(srv, "url", f"http://127.0.0.1:{web_port}/"))
        except OSError as e:
            ui.warn(f"web dashboard not started on port {web_port}: {e}")
    if dash and sys.stdout.isatty():
        from . import dash as tui
        return tui.attach(bus) or (lambda: None)
    return bus.subscribe(_ticker)


def summary(rows: list[dict], result: dict | None = None) -> str:
    """Totals, the BURN.md path and one line per task branch, from the event rows (swarm_end, else a tally of the
    agent_end rows). From burn_week's result only the "report" path and the stop "reason" are used."""
    ends = [r for r in rows if r.get("event") == "agent_end"]
    tot = {"done": 0, "blocked": 0, "failed": 0, "limited": 0, "tokens": sum(int(r.get("tokens") or 0) for r in ends),
           "usd": sum(float(r.get("usd") or 0) for r in ends)}
    for r in ends:
        tot[r.get("status")] = tot.get(r.get("status"), 0) + 1
    res, extra = {**tot, **next((r for r in reversed(rows) if r.get("event") == "swarm_end"), {})}, result or {}
    report, reason = res.get("report") or extra.get("report"), res.get("reason") or extra.get("reason")
    out = ["\n" + ui.c("1", "burn-week · ") + f"{res['done']} done · {res['blocked']} blocked · {res['failed']} failed · "
           f"{res['limited']} limited · {_human(res['tokens'])} tokens · ${float(res['usd'] or 0):.2f}"
           + (f" · {reason}" if isinstance(reason, str) and reason else "")]
    if isinstance(report, str) and report and "\n" not in report:
        out.append(ui.c("1", "BURN.md  ") + report)
    starts = {r.get("task_id"): r for r in rows if r.get("event") == "agent_start"}
    marks = {"done": ui.c("32", "✓"), "blocked": ui.c("33", "!"), "limited": ui.c("35", "⏸")}
    branches = [f"  {marks.get(r.get('status'), ui.c('31', '✗'))} {str(r.get('project') or s.get('project')):<12} "
                f"{str(s.get('branch', '')):<36} {str(r.get('commit') or '-------')[:7]}  {r.get('diffstat') or ''}"
                for r in ends for s in [starts.get(r.get("task_id"), {})] if r.get("commit") or s.get("branch")]
    if branches:
        out += [ui.c("1", "branches") + ui.c("2", " (local only: review, then merge what you like)"), *branches]
    return "\n".join(out)


def _scrub(v, swaps: list[tuple[str, str]]):
    """redact() every string and swap local paths for neutral ones, recursively."""
    if isinstance(v, str):
        for old, new in swaps:
            v = v.replace(old, new)
        return redact(v)
    if isinstance(v, dict):
        return {k: _scrub(x, swaps) for k, x in v.items()}
    return [_scrub(x, swaps) for x in v] if isinstance(v, list) else v


def record_rows(rows: list[dict], path: str, sandbox_root: str | None = None) -> int:
    """Write rows as redacted JSONL (e.g. docs/sample-events.jsonl): sandbox and home paths neutralised. Returns count."""
    roots = {sandbox_root, os.path.realpath(sandbox_root)} if sandbox_root else set()
    swaps = sorted(((p, DEMO_ROOT) for p in roots if p.strip("/")), key=lambda s: -len(s[0]))
    swaps += [(str(Path.home()), "~")] if str(Path.home()).strip("/") else []
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(_scrub(r, swaps)) + "\n")
    return len(rows)


def run_demo(agents: int = 8, speed: float = 1.0, dash: bool = True, web_port: int | None = None,
             record: str | None = None, root: str | None = None) -> dict:
    """Build the sandbox (in `root`, else a temp dir), then burn_week() it on mock lanes with the ledger inside the
    sandbox. Prints the BURN.md path and branch summary; returns burn_week's result dict."""
    from . import swarm
    from .bus import Bus
    sb = make_sandbox(root or tempfile.mkdtemp(prefix="relay-burn-demo-"))
    cfg = demo_config(sb, speed)
    bus = Bus(log_dir=os.path.join(sb["root"], ".relay"))
    print(ui.c("1;36", "relay burn demo") + ui.c("2", f" · {len(sb['projects'])} sandbox projects in {sb['root']}"
                                                  f" · mock lanes, no keys · ledger {bus.tel.path}"))
    stop = watch(bus, dash, web_port)
    try:
        res = swarm.burn_week(cfg, max_agents=agents, data_dir=sb["data_dir"], bus=bus)
        if dash and sys.stdout.isatty():
            time.sleep(2.0)                              # hold the final dashboard frame for the camera
    except KeyboardInterrupt:
        res = {"reason": "interrupted"}
    finally:
        stop()
    res = res if isinstance(res, dict) else {}
    if record:
        n = record_rows(bus.rows, record, sb["root"])
        print(ui.c("2", f"recorded {n} redacted rows to {record}"))
    print(summary(bus.rows, res))
    return {**res, "sandbox": sb}
