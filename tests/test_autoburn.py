"""relay.autoburn: per-plan end-of-week detection (each subscription on its own reset clock), the gates, and run()
handing burn_week only the ready plans' lanes.

Offline: fake relay.usage and relay.swarm modules, a temp HOME / data_dir / ledger, PATH with nothing on it, and
an injected `now` (the CLI tests use clock-relative usages and an always-idle schedule instead)."""
import argparse
import io
import json
import os
import plistlib
import shutil
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import relay.config  # noqa: F401  (imported before any sys.modules patching)
from relay import autoburn, schedule
from relay.bus import Bus
from relay.contracts import Usage

LA, UTC = ZoneInfo("America/Los_Angeles"), ZoneInfo("UTC")


def la(*a) -> float:
    return datetime(*a, tzinfo=LA).timestamp()


W0, W1 = la(2026, 10, 5, 9), la(2026, 10, 12, 9)          # the configured week: Mon 09:00 -> Mon 09:00
SUN_NOON = la(2026, 10, 11, 12)                           # 21h before W1, a free weekend afternoon


def U(name: str, used: float, resets_at: float, limit: float | None = 100, unit: str = "tokens",
      note: str = "") -> Usage:
    return Usage(name, "claude", unit, used, limit, resets_at, "transcripts", note)


def config(tmp: str, **auto) -> dict:
    return {"week": {"reset": "mon 09:00", "timezone": "America/Los_Angeles"},
            "idle": {"weekday": "18:00-09:00", "weekend": "all"}, "data_dir": os.path.join(tmp, "data"),
            "autoburn": {"enabled": True, **auto},
            "subscriptions": [{"name": "claude-max", "kind": "claude_code", "weekly_tokens": 100},
                              {"name": "codex", "kind": "codex", "weekly_tokens": 100},
                              {"name": "seat-b", "kind": "claude_code", "weekly_tokens": 100, "reset": "thu 14:00",
                               "reserve_pct": 30},
                              {"name": "openai", "kind": "api_budget", "monthly_usd": 20},
                              {"name": "agent37", "kind": "agent37_budget", "monthly_usd": 20},
                              {"name": "monid", "kind": "monid_credits", "credits": 1000}],
            "lanes": [{"kind": "claude", "name": "claude", "subscription": "claude-max"},
                      {"kind": "codex", "name": "codex", "subscription": "codex"},
                      {"kind": "claude", "name": "claude-b", "subscription": "seat-b"},
                      {"kind": "relay", "name": "relay", "subscription": "openai"},
                      {"kind": "agent37", "name": "agent37", "subscription": "agent37"},
                      {"kind": "orca", "name": "orca", "max_agents": 4, "enabled": False}]}


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="relay-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home, self.bin = os.path.join(self.tmp, "home"), os.path.join(self.tmp, "bin")
        os.makedirs(self.home)
        os.makedirs(self.bin)
        self.ledger = os.path.join(self.tmp, ".relay", "events.jsonl")
        env = {"HOME": self.home, "PATH": self.bin, "SUPABASE_URL": "", "SUPABASE_KEY": "",
               "SUPABASE_SERVICE_ROLE_KEY": ""}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        self.usages, self.burns, self.burn_error, self.w0, self.w1 = [], [], None, W0, W1
        usage, swarm = types.ModuleType("relay.usage"), types.ModuleType("relay.swarm")
        usage.week_bounds = lambda reset="mon 09:00", tz=None, now=None: (self.w0, self.w1)
        usage.weekly_usage = lambda cfg, ledger=".relay/events.jsonl", now=None: list(self.usages)
        swarm.burn_week = self.burn_week
        mods = mock.patch.dict(sys.modules, {"relay.usage": usage, "relay.swarm": swarm})
        mods.start()
        self.addCleanup(mods.stop)
        self.cfg = config(self.tmp)
        self.data = Path(self.cfg["data_dir"])

    def burn_week(self, cfg, **kw):
        self.burns.append((cfg, kw))
        if self.burn_error:
            raise self.burn_error
        if kw.get("dry_run"):
            return {"dry_run": True, "queue": [{"task_id": "a"}, {"task_id": "b"}], "schedule": "claude-max: 2 agents"}
        return {"done": 2, "blocked": 0, "failed": 0, "reason": "queue empty", "report": "/x/BURN.md"}

    def decide(self, now=SUN_NOON, cfg=None):
        return autoburn.decide(cfg or self.cfg, self.usages, now, ledger=self.ledger)

    def events(self) -> list[dict]:
        return schedule.ledger_rows(self.ledger)


