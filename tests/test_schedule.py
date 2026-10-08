"""relay.schedule: work hours from the idle block, reserves, and job installs (launchd / Orca / cron).

Offline: a temp HOME, PATH holding only fake launchctl / orca / crontab scripts that log their argv, and an
injected `now`. Nothing touches the real ~/Library/LaunchAgents, crontab or Orca."""
import argparse
import io
import os
import plistlib
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import relay.config  # noqa: F401  (imported before any sys.modules patching)
from relay import schedule

LA, UTC = ZoneInfo("America/Los_Angeles"), ZoneInfo("UTC")
CFG = {"idle": {"timezone": "America/Los_Angeles", "weekday": "18:00-09:00", "weekend": "all"}}


def la(*a) -> datetime:
    return datetime(*a, tzinfo=LA)


def fake_bin(d: str, name: str, body: str = "") -> None:
    """An executable that logs `name [arg]...` to $FAKE_LOG, then runs `body` (shell builtins only)."""
    p = Path(d) / name
    p.write_text(f'#!/bin/sh\nprintf "%s" "{name}" >> "$FAKE_LOG"\n'
                 'for a in "$@"; do printf " [%s]" "$a" >> "$FAKE_LOG"; done\n'
                 'printf "\\n" >> "$FAKE_LOG"\n' + body + "\n")
    p.chmod(0o755)


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="relay-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin, self.home, self.log = (os.path.join(self.tmp, x) for x in ("bin", "home", "calls.log"))
        os.makedirs(self.bin)
        os.makedirs(self.home)
        env = {"HOME": self.home, "PATH": self.bin, "FAKE_LOG": self.log, "ORCA_BIN": os.path.join(self.bin, "orca"),
               "SUPABASE_URL": "", "SUPABASE_KEY": "", "SUPABASE_SERVICE_ROLE_KEY": ""}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)

    def calls(self) -> list[str]:
        return Path(self.log).read_text().splitlines() if os.path.exists(self.log) else []


class WorkHours(unittest.TestCase):
    def test_weekday_daytime_is_work(self):
        now = la(2026, 10, 7, 10)                                   # Wed 10:00
        self.assertTrue(schedule.in_work_hours(CFG, now))
        self.assertEqual(schedule.next_free_time(CFG, now), la(2026, 10, 7, 18))
        self.assertEqual(schedule.next_work_start(CFG, now), now)

    def test_evenings_and_weekends_are_free(self):
        now = la(2026, 10, 7, 20)                                   # Wed 20:00
        self.assertFalse(schedule.in_work_hours(CFG, now))
        self.assertEqual(schedule.next_free_time(CFG, now), now)
        self.assertEqual(schedule.next_work_start(CFG, now), la(2026, 10, 8, 9))
        self.assertEqual(schedule.next_work_start(CFG, la(2026, 10, 10, 12)), la(2026, 10, 12, 9))   # Sat -> Mon
        self.assertFalse(schedule.in_work_hours(CFG, la(2026, 10, 12, 8)))                           # Mon 08:00
        self.assertTrue(schedule.in_work_hours(CFG, la(2026, 10, 12, 9, 30)))

    def test_epoch_now_and_timezone_fallbacks(self):
        cfg = {"idle": {"weekday": "18:00-09:00", "weekend": "all"}, "week": {"timezone": "America/Los_Angeles"}}
        self.assertEqual(schedule.tz_name(cfg), "America/Los_Angeles")
        self.assertEqual(schedule.tz_name(cfg, {"timezone": "Asia/Tokyo"}), "Asia/Tokyo")
        self.assertTrue(schedule.in_work_hours(cfg, la(2026, 10, 7, 10).timestamp()))
        self.assertFalse(schedule.in_work_hours(cfg, la(2026, 10, 7, 19).timestamp()))

    def test_default_idle_is_capacitys(self):
        cfg = {"week": {"timezone": "America/Los_Angeles"}}         # weekdays 23:00-07:00, weekends idle
        self.assertTrue(schedule.in_work_hours(cfg, la(2026, 10, 7, 22)))
        self.assertFalse(schedule.in_work_hours(cfg, la(2026, 10, 7, 23, 30)))
        self.assertFalse(schedule.in_work_hours(cfg, la(2026, 10, 10, 15)))


