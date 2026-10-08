"""python3 -m unittest tests.test_swarm -v   (temp git repos, a scripted FakeLane, fake sibling modules; offline)"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from relay import swarm
from relay.bus import Bus
from relay.contracts import Idea, LaneResult, PacePlan, ProjectInfo, Usage, to_dict
from relay.lanes.base import Lane

SPEC_FIELDS = {   # SWARM_SPEC "Events": exact field names
    "swarm_start": {"run", "max_agents", "lanes", "queue", "usages", "resets_at"},
    "pace": {"lane", "subscription", "unit", "used", "limit", "pct", "remaining", "agents", "target_rate", "hours_left"},
    "task_queued": {"task_id", "project", "text", "source"},
    "agent_start": {"agent", "task_id", "project", "text", "lane", "branch", "worktree"},
    "agent_progress": {"agent", "task_id", "lane", "tokens", "usd", "note"},
    "agent_end": {"agent", "task_id", "project", "lane", "status", "summary", "tokens", "usd", "commit", "diffstat",
                  "tests_ok", "seconds"},
    "lane_limited": {"lane", "until", "reason"},
    "swarm_end": {"run", "done", "blocked", "failed", "limited", "tokens", "usd", "by_lane", "report", "seconds",
                  "reason"},
}
ROW = {"ts", "session", "host", "event"}
LEAKS = (".env", "notes/secrets.yml")


def git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=check).stdout.strip()


def make_repo(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Repo Owner")
    git(repo, "config", "user.email", "owner@example.com")
    (repo / "README.md").write_text(f"# {name}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    (repo / ".env").write_text("OPENAI_API_KEY=sk-proj-0123456789abcdefghijkl\n")    # the user's untracked secret
    return repo


def checkout_state(repo: Path) -> tuple:
    return (git(repo, "rev-parse", "HEAD"), git(repo, "symbolic-ref", "HEAD"),
            git(repo, "status", "--porcelain", "--untracked-files=all"), git(repo, "stash", "list"), git(repo, "remote"))


def fake_module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    return mod


class Gauge:
    """How many agents are inside lane.run at once."""

    def __init__(self):
        self.lock, self.now, self.peak = threading.Lock(), 0, 0

    def __enter__(self):
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)

    def __exit__(self, *exc):
        with self.lock:
            self.now -= 1


class FakeLane(Lane):
    """Scripted lane: `script` maps a substring of the task text to done | blocked | failed | limited | crash |
    rogue | none. Done/blocked/failed write a file, plus secret files that must never be committed."""
    kind = "fake"

    def __init__(self, name="fake", script=None, max_agents=4, delay=0.0, ok=True, gauge=None, limit_for=3600.0, **cfg):
        super().__init__(name, {"max_agents": max_agents, **cfg})
        self.script, self.delay, self.ok, self.limit_for = script or {}, delay, ok, limit_for
        self.gauge, self.mine = gauge or Gauge(), Gauge()
        self.ran, self.briefs = [], {}

    def available(self):
        return (True, "ok") if self.ok else (False, "fake CLI not installed")

    def run(self, task, workdir, emit, timeout=1800):
        self.ran.append(task.id)
        self.briefs[task.id] = task.brief
        with self.gauge, self.mine:
            status = next((s for k, s in self.script.items() if k in task.text), "done")
            emit("agent_progress", tokens=40, usd=0.001, note="reading the repo")
            time.sleep(self.delay)
            if status == "crash":
                raise RuntimeError("lane exploded")
            if status == "none":
                return None
            if status == "limited":
                return LaneResult("limited", "usage limit reached", input_tokens=5,
                                  limited_until=time.time() + self.limit_for)
            if status == "rogue":
                emit("swarm_end", run="fake", reason="lanes cannot end the swarm")
                emit("agent_end", status="done")
                emit("lane_log", line="hello")
            Path(workdir, f"{task.id}.txt").write_text(task.text + "\n")
            Path(workdir, ".env").write_text("OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz\n")
            Path(workdir, "notes").mkdir(exist_ok=True)
            Path(workdir, "notes", "secrets.yml").write_text("token: abcdefghijkl\n")
            emit("agent_progress", tokens=120, usd=0.004, note="wrote the change")
            status = "done" if status == "rogue" else status
            summary = {"blocked": "BLOCKED: needs a PyPI token", "failed": "gave up"}.get(status, f"did: {task.text}")
            return LaneResult(status, summary, input_tokens=100, output_tokens=50, usd=0.005)


class SwarmCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="relay-swarm-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "gitconfig").write_text("")
        env = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(self.tmp / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"})
        env.start()
        self.addCleanup(env.stop)
        for k in ("SUPABASE_URL", "SUPABASE_KEY", "SUPABASE_SERVICE_ROLE_KEY"):
            os.environ.pop(k, None)                                    # no telemetry leaves the machine
        mods = mock.patch.dict(sys.modules, {"relay.discover": fake_module("relay.discover", generic_tasks=lambda p: [])})
        mods.start()
        self.addCleanup(mods.stop)
        self.data = self.tmp / "data"
        self.bus = Bus(log_dir=str(self.tmp / ".relay"))
        self.alpha, self.beta = make_repo(self.tmp, "alpha"), make_repo(self.tmp, "beta")
        self.before = {r: checkout_state(r) for r in (self.alpha, self.beta)}

    def project(self, repo, todos=(), test_cmd=None) -> ProjectInfo:
        return ProjectInfo(path=str(repo), name=repo.name, todos=list(todos), test_cmd=test_cmd)

    def run_swarm(self, queue, lanes, **kw):
        kw.setdefault("max_agents", 3)
        return swarm.run_swarm({"tick": 0.01, **kw.pop("cfg", {})}, queue, lanes, self.bus, str(self.data), **kw)

    def events(self, name=None) -> list[dict]:
        return [r for r in self.bus.rows if name is None or r["event"] == name]

    def assert_checkouts_untouched(self):
        for repo, state in self.before.items():
            self.assertEqual(checkout_state(repo), state, repo.name)

    def assert_events_match_spec(self):
        for r in self.bus.rows:
            if r["event"] in SPEC_FIELDS:
                self.assertEqual(set(r) - ROW, SPEC_FIELDS[r["event"]], r["event"])
        self.assertEqual(self.bus.rows[0]["event"], "swarm_start")
        self.assertEqual(self.bus.rows[-1]["event"], "swarm_end")
        self.assertEqual(len(self.events("swarm_end")), 1)


class Run(SwarmCase):
    def test_tasks_land_on_burn_branches_and_checkouts_stay_untouched(self):
        never_break = "python3 -c \"import pathlib,sys; sys.exit(any('break' in p.read_text() for p in pathlib.Path('.').glob('*.txt')))\""
        pa = self.project(self.alpha, ["add feature one", "publish to pypi", "explode on purpose"], "python3 -c 'print(1)'")
        pb = self.project(self.beta, ["add feature two", "give up early", "break the tests"], never_break)
        queue = swarm.build_queue([pa, pb])
        lane = FakeLane("claude", {"publish": "blocked", "explode": "crash", "give up": "failed"}, delay=0.05)
        res = self.run_swarm(queue, [lane], max_agents=2, enrich=lambda t: "Research notes: use argparse")

        status = {t["text"]: t["status"] for t in res["tasks"]}
        self.assertEqual(status, {"add feature one": "done", "add feature two": "done", "publish to pypi": "blocked",
                                  "explode on purpose": "failed", "give up early": "failed", "break the tests": "failed"})
        self.assertEqual((res["done"], res["blocked"], res["failed"], res["limited"]), (2, 1, 3, 0))
        rows = {t["text"]: t for t in res["tasks"]}
        self.assertIn("lane crashed: RuntimeError: lane exploded", rows["explode on purpose"]["summary"])
        broken = rows["break the tests"]
        self.assertTrue(broken["summary"].startswith("tests failed") and broken["commit"] and broken["tests_ok"] is False)
        self.assertTrue(rows["add feature one"]["tests_ok"])
        for t in res["tasks"]:
            repo = Path(t["repo"])
            self.assertEqual(t["branch"], f"relay/burn/{t['task_id']}")
            self.assertEqual(t["worktree"], "")                        # removed after finalize; the branch has the work
            committed = int(git(repo, "rev-list", "--count", f"main..{t['branch']}"))
            self.assertEqual(committed, 1 if t["commit"] else 0, t["text"])
            tree = set(git(repo, "ls-tree", "-r", "--name-only", t["branch"]).split())
            self.assertFalse(tree & set(LEAKS), t["text"])             # secret files are never committed
            if t["commit"]:
                self.assertIn(f"{t['task_id']}.txt", tree)
        self.assert_checkouts_untouched()
        self.assertEqual(set(lane.briefs.values()), {"Research notes: use argparse"})

        self.assert_events_match_spec()
        start = self.events("swarm_start")[0]
        self.assertEqual((start["queue"], start["max_agents"]), (6, 2))
        self.assertEqual(start["lanes"], [{"name": "claude", "kind": "fake", "subscription": "claude", "as": "fake"}])
        for t in queue:
            seq = [r["event"] for r in self.bus.rows if r.get("task_id") == t.id]
            self.assertEqual(seq[0], "task_queued")
            self.assertEqual(seq[1], "agent_start")
            self.assertIn("agent_progress", seq[2:-1])
            self.assertEqual(seq[-1], "agent_end")
        self.assertTrue({r["agent"] for r in self.events("agent_start")} <= {1, 2})
        self.assertTrue(any(r["note"] == "research brief ready" for r in self.events("agent_progress")))
        end = self.events("swarm_end")[0]
        self.assertEqual((end["done"], end["blocked"], end["failed"], end["report"]), (2, 1, 3, str(self.data / "BURN.md")))
        self.assertEqual(end["by_lane"]["claude"]["tasks"], 6)
        ledger = [json.loads(line) for line in (self.tmp / ".relay" / "events.jsonl").read_text().splitlines()]
        self.assertEqual(len(ledger), len(self.bus.rows))

        report = Path(res["report"]).read_text()
        self.assertNotIn("push", report.lower())
        self.assertNotIn("sk-proj", report)
        for t in res["tasks"]:
            self.assertIn(t["branch"], report)
        self.assertIn("## Needs you", report)
        self.assertIn("BLOCKED: needs a PyPI token", report)
        self.assertIn("git merge relay/burn/", report)

    def test_limited_lane_stops_getting_work_and_its_task_moves_to_another_lane(self):
        queue = swarm.build_queue([self.project(self.alpha, ["first task", "second task", "third task"])])
        capped, spare = FakeLane("claude", {"": "limited"}), FakeLane("codex")
        res = self.run_swarm(queue, [capped, spare], max_agents=1)
        first = queue[0].id
        self.assertEqual(capped.ran, [first])                          # once limited, never again
        self.assertEqual(spare.ran, [t.id for t in queue])
        limited = self.events("lane_limited")
        self.assertEqual(len(limited), 1)
        self.assertEqual(limited[0]["lane"], "claude")
        self.assertGreater(limited[0]["until"], time.time() + 3000)
        later = self.bus.rows[self.bus.rows.index(limited[0]):]
        self.assertFalse([r for r in later if r["event"] == "agent_start" and r["lane"] == "claude"])
        ends = [(r["lane"], r["status"]) for r in self.events("agent_end") if r["task_id"] == first]
        self.assertEqual(ends, [("claude", "limited"), ("codex", "done")])
        row = next(t for t in res["tasks"] if t["task_id"] == first)
        self.assertEqual((row["status"], row["lane"], row["attempts"], row["branch"]), ("done", "codex", 2, f"relay/burn/{first}"))
        self.assertEqual((res["done"], res["limited"]), (3, 0))
        self.assertGreater(capped.limited_until, time.time())
        self.assert_events_match_spec()
        self.assert_checkouts_untouched()

    def test_limited_everywhere_stops_without_retrying_the_same_lane(self):
        queue = swarm.build_queue([self.project(self.alpha, ["only task", "never started"])])
        capped = FakeLane("claude", {"": "limited"})
        t = time.time()
        res = self.run_swarm(queue, [capped], max_agents=1, deadline=time.time() + 600)   # the reset is after it
        self.assertLess(time.time() - t, 5)
        self.assertEqual(capped.ran, [queue[0].id])
        self.assertEqual((res["limited"], res["reason"]), (1, "all lanes limited"))
        self.assertEqual([q["task_id"] for q in res["queued"]], [queue[1].id])
        self.assertEqual(res["tasks"][0]["status"], "limited")
        self.assert_events_match_spec()
        self.assert_checkouts_untouched()

    def test_unavailable_lanes_skipped_and_agent_caps_respected(self):
        gauge = Gauge()
        off = FakeLane("codex", ok=False, gauge=gauge)
        a, b = FakeLane("claude", max_agents=2, delay=0.12, gauge=gauge), FakeLane("relay", max_agents=2, delay=0.12, gauge=gauge)
        queue = swarm.build_queue([self.project(self.alpha, [f"alpha chore {i}" for i in range(3)]),
                                   self.project(self.beta, [f"beta chore {i}" for i in range(3)])])
        res = self.run_swarm(queue, [off, a, b], max_agents=3)
        self.assertEqual(res["done"], 6)
        self.assertEqual(off.ran, [])
        self.assertLessEqual(max(a.mine.peak, b.mine.peak), 2)
        self.assertEqual(gauge.peak, 3)
        self.assertTrue({r["agent"] for r in self.events("agent_start")} <= {1, 2, 3})
        self.assertEqual([l["name"] for l in self.events("swarm_start")[0]["lanes"]], ["claude", "relay"])
        skipped = next(l for l in res["lanes"] if l["name"] == "codex")
        self.assertEqual((skipped["available"], skipped["why"]), (False, "fake CLI not installed"))
        self.assert_checkouts_untouched()

    def test_pace_sets_lane_concurrency_and_replans_from_observed_burn(self):
        calls = []

        def plan_pace(usages, lanes, max_agents=12, target_pct=0.97, now=None):
            self.assertTrue(all(isinstance(l, Lane) for l in lanes))   # live lanes, so pace sees limited_until
            calls.append(("plan", usages[0].used, [l.name for l in lanes]))
            return [PacePlan("claude", "claude-max", "tokens", remaining=1e6, hours_left=10, target_rate=1e5, agents=1)]

        def adjust(current, observed_rate, target_rate, lo=1, hi=12):
            calls.append(("adjust", current, observed_rate, target_rate, lo, hi))
            return 1

        usage = Usage("claude-max", "claude", "tokens", used=1000.0, limit=2e6, resets_at=time.time() + 36000)
        lane = FakeLane("claude", max_agents=4, delay=0.08, subscription="claude-max")
        queue = swarm.build_queue([self.project(self.alpha, [f"chore {i}" for i in range(3)])])
        with mock.patch.dict(sys.modules, {"relay.pace": fake_module("relay.pace", plan_pace=plan_pace, adjust=adjust)}):
            res = self.run_swarm(queue, [lane], max_agents=4, usages=[usage], cfg={"repace_seconds": 0.05})
        self.assertEqual(res["done"], 3)
        self.assertEqual(lane.mine.peak, 1)                            # pace said one agent, not the lane's four
        paces = self.events("pace")
        self.assertGreaterEqual(len(paces), 2)
        self.assertEqual((paces[0]["lane"], paces[0]["agents"], paces[0]["used"], paces[0]["limit"]), ("claude", 1, 1000.0, 2e6))
        self.assertGreater(paces[-1]["used"], 1000.0)                  # re-plans see what the swarm burned
        self.assertIn(("plan", 1000.0, ["claude"]), calls)
        self.assertTrue([c for c in calls if c[0] == "adjust" and c[4:] == (1, 4)])        # lo=1, hi=lane cap
        start = self.events("swarm_start")[0]
        self.assertEqual((start["usages"][0]["name"], start["resets_at"]), ("claude-max", usage.resets_at))
        self.assert_events_match_spec()

    def test_each_lane_stops_at_its_own_reset_and_the_swarm_at_the_latest(self):
        now = time.time()
        gone = Usage("codex", "codex", "tokens", used=0.0, limit=1e8, resets_at=now - 1)         # already reset
        soon = Usage("claude-max", "claude", "tokens", used=0.0, limit=4e8, resets_at=now + 3600)
        late = Usage("openai", "relay", "usd", used=1.0, limit=20.0, resets_at=now + 7200)        # matched by lane
        codex, claude, relay = FakeLane("codex", subscription="codex"), FakeLane("claude", subscription="claude-max"), \
            FakeLane("relay")
        queue = swarm.build_queue([self.project(self.alpha, ["one", "two", "three"])])
        with mock.patch.dict(sys.modules, {"relay.pace": None}):                                  # caps: max_agents
            res = self.run_swarm(queue, [codex, claude, relay], max_agents=2, usages=[gone, soon, late])
        self.assertEqual((codex.ran, len(claude.ran) + len(relay.ran), res["done"]), ([], 3, 3))
        self.assertEqual(res["deadline"], late.resets_at)              # no --hours: the latest lane reset
        start = self.events("swarm_start")[0]
        self.assertEqual(start["resets_at"], soon.resets_at)           # the soonest reset still ahead
        self.assertEqual([u["resets_at"] for u in start["usages"]], [gone.resets_at, soon.resets_at, late.resets_at])
        lanes = {l["name"]: l for l in res["lanes"]}
        self.assertEqual((lanes["codex"]["past_reset"], lanes["relay"]["resets_at"]), (True, late.resets_at))
        self.assertIn("stopped at its reset", Path(res["report"]).read_text())
        self.assertEqual((swarm._stop([claude], [soon], None), swarm._stop([claude], [soon], 5.0)), (soon.resets_at, 5.0))
        self.assert_events_match_spec()

    def test_lane_paced_to_zero_while_limited_gets_work_again_when_the_limit_lifts(self):
        def plan_pace(usages, lanes, max_agents=12, target_pct=0.97, now=None):
            return [PacePlan(l.name, l.subscription, "tokens", 1e6, 10, 1e5, 0 if l.is_limited(now) else 1) for l in lanes]

        pace = fake_module("relay.pace", plan_pace=plan_pace, adjust=lambda cur, obs, tgt, lo=1, hi=12: max(lo, min(hi, 1)))
        lane = FakeLane("claude", {"first": "limited"}, limit_for=0.25, subscription="claude-max")
        usage = Usage("claude-max", "claude", "tokens", 0.0, 4e8, resets_at=time.time() + 3600)
        queue = swarm.build_queue([self.project(self.alpha, ["first task", "second task"])])
        with mock.patch.dict(sys.modules, {"relay.pace": pace}):
            res = self.run_swarm(queue, [lane], max_agents=1, usages=[usage],      # no timed re-plans: only
                                 cfg={"repace_seconds": 300, "min_limit_seconds": 0})  # on the limit and the lift
        self.assertEqual(lane.ran, [queue[0].id, queue[1].id])         # the limited task never went back to it
        self.assertEqual({t["text"]: t["status"] for t in res["tasks"]}, {"first task": "limited", "second task": "done"})
        self.assertEqual([r["agents"] for r in self.events("pace")], [1, 0, 1])   # pace saw limited_until
        self.assertEqual(res["reason"], "all lanes limited")
        self.assert_events_match_spec()

    def test_deadline_stops_launching(self):
        queue = swarm.build_queue([self.project(self.alpha, ["one", "two"])])
        lane = FakeLane("claude")
        res = self.run_swarm(queue, [lane], deadline=time.time() - 1)
        self.assertEqual((lane.ran, res["reason"], len(res["queued"])), ([], "deadline", 2))
        self.assertEqual(self.events("agent_start"), [])
        self.assertFalse((self.data / "worktrees").exists())
        self.assert_events_match_spec()

    def test_lanes_cannot_fake_swarm_events_and_garbage_results_fail(self):
        queue = swarm.build_queue([self.project(self.alpha, ["rogue task", "none task"])])
        res = self.run_swarm(queue, [FakeLane("claude", {"rogue": "rogue", "none": "none"})], max_agents=1)
        self.assertEqual({t["text"]: t["status"] for t in res["tasks"]}, {"rogue task": "done", "none task": "failed"})
        self.assertEqual(len(self.events("agent_end")), 2)
        log = self.events("lane_log")
        self.assertEqual((log[0]["agent"], log[0]["task_id"], log[0]["lane"]), (1, queue[0].id, "claude"))
        self.assert_events_match_spec()


class BuildQueue(SwarmCase):
    def test_priority_order_dedupe_caps_and_plan_mapping(self):
        plan = self.tmp / "PLAN.md"
        plan.write_text("# Week\n\n## alpha\ntest: python3 -m pytest -q\n\n- [ ] Ship the v1 release\n- [x] already done\n"
                        "- [ ] Add a --json flag\n\n## gone\nrepo: https://example.com/gone.git\n\n- [ ] clone me\n")
        pa = ProjectInfo(path=str(self.alpha), name="alpha", todos=["add a --json flag!", "Fix the flaky date test"])
        pb = ProjectInfo(path=str(self.beta), name="beta", todos=["Fix the flaky date test", "Speed up startup"])
        ideas = {str(self.beta / "src"): [Idea("Port the CLI to click", score=0.2), Idea("Cache the API client", score=0.9)],
                 "": [Idea("an idea from nowhere")]}
        generic = fake_module("relay.discover", generic_tasks=lambda p: ["Add a README usage section", "Add a CI workflow"])
        with mock.patch.dict(sys.modules, {"relay.discover": generic}):
            q = swarm.build_queue([pa, pb], ideas, str(plan), per_project=3)
            wide = swarm.build_queue([pa, pb], ideas, None, per_project=10)
            short = swarm.build_queue([pa, pb], ideas, str(plan), per_project=3, limit=2)
        self.assertEqual([(t.project.name, t.source, t.text) for t in q], [
            ("alpha", "plan", "Ship the v1 release"), ("alpha", "plan", "Add a --json flag"),
            ("beta", "todo", "Fix the flaky date test"), ("alpha", "todo", "Fix the flaky date test"),
            ("beta", "todo", "Speed up startup"), ("beta", "idea", "Cache the API client")])
        for t in q:
            self.assertEqual(t.id, f"{t.project.slug}-{hashlib.sha1(t.text.encode()).hexdigest()[:6]}")
        self.assertEqual([t.priority for t in q], sorted((t.priority for t in q), reverse=True))
        self.assertEqual({t.project.test_cmd for t in q if t.project.name == "alpha"}, {"python3 -m pytest -q"})
        self.assertIsNone(pa.test_cmd)                                 # the caller's objects are not mutated
        self.assertEqual([t.id for t in short], [t.id for t in q[:2]])
        order = [swarm.SOURCES.index(t.source) for t in wide]
        self.assertEqual(order, sorted(order))
        self.assertEqual([t.text for t in wide if t.source == "generic"], ["Add a README usage section"] * 2 + ["Add a CI workflow"] * 2)
        self.assertNotIn("an idea from nowhere", [t.text for t in wide])

    def test_same_name_repos_get_distinct_ids(self):
        p1 = ProjectInfo(path=str(self.tmp / "a" / "api"), name="api", todos=["add tests"])
        p2 = ProjectInfo(path=str(self.tmp / "b" / "api"), name="api", todos=["add tests"])
        q = swarm.build_queue([p1, p2])
        self.assertEqual(len({t.id for t in q}), 2)
        self.assertTrue(q[1].id.endswith("-2"))


class BurnWeek(SwarmCase):
    def test_dry_run_plans_without_worktrees_then_runs_and_reports(self):
        pa = self.project(self.alpha, ["add feature one"])
        found, usages, week_end = [], [], time.time() + 3600
        mods = {"relay.discover": fake_module("relay.discover", generic_tasks=lambda p: [],
                                              discover_projects=lambda roots, **kw: found.append(roots) or [pa]),
                "relay.usage": fake_module("relay.usage", weekly_usage=lambda cfg, ledger=None, now=None: list(usages),
                                           week_bounds=lambda reset="mon 09:00", tz=None, now=None: (0.0, week_end)),
                "relay.ideas": fake_module("relay.ideas", mine=mock.Mock(return_value={})),
                "relay.monid": fake_module("relay.monid", enricher=lambda cfg: None),
                "relay.pace": None}
        lane = FakeLane("claude")
        cfg = {"roots": [str(self.tmp)], "per_project": 2, "tick": 0.01, "ideas": {"enabled": False}}
        with mock.patch.dict(sys.modules, mods):
            plan = swarm.burn_week(cfg, dry_run=True, data_dir=str(self.data), lanes=[lane])
            self.assertTrue(plan["dry_run"])
            self.assertEqual(plan["deadline"], week_end)                # no subscription known: the configured week
            self.assertEqual([q["text"] for q in plan["queue"]], ["add feature one"])
            self.assertEqual(plan["lanes"][0]["available"], True)
            self.assertEqual((lane.ran, self.bus.rows), ([], []))
            self.assertFalse((self.data / "worktrees").exists())
            mods["relay.ideas"].mine.assert_not_called()
            usages.append(Usage("claude", "claude", "tokens", 0.0, 1e6, resets_at=time.time() + 7200))
            self.assertEqual(swarm.burn_week(cfg, dry_run=True, data_dir=str(self.data), lanes=[lane])["deadline"],
                             usages[0].resets_at)                       # the lane's own reset, not the week's
            res = swarm.burn_week(cfg, data_dir=str(self.data), lanes=[lane], bus=self.bus, ideas=True, max_agents=2,
                                  hours=0.5)
        mods["relay.ideas"].mine.assert_called_once()
        self.assertEqual(found[0], [str(self.tmp)])
        self.assertEqual((res["done"], res["reason"], len(res["projects"])), (1, "queue empty", 1))
        self.assertAlmostEqual(res["deadline"], time.time() + 1800, delta=60)    # --hours wins
        self.assertIn("1 repos", Path(res["report"]).read_text())
        self.assert_events_match_spec()
        self.assert_checkouts_untouched()


class Report(SwarmCase):
    def test_report_redacts_and_never_says_push(self):
        usage = Usage("claude-max", "claude", "tokens", used=1e8, limit=4e8, resets_at=time.time() + 3600)
        result = {"started": time.time(), "done": 1, "blocked": 1, "tokens": 1234, "usd": 0.5, "seconds": 75,
                  "reason": "queue empty", "max_agents": 4,
                  "lanes": [{"name": "claude", "as": "claude", "subscription": "claude-max", "tasks": 2, "tokens": 1234,
                             "usd": 0.5, "available": True},
                            {"name": "codex", "as": "codex", "subscription": "codex", "available": False,
                             "why": "codex CLI not found"}],
                  "tasks": [{"task_id": "alpha-1", "project": "alpha", "repo": str(self.alpha), "text": "ship | it",
                             "source": "todo", "lane": "claude", "status": "done", "branch": "relay/burn/alpha-1",
                             "summary": "used key sk-ant-api03-abcdefghijklmnopqrstuvwxyz", "commit": "abc1234",
                             "diffstat": "1 file changed, 2 insertions(+)", "tests_ok": True, "tokens": 1000,
                             "usd": 0.4, "seconds": 60},
                            {"task_id": "alpha-2", "project": "alpha", "repo": str(self.alpha), "text": "publish",
                             "source": "plan", "lane": "claude", "status": "blocked", "branch": "relay/burn/alpha-2",
                             "summary": "BLOCKED: add a token, then git push origin main", "commit": None,
                             "tests_ok": None}],
                  "queued": [{"task_id": "beta-1", "project": "beta", "text": "later", "source": "idea"}],
                  "usages": [to_dict(usage)], "usages_now": [to_dict(Usage("claude-max", "claude", "tokens", 2e8, 4e8, usage.resets_at))]}
        text = Path(swarm.write_report(result, str(self.tmp / "out" / "BURN.md"))).read_text()
        self.assertNotIn("sk-ant", text)
        self.assertIn("[redacted]", text)
        self.assertNotIn("push", text.lower())
        for part in ("git merge relay/burn/alpha-1", "git branch -D relay/burn/alpha-2", "## Needs you",
                     "## Still queued", "| claude |", "skipped: codex CLI not found", "25% → 50% used of 400.0M tokens"):
            self.assertIn(part, text)


if __name__ == "__main__":
    unittest.main()