class Decide(Sandbox):
    def test_each_plan_on_its_own_clock(self):
        self.usages = [U("claude-max", 40, W1), U("codex", 80, W1), U("seat-b", 10, la(2026, 10, 15, 14)),
                       U("openai", 2, la(2026, 11, 1, 0) + 7 * 3600, 20, "usd")]
        d = self.decide()
        p = {x.name: x for x in d.plans}
        self.assertTrue(d.fire)
        self.assertEqual(d.reason, "burning claude-max until Mon 09:00")
        self.assertEqual((d.lanes, d.burn_hours, d.hours_left, d.target_pct), (["claude"], 21.0, 21.0, 0.85))
        self.assertEqual(d.plans[0].name, "claude-max")
        cm = p["claude-max"]
        self.assertEqual((cm.fire, cm.ready, cm.resets_at, cm.deadline, cm.hours_left), (True, True, W1, W1, 21.0))
        self.assertAlmostEqual(cm.projected_used, 40 / (147 / 168), places=1)          # used ÷ elapsed share of week
        self.assertAlmostEqual(cm.leftover, 0.85 - 0.4571, places=3)
        self.assertEqual(cm.reason, "39% of its limit would expire above the 15% reserve")
        self.assertTrue(p["codex"].reason.startswith("on pace to use it"))
        self.assertEqual(p["seat-b"].reason, "resets in 98h; autoburn starts 36h before")
        self.assertEqual(p["openai"].reason, "pay-as-you-go: spending it is new money")
        self.assertFalse(any(x.fire for x in d.plans if x.name != "claude-max"))

    def test_deadline_is_the_next_work_start_when_that_comes_first(self):
        self.usages = [U("claude-max", 10, W1), U("seat-b", 10, la(2026, 10, 9, 14))]   # seat-b: Fri 14:00
        d = self.decide(now=la(2026, 10, 8, 20))                                       # Thu 20:00, off work
        seat = d.plans[0]
        self.assertEqual((seat.name, seat.fire, seat.deadline), ("seat-b", True, la(2026, 10, 9, 9)))
        self.assertEqual((d.burn_hours, d.target_pct, d.lanes), (13.0, 0.7, ["claude-b"]))   # its own 30% reserve
        self.assertEqual(d.reason, "burning seat-b until Fri 09:00")
        self.assertIn("resets in 85h", d.plans[1].reason)

    def test_most_urgent_first_each_with_its_own_deadline(self):
        self.usages = [U("claude-max", 10, W1), U("seat-b", 30, la(2026, 10, 11, 17))]   # seat-b resets in 5h
        d = self.decide()
        self.assertEqual([p.name for p in d.plans], ["seat-b", "claude-max"])
        self.assertTrue(all(p.fire for p in d.plans))
        self.assertGreater(d.plans[0].urgency, d.plans[1].urgency)
        self.assertEqual([p.deadline for p in d.plans], [la(2026, 10, 11, 17), W1])
        self.assertEqual((d.lanes, d.hours_left, d.burn_hours, d.target_pct), (["claude-b", "claude"], 5.0, 21.0, 0.7))
        bc = autoburn.burn_config(self.cfg, d)
        self.assertEqual([(s["name"], s.get("enabled", True)) for s in bc["lanes"]],
                         [("claude-b", True), ("claude", True), ("codex", False), ("relay", False),
                          ("agent37", False), ("orca", False)])
        self.assertEqual(bc["week"], {"reset": "mon 09:00", "timezone": "America/Los_Angeles", "target_pct": 0.7})
        self.assertNotIn("target_pct", self.cfg["week"])                               # the caller's cfg is untouched

    def test_waits_for_the_work_day_to_end(self):
        self.usages = [U("claude-max", 10, la(2026, 10, 7, 20))]                       # resets Wed 20:00
        d = self.decide(now=la(2026, 10, 7, 10))                                       # Wed 10:00, at work
        self.assertFalse(d.fire)
        self.assertEqual((d.plans[0].ready, d.plans[0].fire), (True, False))
        self.assertIn("waiting for your work day to end (Wed 18:00)", d.reason)
        cfg = config(self.tmp, allow_work_hours=True)
        d = self.decide(now=la(2026, 10, 7, 10), cfg=cfg)
        self.assertTrue(d.fire)
        self.assertEqual((d.burn_hours, d.plans[0].deadline), (10.0, la(2026, 10, 7, 20)))

    def test_global_gates(self):
        self.usages = [U("claude-max", 40, W1)]
        d = self.decide(cfg=config(self.tmp, enabled=False))
        self.assertEqual((d.fire, d.reason, d.plans[0].ready), (False, "autoburn is off: set autoburn.enabled", True))
        self.data.mkdir(parents=True)
        lock = self.data / "autoburn.lock"
        lock.write_text(json.dumps({"pid": os.getpid()}))
        self.assertEqual(self.decide().reason, f"an autoburn is already running (pid {os.getpid()})")
        lock.write_text("")                                                            # being written right now
        self.assertEqual(self.decide().reason, "an autoburn is already running")
        lock.write_text(json.dumps({"pid": 99999999}))                                 # its process is gone
        self.assertTrue(self.decide().fire)

    def test_plan_level_reasons(self):
        os.makedirs(os.path.dirname(self.ledger))
        Path(self.ledger).write_text(json.dumps({"event": "lane_limited", "lane": "claude",
                                                 "until": la(2026, 10, 12, 10), "reason": "usage limit"}) + "\n")
        self.data.mkdir(parents=True)
        (self.data / "autoburn.json").write_text(json.dumps({"fired": {"codex": W1}}))
        self.usages = [U("claude-max", 10, W1), U("codex", 10, W1), U("seat-b", 10, W1, limit=None),
                       U("monid", 10, W1), U("agent37", 0, SUN_NOON, note="error: boom")]
        p = {x.name: x for x in self.decide().plans}
        self.assertEqual(p["claude-max"].reason, "lanes limited until Mon 10:00")
        self.assertEqual(p["codex"].reason, "autoburned this window, until Mon 09:00")
        self.assertEqual(p["seat-b"].reason, "no known limit")
        self.assertEqual(p["monid"].reason, "no enabled lane burns it")
        self.assertEqual(p["agent37"].reason, "reset unknown (error: boom)")
        self.usages = [U("seat-b", 10, la(2026, 10, 12, 12))]                            # its lane isn't limited
        d = self.decide(now=la(2026, 10, 12, 8, 30), cfg=config(self.tmp + "/other"))  # Mon 08:30, work at 09:00
        self.assertEqual(d.plans[0].reason, "only 0.5h before work starts")
        self.assertIn("nothing to burn yet: soonest reset is seat-b in 4h", d.reason)

    def test_monthly_budget_projects_over_its_month(self):
        now = datetime(2026, 10, 31, 12, tzinfo=UTC).timestamp()                     # Sat 05:00 in LA, 12h to reset
        nov1 = datetime(2026, 11, 1, tzinfo=UTC).timestamp()
        self.usages = [U("agent37", 10, nov1, 20, "usd")]
        p = self.decide(now=now).plans[0]
        self.assertTrue(p.fire)
        self.assertAlmostEqual(p.projected_used, 10 / (30.5 / 31), places=1)
        self.assertEqual((p.lanes, p.deadline), (["agent37"], nov1))
        self.usages = [U("agent37", 18, nov1, 20, "usd")]
        self.assertTrue(self.decide(now=now).plans[0].reason.startswith("on pace to use it"))

    def test_report_has_one_row_per_plan(self):
        self.usages = [U("claude-max", 40, W1), U("codex", 80, W1), U("seat-b", 10, la(2026, 10, 15, 14))]
        text = autoburn.report(self.decide(), LA)
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("autoburn · FIRE  burning claude-max until Mon 09:00"))
        for name in ("claude-max", "codex", "seat-b"):
            self.assertEqual(sum(l.lstrip().startswith(name) for l in lines), 1)
        self.assertIn("burn until Mon 09:00", text)
        self.assertIn("lanes claude · aiming at 85%", lines[-1])