class Reserve(unittest.TestCase):
    def test_default_global_and_per_subscription(self):
        subs = [{"name": "a"}, {"name": "b", "reserve_pct": 30}, {"name": "c", "reserve_pct": 0.05}]
        self.assertEqual(schedule.reserve_pct({}, "x"), 0.15)
        self.assertAlmostEqual(schedule.target_pct({}, "x"), 0.85)
        cfg = {"reserve_pct": 0.2, "subscriptions": subs}
        self.assertEqual(schedule.reserve_pct(cfg, "a"), 0.2)            # global
        self.assertAlmostEqual(schedule.reserve_pct(cfg, "b"), 0.30)     # percent, as capacity.py writes it
        self.assertEqual(schedule.reserve_pct(cfg, "c"), 0.05)
        self.assertAlmostEqual(schedule.target_pct(cfg, "b"), 0.70)
        self.assertEqual(schedule.reserve_pct({"reserve_pct": 250}, "z"), 1.0)


class Redaction(unittest.TestCase):
    def test_emit_redacts_nested_strings(self):
        rows = []
        tel = type("Tel", (), {"emit": lambda self, event, **d: rows.append((event, d)) or d})()
        schedule.emit(tel, "x", note="key sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA", n=3,
                      plans=[{"why": "password=hunter2hunter2"}])
        text = repr(rows)
        self.assertNotIn("AAAAAAAAAAAAAAAA", text)
        self.assertNotIn("hunter2hunter2", text)
        self.assertEqual(rows[0][1]["n"], 3)


def job(cwd: str, **kw) -> schedule.Job:
    base = dict(label="com.relay.windows.codex",
                argv=["windows", "prime", "--plan", "codex", "-b", "/c/burner-week.json"],
                calendar=[(d, 5, 0) for d in range(5)], timezone="America/Los_Angeles", cwd=cwd,
                precheck=["windows", "due", "--plan", "codex", "-b", "/c/burner-week.json"], provider="codex")
    return schedule.Job(**{**base, **kw})


