"""Local lanes (mock, claude, codex): python3 -m unittest tests.test_lanes_local -v

Offline and hermetic: PATH holds only a temp bin dir with the fake `claude` / `codex` from tests/fixtures/lanes
(shebang pinned to this interpreter), HOME is a temp dir, and the real CLIs are never reachable.
"""
import dataclasses
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from relay.contracts import ProjectInfo, SwarmTask
from relay.lanes import lane_class
from relay.lanes.base import task_prompt
from relay.lanes.claude import ClaudeLane, reset_time, scrubbed_env
from relay.lanes.codex import CodexLane
from relay.lanes.mock import MockLane

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "lanes"
FAKE = "x" * 24
SECRETS = {"ANTHROPIC_API_KEY": "sk-ant-" + FAKE, "OPENAI_API_KEY": "sk-" + FAKE, "GITHUB_TOKEN": "gh" + "p_" + FAKE,
           "CLAUDE_CODE_OAUTH_TOKEN": FAKE, "AWS_SECRET_ACCESS_KEY": FAKE, "STRIPE_CLIENT_SECRET_LIVE": FAKE,
           "SUPABASE_URL": "https://demo.supabase.co", "SUPABASE_KEY": FAKE, "MONID_API_KEY": FAKE,
           "MONID_BASE": "https://monid.example", "CONTEXT_DEV_API": FAKE, "AGENT37_API_KEY": FAKE,
           "AGENT37_INSTANCE_ID": "inst-1", "SSH_AUTH_SOCK": "/tmp/agent.sock"}
