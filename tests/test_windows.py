"""relay.windows: staggered 5-hour window priming, hour alignment, the minimal safe prime command, skip-if-active.

Offline: fake claude / codex / git executables on a PATH that holds nothing else, a fake relay.usage for
transcript counts, a temp HOME and ledger, and an injected `now`."""
import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import relay.config  # noqa: F401  (imported before any sys.modules patching)
from relay import schedule, windows

LA = ZoneInfo("America/Los_Angeles")
CLAUDE_OK = """printf "cwd=%s cfg=%s key=%s\\n" "$PWD" "$CLAUDE_CONFIG_DIR" "${ANTHROPIC_API_KEY:-none}" >> "$FAKE_LOG"
for f in "$PWD"/* "$PWD"/.[!.]*; do [ -e "$f" ] && printf "file=%s\\n" "$f" >> "$FAKE_LOG"; done
echo '{"type": "result", "subtype": "success", "is_error": false, "result": "OK"}'"""


def la(*a) -> datetime:
    return datetime(*a, tzinfo=LA)


def fake_bin(d: str, name: str, body: str = "") -> None:
    """An executable that logs `name [arg]...` to $FAKE_LOG, then runs `body` (shell builtins only)."""
    p = Path(d) / name
    p.write_text(f'#!/bin/sh\nprintf "%s" "{name}" >> "$FAKE_LOG"\n'
                 'for a in "$@"; do printf " [%s]" "$a" >> "$FAKE_LOG"; done\n'
                 'printf "\\n" >> "$FAKE_LOG"\n' + body + "\n")
    p.chmod(0o755)


def fake_usage(total: int = 0) -> types.ModuleType:
    m = types.ModuleType("relay.usage")
    m.seen = []

    def tokens(since, root="~"):
        m.seen.append((since, root))
        return {"total": total, "sessions": 1 if total else 0}
    m.claude_code_tokens = m.codex_tokens = tokens
    return m


def config(tmp: str, **w) -> dict:
    return {"windows": {"prime_at": "05:00", "stagger_minutes": 60, "days": ["mon", "tue", "wed", "thu", "fri"],
                        "timezone": "America/Los_Angeles",
                        "plans": [{"name": "claude-a", "kind": "claude_code",
                                   "env": {"CLAUDE_CONFIG_DIR": "~/.claude-a"}},
                                  {"name": "codex", "kind": "codex"}], **w},
            "subscriptions": [{"name": "codex", "kind": "codex", "window_hours": 5}],
            "data_dir": os.path.join(tmp, "data")}


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="relay-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin, self.home, self.log = (os.path.join(self.tmp, x) for x in ("bin", "home", "calls.log"))
        os.makedirs(self.bin)
        os.makedirs(self.home)
        self.ledger = os.path.join(self.tmp, ".relay", "events.jsonl")
        env = {"HOME": self.home, "PATH": self.bin, "FAKE_LOG": self.log, "ORCA_BIN": os.path.join(self.bin, "orca"),
               "ANTHROPIC_API_KEY": "sk-ant-api03-FAKEFAKEFAKEFAKEFAKE",
               "OPENAI_API_KEY": "sk-proj-FAKEFAKEFAKEFAKEFAKE",
               "SUPABASE_URL": "", "SUPABASE_KEY": "", "SUPABASE_SERVICE_ROLE_KEY": ""}
        for k in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
            env[k] = ""
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        for k in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
            os.environ.pop(k)
        self.usage = fake_usage()
        mods = mock.patch.dict(sys.modules, {"relay.usage": self.usage})
        mods.start()
        self.addCleanup(mods.stop)
        self.cfg = config(self.tmp)
        self.plans = {p["name"]: p for p in windows.plans(self.cfg)}

    def calls(self) -> list[str]:
        return Path(self.log).read_text().splitlines() if os.path.exists(self.log) else []

    def rows(self) -> list[dict]:
        return schedule.ledger_rows(self.ledger)