class Generate(Sandbox):
    def test_label_and_default_via(self):
        self.assertEqual(schedule.label("windows", "Claude A"), "com.relay.windows.claude-a")
        self.assertEqual(schedule.label("autoburn"), "com.relay.autoburn")
        with mock.patch("platform.system", return_value="Darwin"):
            self.assertEqual(schedule.default_via(), "launchd")
        with mock.patch("platform.system", return_value="Linux"):
            self.assertEqual(schedule.default_via(), "cron")

    def test_plist_calendar(self):
        d = plistlib.loads(schedule.plist(job(self.tmp, log="/l/codex.log"), local=LA).encode())
        self.assertEqual(d["Label"], "com.relay.windows.codex")
        self.assertEqual(d["ProgramArguments"][:3], [sys.executable, "-m", "relay"])
        self.assertEqual(d["ProgramArguments"][3:6], ["windows", "prime", "--plan"])
        self.assertEqual(d["StartCalendarInterval"], [{"Weekday": w, "Hour": 5, "Minute": 0} for w in range(1, 6)])
        self.assertNotIn("StartInterval", d)
        self.assertEqual(d["WorkingDirectory"], os.path.abspath(self.tmp))
        self.assertEqual(d["EnvironmentVariables"], {"PATH": self.bin, "PYTHONPATH": schedule.ROOT})
        self.assertEqual(d["StandardOutPath"], "/l/codex.log")
        self.assertFalse(d["RunAtLoad"])

    def test_plist_converts_to_the_machine_clock(self):
        tokyo = job(self.tmp, calendar=[(0, 5, 0)], timezone="Asia/Tokyo")          # Mon 05:00 JST = Sun 20:00 UTC
        d = plistlib.loads(schedule.plist(tokyo, local=UTC).encode())
        self.assertEqual(d["StartCalendarInterval"], [{"Weekday": 0, "Hour": 20, "Minute": 0}])
        la_job = job(self.tmp, calendar=[(2, 5, 0)])                                  # Wed 05:00 PDT = 12:00 UTC
        d = plistlib.loads(schedule.plist(la_job, local=UTC, now=la(2026, 10, 7)).encode())
        self.assertEqual(d["StartCalendarInterval"], [{"Weekday": 3, "Hour": 12, "Minute": 0}])

    def test_plist_interval(self):
        d = plistlib.loads(schedule.plist(schedule.Job("com.relay.autoburn", ["burn", "auto", "--check"])).encode())
        self.assertEqual(d["StartInterval"], 3600)
        self.assertNotIn("StartCalendarInterval", d)

    def test_cron_lines(self):
        line, = schedule.cron_lines(job(self.tmp, log="/l/x.log"), local=LA)
        self.assertTrue(line.startswith("0 5 * * 1,2,3,4,5 cd "), line)
        self.assertIn(f"exec {sys.executable} -m relay windows prime --plan codex -b /c/burner-week.json", line)
        self.assertIn("PYTHONPATH=", line)
        self.assertTrue(line.endswith(">> /l/x.log 2>&1  # com.relay.windows.codex"), line)
        hourly, = schedule.cron_lines(schedule.Job("com.relay.autoburn", ["burn", "auto", "--check"]))
        self.assertTrue(hourly.startswith("0 * * * * cd "), hourly)

    def test_orca_argv_follows_automations_create(self):
        argv = schedule.orca_argv(job(self.tmp))
        self.assertEqual(argv[:3], [os.path.join(self.bin, "orca"), "automations", "create"])
        opts = dict(zip(argv[3::2], argv[4::2]))
        self.assertEqual(opts["--name"], "com.relay.windows.codex")
        self.assertEqual(opts["--trigger"], "0 5 * * 1,2,3,4,5")
        self.assertEqual(opts["--timezone"], "America/Los_Angeles")
        self.assertEqual(opts["--provider"], "codex")
        self.assertEqual(opts["--prompt"], "Reply with OK.")
        self.assertEqual(opts["--repo"], "path:" + os.path.abspath(self.tmp))
        self.assertIn("-m relay windows due --plan codex", opts["--precheck"])
        self.assertTrue(opts["--precheck"].startswith("/bin/sh -c "))
        self.assertEqual(argv[-2:], ["--enabled", "--json"])
        hourly = schedule.orca_argv(schedule.Job("com.relay.autoburn", ["burn", "auto", "--check"]))
        self.assertEqual(hourly[hourly.index("--trigger") + 1], "hourly")
        with self.assertRaises(ValueError):
            schedule.orca_argv(job(self.tmp, calendar=[(0, 5, 0), (1, 6, 0)]))


class Launchd(Sandbox):
    def setUp(self):
        super().setUp()
        fake_bin(self.bin, "launchctl")
        self.job = job(self.tmp, log=os.path.join(self.tmp, "logs", "codex.log"))
        self.path = Path(self.home) / "Library" / "LaunchAgents" / "com.relay.windows.codex.plist"
        self.domain = f"gui/{os.getuid()}"

    def test_dry_run_writes_and_calls_nothing(self):
        ok, msg = schedule.install(self.job, "launchd", dry_run=True, local=LA)
        self.assertTrue(ok)
        self.assertIn("would write", msg)
        self.assertIn("<key>StartCalendarInterval</key>", msg)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.calls(), [])

    def test_install_status_uninstall(self):
        ok, msg = schedule.install(self.job, "launchd", local=LA)
        self.assertTrue(ok, msg)
        self.assertEqual(plistlib.loads(self.path.read_bytes())["Label"], "com.relay.windows.codex")
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "logs")))
        self.assertEqual(self.calls(), [f"launchctl [bootout] [{self.domain}/com.relay.windows.codex]",
                                        f"launchctl [bootstrap] [{self.domain}] [{self.path}]"])
        st = schedule.status("com.relay.windows.codex", "launchd")
        self.assertEqual((st["installed"], st["loaded"]), (True, True))
        ok, msg = schedule.uninstall("com.relay.windows.codex", "launchd")
        self.assertTrue(ok)
        self.assertFalse(self.path.exists())
        self.assertIn(f"launchctl [bootout] [{self.domain}/com.relay.windows.codex]", self.calls()[-2:])
        self.assertFalse(schedule.status("com.relay.windows.codex", "launchd")["installed"])

    def test_bootstrap_failure_is_reported(self):
        fake_bin(self.bin, "launchctl", '[ "$1" = bootstrap ] && { echo "Bootstrap failed: 5" >&2; exit 5; }\nexit 0')
        ok, msg = schedule.install(self.job, "launchd", local=LA)
        self.assertFalse(ok)
        self.assertIn("Bootstrap failed: 5", msg)