KEEP = {"RELAY_LANE_TEST": "1", "LANG": "en_US.UTF-8", "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "8000"}
FORBIDDEN = {"--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "bypassPermissions",
             "--dangerously-bypass-approvals-and-sandbox", "--yolo", "danger-full-access"}
SPEC_DENY = ["Bash(git push:*)", "Bash(git remote:*)", "Bash(git checkout:*)", "Bash(git switch:*)",
             "Bash(git stash:*)", "Bash(git reset:*)", "Bash(rm -rf:*)", "Bash(sudo:*)", "Bash(curl:*)",
             "Bash(wget:*)", "Read(./.env*)", "Read(**/.env*)", "Read(~/.ssh/**)"]


def make_task(text="handle empty input in parse()", tid="demo-1a2b3c", test_cmd="python3 -m unittest"):
    p = ProjectInfo(path="/nonexistent/demo", name="demo", languages=["python"], test_cmd=test_cmd)
    return SwarmTask(id=tid, project=p, text=text)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


class LaneCase(unittest.TestCase):
    """A temp worktree, a temp HOME, and a PATH with nothing on it but the fake CLIs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin, self.home, self.work = self.tmp / "bin", self.tmp / "home", self.tmp / "wt"
        for d in (self.bin, self.home, self.work / "src"):
            d.mkdir(parents=True)
        for name in ("claude", "codex"):
            body = (FIXTURES / name).read_text().split("\n", 1)[1]
            (self.bin / name).write_text(f"#!{sys.executable}\n{body}")
            (self.bin / name).chmod(0o755)
        (self.work / ".git").write_text("gitdir: /nonexistent/.git/worktrees/wt\n")      # a worktree, not a checkout
        (self.work / "src" / "parse.py").write_text("def parse(s):\n    # TODO: handle empty input\n    return s.split()\n")
        self.dump, self.pids, self.events = self.tmp / "dump.json", self.tmp / "pids.json", []

    def env(self, scenario: str):
        return mock.patch.dict(os.environ, {"PATH": str(self.bin), "HOME": str(self.home), "FAKE_SCENARIO": scenario,
                                            "FAKE_LANE_DUMP": str(self.dump), "FAKE_PIDS": str(self.pids),
                                            **SECRETS, **KEEP}, clear=True)

    def emit(self, event, **data):
        self.events.append(dict(data, event=event))

    def run_lane(self, lane, scenario="success", timeout=20.0):
        with self.env(scenario):
            self.assertEqual(Path(lane.available()[1].split(" at ")[-1]).parent, self.bin)   # only ever the fake
            return lane.run(make_task(), str(self.work), self.emit, timeout=timeout)

    def dumped(self) -> dict:
        return json.loads(self.dump.read_text())

    def progress(self) -> list[dict]:
        self.assertTrue(self.events)
        self.assertEqual({e["event"] for e in self.events}, {"agent_progress"})
        for key in ("tokens", "usd"):
            seq = [e[key] for e in self.events]
            self.assertEqual(seq, sorted(seq), f"{key} must be cumulative")
        return self.events

    def check_env(self):
        d = self.dumped()
        self.assertFalse(set(SECRETS) & set(d["env"]), "secrets must not reach the agent CLI")
        self.assertTrue(set(KEEP) <= set(d["env"]))
        self.assertEqual((d["home"], d["path"]), (str(self.home), str(self.bin)))
        self.assertEqual(os.path.realpath(d["cwd"]), os.path.realpath(self.work))

    def assert_pids_dead(self):
        pids, end = json.loads(self.pids.read_text()), time.monotonic() + 3
        while any(alive(p) for p in pids) and time.monotonic() < end:
            time.sleep(0.05)
        self.assertEqual([p for p in pids if alive(p)], [], "the CLI and everything it spawned must be dead")

    def check_timeout_kills_group(self, lane):
        t0 = time.monotonic()
        res = self.run_lane(lane, "slow", timeout=1)
        self.assertLess(time.monotonic() - t0, 8)
        self.assertEqual(res.status, "failed")
        self.assertIn("timed out after 1s", res.summary)
        self.assert_pids_dead()


class ClaudeLaneTest(LaneCase):
    def lane(self, **cfg):
        return ClaudeLane(None, {"progress_every": 0, "model": "sonnet", **cfg})

    def test_argv_exact_no_forbidden_flags_deny_list_present(self):
        lane = self.lane(allowed_tools=["Bash(make lint:*)", "--dangerously-skip-permissions", "Bash(git push origin:*)"],
                         disallowed_tools=["WebFetch"])
        self.assertEqual(self.run_lane(lane).status, "done")
        task, argv = make_task(), self.dumped()["argv"]
        self.assertEqual(argv, lane.args(task))
        self.assertEqual(argv[:9], ["-p", task_prompt(task), "--output-format", "stream-json", "--verbose",
                                    "--permission-mode", "acceptEdits", "--model", "sonnet"])
        self.assertFalse(FORBIDDEN & set(argv))
        self.assertFalse([a for a in argv if a.startswith("--dangerously") or a.startswith("--allow-dangerously")])
        allow = argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")]
        deny = argv[argv.index("--disallowedTools") + 1:]
        self.assertEqual([t for t in SPEC_DENY if t not in deny], [])
        self.assertIn("WebFetch", deny)
        self.assertIn("Bash(make lint:*)", allow)
        self.assertIn("Bash(python3 -m unittest:*)", allow)                  # the project's own test command
        self.assertFalse([t for t in allow if "push" in t or "dangerously" in t])

    def test_hostile_model_never_reaches_argv(self):
        argv = ClaudeLane(None, {"model": "--dangerously-skip-permissions"}).args(make_task())
        self.assertNotIn("--model", argv)
        self.assertFalse(FORBIDDEN & set(argv))

    def test_env_is_scrubbed(self):
        self.run_lane(self.lane())
        self.check_env()

    def test_success_progress_tokens_and_usd(self):
        res = self.run_lane(self.lane())
        self.assertEqual((res.status, res.input_tokens, res.output_tokens, res.usd, res.model),
                         ("done", 9123, 195, 0.0421, "claude-sonnet-5"))
        self.assertIn("tests pass", res.summary)
        ev = self.progress()
        self.assertEqual([e["tokens"] for e in ev], [2015, 2015, 4440, 6885, 9318])   # deduped per API message
        self.assertGreater(ev[-1]["usd"], 0)
        notes = [e["note"] for e in ev]
        self.assertIn("Read src/parse.py", notes)
        self.assertIn("$ python3 -m unittest # key [redacted]", notes)
        self.assertFalse([n for n in notes if "sk-ant" in n])
        self.assertTrue((self.work / "fake_claude_edit.txt").exists())

    def test_blocked(self):
        res = self.run_lane(self.lane(), "blocked")
        self.assertEqual(res.status, "blocked")
        self.assertTrue(res.summary.startswith("BLOCKED: need the Stripe test key"))

    def test_limit_words_in_a_successful_summary_are_not_a_limit(self):
        lane = self.lane()
        self.assertEqual(self.run_lane(lane, "rateword").status, "done")
        self.assertFalse(lane.is_limited())

    def test_usage_limit_sets_limited_until_and_stops_launching(self):
        lane = self.lane()
        res = self.run_lane(lane, "limit")
        self.assertEqual((res.status, res.limited_until, res.summary),
                         ("limited", 1893456000.0, "Claude AI usage limit reached"))
        self.assertFalse(lane.is_limited(), "the swarm marks the lane (and emits lane_limited), not the lane itself")
        lane.limited_until = res.limited_until                                  # what the swarm does
        self.dump.unlink()
        self.assertEqual(self.run_lane(lane, "success").status, "limited")
        self.assertFalse(self.dump.exists(), "a limited lane must not launch again before its reset")

    def test_rate_limit_without_a_reset_time_waits_an_hour(self):
        res = self.run_lane(self.lane(), "ratelimit")
        self.assertEqual(res.status, "limited")
        self.assertAlmostEqual(res.limited_until, time.time() + 3600, delta=60)
        self.assertEqual(res.input_tokens, 2010)

    def test_crash_fails_with_a_redacted_tail(self):
        res = self.run_lane(self.lane(), "crash")
        self.assertEqual(res.status, "failed")
        for part in ("claude exited 3", "exploded", "Loading session", "[redacted]"):
            self.assertIn(part, res.summary)
        self.assertNotIn("Z" * 20, res.summary)

    def test_timeout_kills_the_process_group(self):
        self.check_timeout_kills_group(self.lane())

    def test_leftover_processes_die_with_the_agent(self):
        t0 = time.monotonic()
        res = self.run_lane(self.lane(), "orphan", timeout=20)
        self.assertEqual(res.status, "done")
        self.assertLess(time.monotonic() - t0, 8, "a leftover child holding stdout must not stall the lane")
        self.assert_pids_dead()

    def test_refuses_the_users_own_checkout(self):
        (self.work / ".git").unlink()
        (self.work / ".git").mkdir()
        res = self.run_lane(self.lane())
        self.assertEqual(res.status, "failed")
        self.assertIn("primary checkout", res.summary)
        self.assertFalse(self.dump.exists())

    def test_available_follows_path(self):
        with self.env("success"):
            self.assertTrue(ClaudeLane().available()[0])
        with mock.patch.dict(os.environ, {"PATH": str(self.home)}, clear=True):
            self.assertFalse(ClaudeLane().available()[0])


class CodexLaneTest(LaneCase):
    def lane(self, **cfg):
        return CodexLane(None, {"progress_every": 0, "model": "gpt-5-codex", "price_in": 1.25, "price_out": 10, **cfg})

    def test_argv_exact_and_sandboxed(self):
        lane = self.lane()
        self.assertEqual(self.run_lane(lane).status, "done")
        argv = self.dumped()["argv"]
        self.assertEqual(argv, ["exec", "--json", "--sandbox", "workspace-write", "-c",
                                "sandbox_workspace_write.network_access=false", "--model", "gpt-5-codex",
                                task_prompt(make_task())])
        self.assertFalse(FORBIDDEN & set(argv))
        self.assertNotIn("--model", CodexLane(None, {"model": "--yolo"}).args(make_task()))

    def test_env_is_scrubbed(self):
        self.run_lane(self.lane())
        self.check_env()

    def test_success_progress_tokens_and_usd(self):
        res = self.run_lane(self.lane())
        self.assertEqual((res.status, res.input_tokens, res.output_tokens, res.usd, res.model),
                         ("done", 24763, 1220, 0.043154, "gpt-5-codex"))
        self.assertEqual(res.summary, "Fixed parse() for empty input; tests pass.")
        ev = self.progress()
        self.assertEqual(ev[-1]["tokens"], 25983)
        notes = [e["note"] for e in ev]
        for note in ("thinking: Scanning the parser", "$ bash -lc 'rg -n TODO src'", "editing src/parse.py"):
            self.assertIn(note, notes)
        self.assertEqual(notes.count("$ bash -lc 'rg -n TODO src'"), 1)
        self.assertTrue((self.work / "fake_codex_edit.txt").exists())

    def test_older_event_format(self):
        res = self.run_lane(self.lane(), "legacy")
        self.assertEqual((res.status, res.input_tokens, res.output_tokens, res.summary),
                         ("done", 9000, 700, "Done: added tests for parse()."))
        self.assertIn("$ bash -lc pytest -q", [e["note"] for e in self.progress()])

    def test_blocked(self):
        res = self.run_lane(self.lane(), "blocked")
        self.assertEqual(res.status, "blocked")
        self.assertTrue(res.summary.startswith("BLOCKED:"))

    def test_usage_limit_parses_the_reset(self):
        lane = self.lane()
        res = self.run_lane(lane, "limit")
        self.assertEqual(res.status, "limited")
        self.assertIn("usage limit", res.summary)
        self.assertAlmostEqual(res.limited_until, time.time() + 7500, delta=60)    # "try again in 2 hours 5 minutes"
        self.assertFalse(lane.is_limited())                                         # the swarm marks the lane

    def test_crash_fails_with_a_redacted_tail(self):
        res = self.run_lane(self.lane(), "crash")
        self.assertEqual(res.status, "failed")
        self.assertIn("codex exited 101", res.summary)
        self.assertIn("panicked", res.summary)
        self.assertNotIn("Z" * 20, res.summary)

    def test_timeout_kills_the_process_group(self):
        self.check_timeout_kills_group(self.lane())


class MockLaneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def project(self, name: str, todo: bool = True) -> Path:
        root = self.tmp / name
        (root / "src").mkdir(parents=True)
        (root / "tests").mkdir()
        (root / "README.md").write_text("# demo\n")
        (root / ".env").write_text("NOT_READ=1\n")
        (root / "src" / "app.py").write_text("def fetch(url):\n" + ("    # TODO: add retry to fetch()\n" if todo else "")
                                             + "    return url\n")
        (root / "tests" / "test_app.py").write_text("import unittest\n")
        return root

    def run_mock(self, work: Path, task=None, timeout: float = 30, **cfg):
        events = []
        lane = MockLane(None, {"speed": 1000, **cfg})
        res = lane.run(task or make_task("add retry to fetch()"), str(work), lambda ev, **d: events.append(dict(d, event=ev)),
                       timeout=timeout)
        return lane, res, events

    def test_registry_kinds(self):
        self.assertEqual((lane_class("mock"), lane_class("claude"), lane_class("codex")), (MockLane, ClaudeLane, CodexLane))

    def test_deterministic_per_seed_and_task_id(self):
        runs = [self.run_mock(self.project(n), seed=seed) for n, seed in (("a", 7), ("b", 7), ("c", 8))]
        (_, r1, e1), (_, r2, e2), (_, r3, e3) = runs
        same = lambda r: dataclasses.replace(r, limited_until=None)
        self.assertEqual(same(r1), same(r2))
        self.assertEqual(e1, e2)
        self.assertEqual(snapshot(self.tmp / "a"), snapshot(self.tmp / "b"))
        self.assertNotEqual([e["tokens"] for e in e1], [e["tokens"] for e in e3])
        _, r4, e4 = self.run_mock(self.project("d"), task=make_task("add retry to fetch()", tid="demo-ffffff"), seed=7)
        self.assertNotEqual([e["tokens"] for e in e1], [e["tokens"] for e in e4])

    def test_done_fixes_the_todo_and_reports_cumulative_progress(self):
        w = self.project("p")
        lane, res, ev = self.run_mock(w, outcomes={"done": 1}, **{"as": "claude"})
        self.assertEqual((res.status, res.model), ("done", "claude-sonnet-5"))
        self.assertIn("    # Done: add retry to fetch() (relay burn, claude)\n", (w / "src" / "app.py").read_text())
        self.assertEqual((w / ".env").read_text(), "NOT_READ=1\n")
        self.assertEqual({e["event"] for e in ev}, {"agent_progress"})
        for key in ("tokens", "usd"):
            self.assertEqual([e[key] for e in ev], sorted(e[key] for e in ev))
        self.assertEqual((ev[-1]["tokens"], ev[-1]["usd"]), (res.tokens, res.usd))
        self.assertGreater(res.usd, 0)
        notes = [e["note"] for e in ev]
        self.assertIn("Edit src/app.py", notes)
        self.assertIn("$ python3 -m unittest", notes)
        self.assertFalse([n for n in notes if ".env" in n])

    def test_without_a_matching_todo_it_adds_a_test(self):
        w = self.project("p", todo=False)
        before = snapshot(w)
        _, res, _ = self.run_mock(w, task=make_task("document the CLI flags"), outcomes={"done": 1})
        added = set(snapshot(w)) - set(before)
        self.assertEqual(res.status, "done")
        self.assertEqual(len(added), 1)
        new = w / added.pop()
        self.assertTrue(new.name.startswith("test_burn_") and new.suffix == ".py")
        compile(new.read_text(), str(new), "exec")

    def test_ticks_its_checklist_item_and_blocks_human_only_tasks(self):
        w = self.project("p", todo=False)
        (w / "TODO.md").write_text("# todo\n\n- [ ] Add a friendly 404 page\n- [ ] Publish 0.3.0 to PyPI\n")
        _, res, _ = self.run_mock(w, task=make_task("Add a friendly 404 page (TODO.md)"), outcomes={"done": 1})
        self.assertEqual(res.status, "done")
        self.assertIn("- [x] Add a friendly 404 page (relay burn, mock)\n", (w / "TODO.md").read_text())
        before = snapshot(w)
        _, res, _ = self.run_mock(w, task=make_task("Publish 0.3.0 to PyPI (TODO.md)", tid="demo-pub"), outcomes={"done": 1})
        self.assertEqual(res.status, "blocked")
        self.assertTrue(res.summary.startswith("BLOCKED: needs a human (Publish)"))
        self.assertEqual(snapshot(w), before)

    def test_outcomes_are_configurable(self):
        w = self.project("p")
        before = snapshot(w)
        _, res, _ = self.run_mock(w, outcomes={"blocked": 1})
        self.assertEqual(res.status, "blocked")
        self.assertTrue(res.summary.startswith("BLOCKED:"))
        self.assertEqual(snapshot(w), before)
        self.assertEqual(self.run_mock(w, outcomes={"failed": 1})[1].status, "failed")
        self.assertIn(self.run_mock(w, outcomes={"success": 1, "done": 0})[1].status,    # unusable -> the defaults
                      {"done", "blocked", "failed", "limited"})
        lane, res, _ = self.run_mock(w, outcomes={"limited": 1}, limit_seconds=600)
        self.assertEqual(res.status, "limited")
        self.assertAlmostEqual(res.limited_until, time.time() + 600, delta=30)
        self.assertFalse(lane.is_limited())                                         # the swarm marks the lane
        lane.limited_until = res.limited_until
        self.assertEqual(lane.run(make_task(), str(w), lambda *a, **k: None).status, "limited")

    def test_flavor_changes_model_and_narration(self):
        _, res, ev = self.run_mock(self.project("p"), outcomes={"done": 1}, **{"as": "codex"})
        self.assertEqual(res.model, "gpt-5-codex")
        self.assertIn("apply_patch src/app.py", [e["note"] for e in ev])

    def test_timeout(self):
        t0 = time.monotonic()
        _, res, _ = self.run_mock(self.project("p"), timeout=0.3, speed=1, seconds=14)
        self.assertEqual(res.status, "failed")
        self.assertIn("timed out", res.summary)
        self.assertLess(time.monotonic() - t0, 2)


class Helpers(unittest.TestCase):
    def test_reset_time_reads_the_cli_wording(self):
        now = 1_800_000_000.0
        self.assertEqual(reset_time("Claude AI usage limit reached|1893456000", now), 1893456000.0)
        self.assertEqual(reset_time("Upgrade to Pro or try again in 2 hours 5 minutes.", now), now + 7500)
        self.assertEqual(reset_time('{"resets_in_seconds": 90}', now), now + 90)
        at = reset_time("You've hit your limit · resets 3pm (America/Los_Angeles)", now)
        self.assertTrue(now < at <= now + 86400)
        self.assertEqual(datetime.fromtimestamp(at, ZoneInfo("America/Los_Angeles")).hour, 15)
        self.assertIsNone(reset_time("rate_limit_error", now))

    def test_scrubbed_env(self):
        env = scrubbed_env({**SECRETS, **KEEP, "PATH": "/bin", "HOME": "/h"})
        self.assertEqual(env, {**KEEP, "PATH": "/bin", "HOME": "/h"})


if __name__ == "__main__":
    unittest.main()