class Plan(unittest.TestCase):
    def test_staggered_resets_all_morning(self):
        primes = windows.plan_primes(config("/x"), "2026-10-07")            # a Wednesday
        self.assertEqual([p["name"] for p, _, _ in primes], ["claude-a", "codex"])
        self.assertEqual([(a, z) for _, a, z in primes], [(la(2026, 10, 7, 5), la(2026, 10, 7, 10)),
                                                           (la(2026, 10, 7, 6), la(2026, 10, 7, 11))])
        self.assertEqual(windows.plan_primes(config("/x"), date(2026, 10, 10)), [])   # Saturday: not a prime day
        self.assertEqual(len(windows.plan_primes(config("/x"), la(2026, 10, 9, 23).timestamp())), 2)

    def test_hour_alignment_is_configurable(self):
        self.assertEqual(windows.reset_at(la(2026, 10, 7, 5, 1)), la(2026, 10, 7, 10))
        self.assertEqual(windows.reset_at(la(2026, 10, 7, 5, 59, 59)), la(2026, 10, 7, 10))
        self.assertEqual(windows.reset_at(la(2026, 10, 7, 5, 1), align="exact"), la(2026, 10, 7, 10, 1))
        self.assertEqual(windows.reset_at(la(2026, 10, 7, 5, 1), window_hours=4), la(2026, 10, 7, 9))
        half = windows.plan_primes(config("/x", stagger_minutes=30), "2026-10-07")
        self.assertEqual([z for _, _, z in half], [la(2026, 10, 7, 10)] * 2)          # 05:00 and 05:30 share 10:00
        exact = windows.plan_primes(config("/x", stagger_minutes=30, align="exact"), "2026-10-07")
        self.assertEqual([z for _, _, z in exact], [la(2026, 10, 7, 10), la(2026, 10, 7, 10, 30)])

    def test_per_plan_overrides(self):
        cfg = config("/x")
        cfg["windows"]["plans"] = [{"name": "a", "kind": "claude_code", "prime_at": "07:15"},
                                   {"name": "b", "kind": "codex", "subscription": "chatgpt", "days": ["sat"]}]
        cfg["subscriptions"] = [{"name": "chatgpt", "kind": "codex", "window_hours": 4}]
        (p, a, z), = windows.plan_primes(cfg, "2026-10-07")
        self.assertEqual((p["name"], a, z), ("a", la(2026, 10, 7, 7, 15), la(2026, 10, 7, 12)))
        (p, a, z), = windows.plan_primes(cfg, "2026-10-10")
        self.assertEqual((p["name"], a, z), ("b", la(2026, 10, 10, 6), la(2026, 10, 10, 10)))