class Orca(Sandbox):
    def setUp(self):
        super().setUp()
        fake_bin(self.bin, "orca", 'case "$2" in list) echo \'[{"id": "a1", "name": "com.relay.windows.codex"}, '
                                   '{"id": "b2", "name": "other"}]\';; create) echo \'{"id": "n1"}\';; esac')

    def test_dry_run_prints_the_exact_command(self):
        ok, msg = schedule.install(job(self.tmp), "orca", dry_run=True)
        self.assertTrue(ok)
        self.assertIn("automations create --name com.relay.windows.codex --trigger '0 5 * * 1,2,3,4,5'", msg)
        self.assertEqual(self.calls(), [])

    def test_install_replaces_same_name_then_creates(self):
        ok, msg = schedule.install(job(self.tmp), "orca")
        self.assertTrue(ok, msg)
        calls = self.calls()
        self.assertEqual(calls[0], "orca [automations] [list] [--json]")
        self.assertEqual(calls[1], "orca [automations] [remove] [--id] [a1] [--json]")
        self.assertTrue(calls[2].startswith("orca [automations] [create] [--name] [com.relay.windows.codex]"))
        self.assertEqual(len(calls), 3)

    def test_status_and_uninstall(self):
        self.assertEqual(schedule.status("com.relay.windows.codex", "orca")["ids"], ["a1"])
        ok, msg = schedule.uninstall("com.relay.windows.codex", "orca", dry_run=True)
        self.assertIn("remove --id a1", msg)
        self.assertNotIn("remove", " ".join(self.calls()))
        ok, _ = schedule.uninstall("com.relay.windows.codex", "orca")
        self.assertTrue(ok)
        self.assertIn("orca [automations] [remove] [--id] [a1] [--json]", self.calls())


class Cron(Sandbox):
    def test_install_only_prints(self):
        fake_bin(self.bin, "crontab", 'echo "0 * * * * x  # com.relay.autoburn"')
        j = schedule.Job("com.relay.autoburn", ["burn", "auto", "--check"])
        ok, msg = schedule.install(j, "cron")
        self.assertTrue(ok)
        self.assertIn("crontab -e", msg)
        self.assertIn("0 * * * * cd ", msg)
        self.assertEqual(self.calls(), [])                        # never touches the crontab
        self.assertTrue(schedule.status("com.relay.autoburn", "cron")["installed"])   # reads `crontab -l`
        self.assertEqual(self.calls(), ["crontab [-l]"])


class Report(Sandbox):
    def test_report_and_cli(self):
        agents = Path(self.home) / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        (agents / "com.relay.autoburn.plist").write_text("")
        cfg = {**CFG, "subscriptions": [{"name": "claude-max", "reserve_pct": 20}]}
        text = schedule.report(cfg, la(2026, 10, 10, 12))
        self.assertIn("off · work starts Mon 09:00", text)
        self.assertIn("claude-max", text)
        self.assertIn("80%", text)
        self.assertIn("com.relay.autoburn", text)
        path = os.path.join(self.tmp, "burner-week.json")
        Path(path).write_text('{"idle": {"timezone": "UTC", "weekday": "all", "weekend": "all"}}')
        ap = argparse.ArgumentParser()
        schedule.add_cli(ap.add_subparsers(dest="cmd"))
        out = io.StringIO()
        with redirect_stdout(out):
            args = ap.parse_args(["schedule", "-b", path])
            self.assertEqual(args.func(args), 0)
            args = ap.parse_args(["schedule", "-b", os.path.join(self.tmp, "missing.json")])
            self.assertEqual(args.func(args), 2)
        self.assertIn("schedule · UTC", out.getvalue())


if __name__ == "__main__":
    unittest.main()
