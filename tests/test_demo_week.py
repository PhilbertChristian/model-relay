"""relay.demo_week: sandbox git repos, the demo config, summary/record/ticker helpers, and (once the swarm and the
mock lane exist) one short full run. Offline: temp dirs only, no keys, no network."""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from relay import demo_week
from relay.bus import Bus

NO_SUPABASE = {"SUPABASE_URL": "", "SUPABASE_KEY": "", "SUPABASE_SERVICE_ROLE_KEY": ""}   # Bus/Telemetry stay local
SIBLINGS = ("relay.swarm", "relay.lanes.mock", "relay.worktree", "relay.discover", "relay.usage", "relay.pace")


def importable(*names: str) -> bool:
    try:
        return all(importlib.util.find_spec(n) is not None for n in names)
    except ImportError:
        return False


def git(path, *args) -> str:
    return demo_week._git(Path(path), *args).strip()


class Sandbox(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.sb = demo_week.make_sandbox(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_projects_are_valid_clean_local_git_repos(self):
        self.assertGreaterEqual(len(self.sb["projects"]), 5)
        for p in self.sb["projects"]:
            self.assertEqual(git(p, "rev-parse", "--is-inside-work-tree"), "true")
            self.assertEqual(Path(git(p, "rev-parse", "--show-toplevel")).resolve(), Path(p).resolve())
            self.assertEqual(git(p, "rev-list", "--count", "HEAD"), "1")            # one initial commit
            self.assertEqual(git(p, "branch", "--show-current"), "main")
            self.assertEqual(git(p, "status", "--porcelain"), "")                   # clean checkout
            self.assertEqual(git(p, "remote"), "")                                  # nothing to push to
            self.assertEqual(git(p, "config", "--local", "user.email"), "demo@relay.invalid")
            self.assertEqual(git(p, "config", "--local", "commit.gpgsign"), "false")

    def test_projects_look_real_and_have_work_to_harvest(self):
        names = {Path(p).name: Path(p) for p in self.sb["projects"]}
        for name, manifest in (("orbit-api", "pyproject.toml"), ("lumen-web", "tsconfig.json"), ("ferry", "main.go"),
                               ("atlas-docs", "mkdocs.yml"), ("quill", "src/lib.rs")):
            self.assertTrue((names[name] / manifest).exists(), manifest)
        for (name, rel), tool in demo_week.TOOLED.items():          # only where the toolchain is installed
            self.assertEqual((names[name] / rel).exists(), bool(shutil.which(tool)), rel)
        for name, p in names.items():
            text = {f: f.read_text() for f in p.rglob("*") if f.is_file() and ".git" not in f.parts}
            self.assertTrue(any("TODO" in t or "FIXME" in t for t in text.values()), name)
            self.assertTrue(any(f.suffix == ".md" and "- [ ]" in t for f, t in text.items()), name)
            self.assertFalse(any(f.name.startswith(".env") for f in text), name)    # no secret-shaped files

    def test_layout_stays_inside_the_root(self):
        root = Path(self.sb["root"])
        self.assertEqual(root, Path(self.tmp.name).resolve())
        self.assertTrue(Path(self.sb["data_dir"]).is_dir())
        self.assertIn(root, Path(self.sb["data_dir"]).parents)
        self.assertTrue(all(root in Path(p).parents for p in self.sb["projects"]))
        with self.assertRaises(FileExistsError):                                    # never builds over old work
            demo_week.make_sandbox(self.tmp.name)

    def test_python_project_tests_pass_offline(self):
        api = next(p for p in self.sb["projects"] if p.endswith("orbit-api"))
        r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"], cwd=api,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_toolchain_manifests_are_left_out_without_the_tool(self):
        for which, present in ((lambda tool: None, False), (lambda tool: "/usr/bin/" + tool, True)):
            with tempfile.TemporaryDirectory() as d, mock.patch.object(demo_week.shutil, "which", which):
                names = {Path(p).name: Path(p) for p in demo_week.make_sandbox(d)["projects"]}
                for (name, rel), _ in demo_week.TOOLED.items():
                    self.assertEqual((names[name] / rel).exists(), present, rel)
                self.assertEqual(git(names["ferry"], "status", "--porcelain"), "")

    @unittest.skipUnless(importable("relay.discover"), "relay.discover not built yet")
    def test_every_detected_test_command_passes_here(self):
        """The demo never shows a test failure the machine caused: discover's command for each repo passes."""
        from relay import discover
        env = {**os.environ, "CI": "true", "npm_config_update_notifier": "false"}    # npm stays offline
        for p in self.sb["projects"]:
            cmd = discover.inspect_project(p).test_cmd
            if cmd and cmd.split()[0] not in ("go", "cargo"):                       # compiles: too slow here
                r = subprocess.run(cmd, shell=True, cwd=p, env=env, capture_output=True, text=True, timeout=120)
                self.assertEqual(r.returncode, 0, f"{p}: {cmd}\n{r.stdout}{r.stderr}")


class Config(unittest.TestCase):
    def setUp(self):
        self.sb = {"root": "/tmp/sbx", "projects": [], "data_dir": "/tmp/sbx/.relay-burn"}
        self.cfg = demo_week.demo_config(self.sb, speed=3.0)

    def test_demo_subscriptions(self):
        subs = {s["name"]: s for s in self.cfg["subscriptions"]}
        self.assertEqual(set(subs), {"claude-max", "codex", "agent37", "openai", "monid"})
        for s in subs.values():
            self.assertEqual(s["kind"], "demo")
            self.assertTrue({"used", "limit", "unit", "resets_in_hours", "lane"} <= set(s))
        cm = subs["claude-max"]
        self.assertEqual((cm["unit"], cm["limit"]), ("tokens", 400_000_000))
        self.assertAlmostEqual(cm["used"] / cm["limit"], 0.78, places=2)
        self.assertAlmostEqual(cm["resets_in_hours"], 30, delta=3)
        self.assertAlmostEqual(subs["codex"]["used"] / subs["codex"]["limit"], 0.60, places=2)
        self.assertEqual([subs[n]["unit"] for n in ("agent37", "openai", "monid")], ["usd", "usd", "credits"])

    def test_mock_lanes(self):
        from relay.lanes import KINDS
        lanes = self.cfg["lanes"]
        self.assertTrue(all(ln["kind"] == "mock" and ln["kind"] in KINDS for ln in lanes))
        self.assertEqual({ln["as"] for ln in lanes}, {"claude", "codex", "agent37", "relay", "orca"})
        subs = {s["name"] for s in self.cfg["subscriptions"]}
        for ln in lanes:
            self.assertGreaterEqual(ln["max_agents"], 1)
            self.assertIn(ln["subscription"], subs)
        self.assertEqual(self.cfg["lane_defaults"]["speed"], 3.0)

    def test_sandbox_roots_and_no_remote_features(self):
        self.assertEqual(self.cfg["roots"], ["/tmp/sbx/projects"])
        self.assertEqual(self.cfg["data_dir"], "/tmp/sbx/.relay-burn")
        self.assertFalse(self.cfg["ideas"]["enabled"])                              # never reads real chat history
        self.assertFalse(self.cfg["monid"]["enabled"])                              # no network

    def test_pace_fills_the_demo_lanes(self):
        if not importable("relay.pace", "relay.usage"):
            self.skipTest("relay.pace / relay.usage not built yet")
        from relay import pace, usage
        plans = pace.plan_pace(usage.weekly_usage(self.cfg, "/nonexistent/events.jsonl"), self.cfg["lanes"],
                               max_agents=8, target_pct=0.97)
        self.assertEqual(sum(p.agents for p in plans), 8)
        self.assertTrue(all(p.agents >= 1 for p in plans), [(p.lane, p.agents) for p in plans])


ROWS = [
    {"event": "swarm_start", "run": "r1", "max_agents": 4, "queue": 2, "lanes": [{"name": "claude"}, {"name": "codex"}]},
    {"event": "agent_start", "agent": 1, "task_id": "t1", "project": "orbit-api", "text": "add rate limiting",
     "lane": "claude", "branch": "relay/burn/orbit-api-1a2b3c", "worktree": "/sb/.relay-burn/worktrees/orbit-api/t1"},
    {"event": "agent_end", "agent": 1, "task_id": "t1", "project": "orbit-api", "lane": "claude", "status": "done",
     "tokens": 8_400_000, "usd": 0.0, "commit": "abcdef1234", "diffstat": "1 file changed, 12 insertions(+)"},
    {"event": "agent_start", "agent": 2, "task_id": "t2", "project": "ferry", "text": "publish release binaries",
     "lane": "codex", "branch": "relay/burn/ferry-9f8e7d", "worktree": "/sb/.relay-burn/worktrees/ferry/t2"},
    {"event": "agent_end", "agent": 2, "task_id": "t2", "project": "ferry", "lane": "codex", "status": "blocked",
     "tokens": 3_900_000, "usd": 0.0, "commit": None, "diffstat": ""},
    {"event": "lane_limited", "lane": "codex", "until": 0, "reason": "usage limit"},
    {"event": "swarm_end", "run": "r1", "done": 1, "blocked": 1, "failed": 0, "limited": 0, "tokens": 12_300_000,
     "usd": 1.5, "report": "/sb/.relay-burn/BURN.md", "seconds": 71, "reason": "queue empty"},
]


class Reporting(unittest.TestCase):
    def test_summary_lists_report_totals_and_branches(self):
        s = demo_week.summary(ROWS, {"done": 1})
        for want in ("1 done", "1 blocked", "12.3M tokens", "$1.50", "queue empty", "BURN.md  /sb/.relay-burn/BURN.md",
                     "relay/burn/orbit-api-1a2b3c", "abcdef1", "relay/burn/ferry-9f8e7d", "local only"):
            self.assertIn(want, s)
        self.assertIn("burn-week · 0 done", demo_week.summary([]))                   # nothing ran: still prints

    def test_ticker_prints_one_line_per_lifecycle_event(self):
        with redirect_stdout(io.StringIO()) as out:
            for r in ROWS:
                demo_week._ticker(r)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 6)                                             # no line for swarm_end
        self.assertIn("orbit-api: add rate limiting", lines[1])
        self.assertIn("8.4M tok", lines[2])
        self.assertIn("blocked", lines[4])
        self.assertIn("lane codex hit a usage limit", lines[5])

    def test_watch_without_dash_subscribes_a_ticker_and_stops(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, NO_SUPABASE):
            bus = Bus(log_dir=d)
            with redirect_stdout(io.StringIO()) as out:
                stop = demo_week.watch(bus, dash=True)                              # not a TTY: ticker, no TUI
                bus.emit("agent_start", **{k: v for k, v in ROWS[1].items() if k != "event"})
                stop()
                bus.emit("agent_start", **{k: v for k, v in ROWS[3].items() if k != "event"})
        self.assertIn("orbit-api: add rate limiting", out.getvalue())
        self.assertNotIn("ferry", out.getvalue())

    def test_record_rows_redacts_and_neutralises_paths(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "sb")
            rows = [{"event": "agent_progress", "note": "used sk-ant-abcdefghijklmnopqrstuvwxyz123",
                     "worktree": f"{root}/.relay-burn/worktrees/orbit-api/t1",
                     "lanes": [{"name": "c", "note": "token ghp_abcdefghijklmnopqrstuvwxyz0123456789"}],
                     "home": os.path.join(str(Path.home()), "code")}]
            out = os.path.join(d, "docs", "sample-events.jsonl")
            self.assertEqual(demo_week.record_rows(rows, out, root), 1)
            text = Path(out).read_text()
        row = json.loads(text)
        self.assertEqual(row["worktree"], "~/relay-burn-demo/.relay-burn/worktrees/orbit-api/t1")
        self.assertEqual(row["home"], "~/code")
        for leak in ("sk-ant-", "ghp_", root, str(Path.home()) + "/"):
            self.assertNotIn(leak, text)


@unittest.skipUnless(importable(*SIBLINGS), "swarm / mock lane / worktree / discover / usage / pace not built yet")
class FullDemo(unittest.TestCase):
    def test_short_run_leaves_checkouts_untouched(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, NO_SUPABASE):
            rec, box = os.path.join(d, "sample-events.jsonl"), {}

            def go():
                try:
                    with redirect_stdout(io.StringIO()) as out:
                        box["res"] = demo_week.run_demo(agents=4, speed=200, dash=False, record=rec,
                                                        root=os.path.join(d, "sb"))
                    box["out"] = out.getvalue()
                except BaseException as e:                                          # surface it in the main thread
                    box["err"] = e

            t = threading.Thread(target=go, daemon=True)
            t.start()
            t.join(60)
            self.assertFalse(t.is_alive(), "run_demo did not finish within 60 s")
            if "err" in box:
                raise box["err"]
            res, sb = box["res"], box["res"]["sandbox"]
            rows = [json.loads(x) for x in (Path(sb["root"]) / ".relay" / "events.jsonl").read_text().splitlines()]
            events = [r["event"] for r in rows]
            self.assertIn("swarm_start", events)
            self.assertIn("swarm_end", events)
            self.assertIn("burn-week ·", box["out"])
            branches = 0
            for p in sb["projects"]:                                                # invariants 1 and 2
                self.assertEqual(git(p, "branch", "--show-current"), "main")
                self.assertEqual(git(p, "rev-list", "--count", "main"), "1")
                self.assertEqual(git(p, "status", "--porcelain"), "")
                self.assertEqual(git(p, "remote"), "")
                branches += len(git(p, "branch", "--list", "relay/burn/*").split())
            self.assertGreaterEqual(branches, 1)
            recorded = Path(rec).read_text()
            self.assertEqual(len(recorded.splitlines()), len(rows))
            self.assertNotIn(sb["root"], recorded)
            self.assertIsInstance(res, dict)


if __name__ == "__main__":
    unittest.main()