class Prime(Sandbox):
    def test_claude_prime_is_minimal_and_safe(self):
        fake_bin(self.bin, "claude", CLAUDE_OK)
        row = windows.prime(self.plans["claude-a"], now=la(2026, 10, 7, 5, 0, 30), ledger=self.ledger)
        self.assertEqual((row["ok"], row["note"], row["skipped"]), (True, "OK", False))
        self.assertEqual(row["reset_at"], la(2026, 10, 7, 10).timestamp())
        argv, info = self.calls()
        self.assertEqual(argv, "claude [-p] [Reply with OK.] [--model] [haiku] [--max-turns] [1] [--output-format] "
                               "[json] [--tools] [] [--strict-mcp-config] [--no-session-persistence]")
        self.assertNotIn("dangerously", argv)
        self.assertIn("relay-prime-", info)                       # an empty temp dir, not the user's checkout
        self.assertIn(f"cfg={self.home}/.claude-a ", info)        # the seat's own config dir, ~ expanded
        self.assertIn("key=none", info)                           # API keys dropped: the plan's login answers
        row_logged, = self.rows()
        self.assertEqual(row_logged["event"], "window_primed")
        self.assertEqual({k: row_logged[k] for k in ("plan", "prime_at", "reset_at", "ok", "note")},
                         {k: row[k] for k in ("plan", "prime_at", "reset_at", "ok", "note")})
        (since, root), = self.usage.seen
        self.assertEqual(since, la(2026, 10, 7, 1).timestamp())   # use before 01:00 sat in a window that has reset
        self.assertEqual(root, f"{self.home}/.claude-a/projects")

    def test_codex_prime_runs_in_a_fresh_repo(self):
        fake_bin(self.bin, "git")
        fake_bin(self.bin, "codex", 'printf "cwd=%s\\n" "$PWD" >> "$FAKE_LOG"\necho OK')
        row = windows.prime(self.plans["codex"], now=la(2026, 10, 7, 6, 0, 5), ledger=self.ledger)
        self.assertEqual((row["ok"], row["note"]), (True, "OK"))
        git, codex, cwd = self.calls()
        self.assertTrue(git.startswith("git [init] [-q] ["), git)
        self.assertEqual(codex, "codex [exec] [--sandbox] [read-only] [Reply with OK.]")
        self.assertEqual(os.path.basename(git[:-1]), os.path.basename(cwd))
        self.assertEqual(self.usage.seen[0][1], "~/.codex/sessions")
        self.cfg["subscriptions"][0]["root"] = "/seats/codex/sessions"            # usage.py's per-subscription root
        windows.prime(windows.plans(self.cfg)[1], True, now=la(2026, 10, 7, 6), ledger=self.tmp + "/empty.jsonl")
        self.assertEqual(self.usage.seen[-1][1], "/seats/codex/sessions")

    def test_skips_a_window_primed_earlier(self):
        fake_bin(self.bin, "claude", CLAUDE_OK)
        os.makedirs(os.path.dirname(self.ledger))
        prior = {"event": "window_primed", "plan": "claude-a", "ok": True, "prime_at": la(2026, 10, 7, 5).timestamp(),
                 "reset_at": la(2026, 10, 7, 10).timestamp()}
        Path(self.ledger).write_text(json.dumps(prior) + "\n")
        row = windows.prime(self.plans["claude-a"], now=la(2026, 10, 7, 9), ledger=self.ledger)
        self.assertEqual((row["skipped"], row["ok"], row["reset_at"]), (True, None, None))
        self.assertIn("primed 05:00, resets 10:00", row["note"])
        self.assertEqual(self.calls(), [])
        self.assertTrue(self.rows()[-1]["skipped"])
        later = windows.prime(self.plans["claude-a"], now=la(2026, 10, 7, 10, 1), ledger=self.ledger)
        self.assertTrue(later["ok"])                               # that window has reset: prime again

    def test_skips_on_recent_transcript_use_unless_forced(self):
        fake_bin(self.bin, "claude", CLAUDE_OK)
        self.usage.claude_code_tokens = lambda since, root: {"total": 5000}
        row = windows.prime(self.plans["claude-a"], now=la(2026, 10, 7, 5, 0, 30), ledger=self.ledger)
        self.assertTrue(row["skipped"])
        self.assertIn("5,000 tokens used since 01:00", row["note"])
        self.assertEqual(self.calls(), [])
        forced = windows.prime(self.plans["claude-a"], now=la(2026, 10, 7, 5, 0, 30), ledger=self.ledger, force=True)
        self.assertTrue(forced["ok"])

    def test_dry_run_sends_and_logs_nothing(self):
        fake_bin(self.bin, "claude", CLAUDE_OK)
        row = windows.prime(self.plans["claude-a"], True, now=la(2026, 10, 7, 5), ledger=self.ledger)
        self.assertIsNone(row["ok"])
        self.assertTrue(row["note"].startswith("dry run: would run claude -p 'Reply with OK.' --model haiku"),
                        row["note"])
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.exists(self.ledger))

    def test_failure_is_reported_and_redacted(self):
        fake_bin(self.bin, "claude",
                 'echo "usage limit reached; token sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA" >&2\nexit 1')
        row = windows.prime(self.plans["claude-a"], now=la(2026, 10, 7, 5), ledger=self.ledger)
        self.assertFalse(row["ok"])
        self.assertIn("exit 1: usage limit reached", row["note"])
        self.assertIn("[redacted]", row["note"])
        self.assertNotIn("AAAAAAAAAAAAAAAA", Path(self.ledger).read_text())

    def test_claude_is_error_counts_as_failure(self):
        fake_bin(self.bin, "claude",
                 """echo '{"type": "result", "is_error": true, "result": "Credit balance too low"}'""")
        row = windows.prime(self.plans["claude-a"], now=la(2026, 10, 7, 5), ledger=self.ledger)
        self.assertEqual((row["ok"], row["note"]), (False, "Credit balance too low"))

    def test_missing_binary(self):
        row = windows.prime(self.plans["claude-a"], now=la(2026, 10, 7, 5), ledger=self.ledger)
        self.assertEqual((row["ok"], row["note"]), (False, "claude not found on PATH"))