class Run(Sandbox):
    def test_fires_logs_and_records(self):
        self.usages = [U("claude-max", 40, W1), U("codex", 80, W1)]
        d = autoburn.run(self.cfg, SUN_NOON, ledger=self.ledger)
        self.assertTrue(d.fire)
        (cfg, kw), = self.burns
        self.assertEqual(kw["hours"], 21.0)
        self.assertIsInstance(kw["bus"], Bus)
        self.assertFalse(kw.get("dry_run"))
        self.assertEqual(cfg["week"]["target_pct"], 0.85)
        self.assertEqual([s["name"] for s in cfg["lanes"] if s.get("enabled", True)], ["claude"])
        self.assertEqual(d.result["done"], 2)
        check, fire = self.events()
        self.assertEqual((check["event"], check["fire"], check["dry_run"]), ("autoburn_check", True, False))
        self.assertEqual([p["name"] for p in check["plans"]], ["claude-max", "codex"])
        self.assertEqual(fire["event"], "autoburn_fire")
        self.assertEqual([(p["name"], p["deadline"]) for p in fire["plans"]], [("claude-max", W1)])
        self.assertEqual(json.loads((self.data / "autoburn.json").read_text())["fired"], {"claude-max": W1})
        self.assertFalse((self.data / "autoburn.lock").exists())
        again = autoburn.run(self.cfg, SUN_NOON + 3600, ledger=self.ledger)            # an hour later: no re-fire
        self.assertFalse(again.fire)
        self.assertEqual(again.plans[0].reason, "autoburned this window, until Mon 09:00")
        self.assertEqual(len(self.burns), 1)
        self.assertEqual([e["event"] for e in self.events()], ["autoburn_check", "autoburn_fire", "autoburn_check"])

    def test_dry_run_only_asks_for_the_plan(self):
        self.usages = [U("claude-max", 40, W1)]
        d = autoburn.run(self.cfg, SUN_NOON, dry_run=True, ledger=self.ledger)
        (_, kw), = self.burns
        self.assertTrue(kw["dry_run"])
        self.assertEqual(d.result["queue"], [{"task_id": "a"}, {"task_id": "b"}])
        self.assertEqual([(e["event"], e["dry_run"]) for e in self.events()], [("autoburn_check", True)])
        self.assertFalse(self.data.exists())                                           # no lock, no state

    def test_not_firing_never_calls_the_swarm(self):
        self.usages = [U("claude-max", 40, W1)]
        d = autoburn.run(config(self.tmp, enabled=False), SUN_NOON, ledger=self.ledger)
        self.assertFalse(d.fire)
        self.assertEqual(self.burns, [])
        self.assertEqual([(e["event"], e["fire"]) for e in self.events()], [("autoburn_check", False)])

    def test_a_failed_burn_releases_the_lock_and_is_not_retried(self):
        self.usages, self.burn_error = [U("claude-max", 40, W1)], RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            autoburn.run(self.cfg, SUN_NOON, ledger=self.ledger)
        self.assertFalse((self.data / "autoburn.lock").exists())
        self.assertFalse(autoburn.run(self.cfg, SUN_NOON + 3600, ledger=self.ledger).fire)
        self.assertEqual(len(self.burns), 1)

    def test_losing_the_lock_race_fires_nothing(self):
        self.usages = [U("claude-max", 40, W1)]
        self.data.mkdir(parents=True)
        (self.data / "autoburn.lock").write_text(json.dumps({"pid": os.getpid()}))
        with mock.patch.object(autoburn, "running", side_effect=[None, os.getpid()]):
            d = autoburn.run(self.cfg, SUN_NOON, ledger=self.ledger)
        self.assertEqual((d.fire, d.reason), (False, "another autoburn started first"))
        self.assertEqual(self.burns, [])