class ReportAndJobs(Sandbox):
    def test_report_marks_what_happened(self):
        fake_bin(self.bin, "claude", CLAUDE_OK)
        windows.prime(self.plans["claude-a"], now=la(2026, 10, 7, 5), ledger=self.ledger)
        text = windows.report(self.cfg, now=la(2026, 10, 7, 5, 30), ledger=self.ledger)
        self.assertIn("5-hour windows · Wed Oct 07 · America/Los_Angeles", text)
        line_a = next(l for l in text.splitlines() if "claude-a" in l)
        line_c = next(l for l in text.splitlines() if "codex" in l)
        self.assertIn("05:00  10:00  ✓ primed 05:00", line_a)
        self.assertIn("06:00  11:00  due", line_c)
        self.assertIn("no primes today", windows.report(self.cfg, now=la(2026, 10, 10, 9), ledger=self.ledger))

    def test_job(self):
        j = windows.job(self.cfg, self.plans["codex"], "/c/burner-week.json", self.tmp)
        self.assertEqual(j.label, "com.relay.windows.codex")
        self.assertEqual(j.calendar, [(d, 6, 0) for d in range(5)])
        self.assertEqual(j.argv, ["windows", "prime", "--plan", "codex", "-b", "/c/burner-week.json"])
        self.assertEqual(j.precheck, ["windows", "due", "--plan", "codex", "-b", "/c/burner-week.json"])
        self.assertEqual((j.provider, j.prompt, j.timezone), ("codex", "Reply with OK.", "America/Los_Angeles"))
        self.assertEqual(j.log, os.path.join(self.tmp, "data", "logs", "com.relay.windows.codex.log"))
        late = windows.plans(config("/x", prime_at="23:30"))[1]                       # 24:30 -> next day 00:30
        self.assertEqual(windows.job(config("/x", prime_at="23:30"), late, "/c/b.json").calendar,
                         [(d, 0, 30) for d in range(1, 6)])


class Cli(Sandbox):
    def run_cli(self, *argv) -> tuple[int, str]:
        path = os.path.join(self.tmp, "burner-week.json")
        Path(path).write_text(json.dumps(self.cfg))
        ap = argparse.ArgumentParser()
        ap.add_argument("-C", "--cwd", default=".")
        windows.add_cli(ap.add_subparsers(dest="cmd"))
        out = io.StringIO()
        with redirect_stdout(out):
            args = ap.parse_args(["-C", self.tmp, "windows", *argv, "-b", path])
            return args.func(args), out.getvalue()

    def test_plan_due_prime_install(self):
        rc, out = self.run_cli("plan")
        self.assertEqual(rc, 0)
        self.assertIn("5-hour windows", out)
        self.assertEqual(self.run_cli("due", "--plan", "claude-a"), (0, "claude-a: due\n"))
        os.makedirs(os.path.dirname(self.ledger))
        Path(self.ledger).write_text(json.dumps({"event": "window_primed", "plan": "claude-a", "ok": True,
                                                 "prime_at": time.time() - 60, "reset_at": time.time() + 3600}) + "\n")
        rc, out = self.run_cli("due", "--plan", "claude-a")
        self.assertEqual(rc, 1)
        self.assertIn("not due, already active", out)
        rc, out = self.run_cli("prime", "--plan", "codex", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("dry run: would run codex exec --sandbox read-only", out)
        rc, out = self.run_cli("install", "--plan", "codex", "--dry-run", "--via", "launchd")
        self.assertEqual(rc, 0)
        self.assertIn("would write", out)
        self.assertIn("<string>com.relay.windows.codex</string>", out)
        rc, out = self.run_cli("install", "--plan", "codex", "--dry-run", "--via", "orca")
        self.assertIn("automations create --name com.relay.windows.codex", out)
        rc, out = self.run_cli("status", "--plan", "codex", "--via", "launchd")
        self.assertIn("not installed", out)
        self.assertEqual(self.calls(), [])                         # dry runs and status called nothing
        self.assertFalse((Path(self.home) / "Library").exists())
        self.assertEqual(self.run_cli("prime", "--plan", "nope")[0], 2)


if __name__ == "__main__":
    unittest.main()