class JobAndCli(Sandbox):
    def setUp(self):
        super().setUp()
        now = time.time()
        self.w0, self.w1 = now - 7 * 86400 + 20 * 3600, now + 20 * 3600
        self.usages = [U("claude-max", 40, self.w1), U("codex", 80, self.w1)]
        self.cfg["idle"] = {"timezone": "America/Los_Angeles", "weekday": "all", "weekend": "all"}   # always off

    def cli(self, *argv, cfg=None) -> tuple[int, str]:
        path = os.path.join(self.tmp, "burner-week.json")
        Path(path).write_text(json.dumps(cfg or self.cfg))
        ap = argparse.ArgumentParser()
        ap.add_argument("-C", "--cwd", default=".")
        burn = ap.add_subparsers(dest="cmd").add_parser("burn").add_subparsers(dest="action")
        autoburn.add_cli(burn)
        out = io.StringIO()
        with redirect_stdout(out):
            args = ap.parse_args(["-C", self.tmp, "burn", "auto", *argv, "-b", path])
            return args.func(args), out.getvalue()

    def test_job(self):
        j = autoburn.job(self.cfg, "/c/burner-week.json", self.tmp)
        self.assertEqual((j.label, j.argv, j.interval, j.calendar),
                         ("com.relay.autoburn", ["burn", "auto", "--check", "-b", "/c/burner-week.json"], 3600, []))
        self.assertEqual(j.precheck, ["burn", "auto", "--check", "--detach", "-b", "/c/burner-week.json"])
        d = plistlib.loads(schedule.plist(j).encode())
        self.assertEqual(d["StartInterval"], 3600)
        self.assertEqual(d["ProgramArguments"][1:6], ["-m", "relay", "burn", "auto", "--check"])

    def test_show_check_and_detach(self):
        rc, out = self.cli()
        self.assertEqual(rc, 0)
        self.assertIn("autoburn · FIRE", out)
        self.assertEqual((self.burns, self.events()), ([], []))                      # showing decides only
        rc, out = self.cli("--check")
        self.assertEqual(rc, 0)
        self.assertIn("done 2 · blocked 0 · failed 0 · reason queue empty", out)
        self.assertEqual(len(self.burns), 1)
        (self.data / "autoburn.json").unlink()
        with mock.patch("relay.autoburn.subprocess.Popen") as popen:
            popen.return_value.pid = 4242
            rc, out = self.cli("--detach")
            self.assertEqual(rc, 0)
            self.assertIn("burn started in the background (pid 4242)", out)
            argv = popen.call_args[0][0]
            self.assertEqual(argv[:7], [sys.executable, "-m", "relay", "burn", "auto", "--check", "-b"])
            self.assertTrue(popen.call_args[1]["start_new_session"])
            rc, out = self.cli("--detach", cfg=config(self.tmp, enabled=False))
            self.assertEqual(rc, 1)
            self.assertEqual(popen.call_count, 1)

    def test_install_dry_run_and_status(self):
        rc, out = self.cli("--install", "--dry-run", "--via", "launchd", cfg=config(self.tmp, enabled=False))
        self.assertEqual(rc, 0)
        self.assertIn("autoburn.enabled is false", out)
        self.assertIn("<key>StartInterval</key>", out)
        self.assertIn("<integer>3600</integer>", out)
        rc, out = self.cli("--install", "--dry-run", "--via", "orca")
        self.assertIn("automations create --name com.relay.autoburn --trigger hourly", out)
        self.assertIn("burn auto --check --detach", out)
        rc, out = self.cli("--status", "--via", "launchd")
        self.assertIn("not installed", out)
        self.assertFalse((Path(self.home) / "Library").exists())


if __name__ == "__main__":
    unittest.main()
