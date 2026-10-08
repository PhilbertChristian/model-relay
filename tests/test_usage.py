"""python3 -m unittest tests.test_usage -v

Offline: synthetic transcripts from tests/fixtures/usage are copied into a temp dir; Agent37 and Monid are mocked.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from relay.contracts import Usage
from relay.usage import claude_code_tokens, codex_tokens, report, week_bounds, weekly_usage

FIX = Path(__file__).resolve().parent / "fixtures" / "usage"
LA = "America/Los_Angeles"


def at(s: str, tz: str = LA) -> float:
    """Epoch seconds of wall-clock time `s` ('2026-10-07 12:00') in `tz`."""
    return datetime.fromisoformat(s).replace(tzinfo=ZoneInfo(tz)).timestamp()


def utc(s: str) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


NOW = at("2026-10-07 12:00")                     # a Wednesday: the week runs Mon Oct 5 09:00 -> Mon Oct 12 09:00 PDT
WEEK_START, WEEK_END = at("2026-10-05 09:00"), at("2026-10-12 09:00")
MONTH_END = utc("2026-11-01 00:00")


class WeekBounds(unittest.TestCase):
    def test_mid_week(self):
        self.assertEqual(week_bounds("mon 09:00", LA, NOW), (WEEK_START, WEEK_END))
        self.assertEqual(week_bounds("mon 09:00", LA, datetime.fromtimestamp(NOW, timezone.utc)), (WEEK_START, WEEK_END))

    def test_just_before_at_and_after_the_reset(self):
        nxt = (WEEK_END, at("2026-10-19 09:00"))
        self.assertEqual(week_bounds("mon 09:00", LA, WEEK_END - 1), (WEEK_START, WEEK_END))  # Monday 08:59:59
        self.assertEqual(week_bounds("mon 09:00", LA, WEEK_END), nxt)                        # the reset opens the new week
        self.assertEqual(week_bounds("mon 09:00", LA, WEEK_END + 1), nxt)

    def test_dst_weeks_last_167_or_169_hours(self):
        for tz, now, hours in ((LA, "2026-03-05 12:00", 167),                # spring forward Sun Mar 8
                               (LA, "2026-10-30 12:00", 169),                # fall back Sun Nov 1
                               ("Europe/London", "2026-10-21 12:00", 169)):  # fall back Sun Oct 25
            s, e = week_bounds("mon 09:00", tz, at(now, tz))
            self.assertEqual(e - s, hours * 3600, (tz, now))
            for t in (s, e):                                                  # both ends are Monday 09:00 local
                d = datetime.fromtimestamp(t, ZoneInfo(tz))
                self.assertEqual((d.weekday(), d.hour, d.minute), (0, 9, 0))

    def test_reset_in_a_dst_gap_or_overlap_still_tiles(self):
        # 02:30 never happens on Mar 8 in LA; 01:30 happens twice on Nov 1. Windows must still tile time exactly.
        for reset, start in (("sun 02:30", "2026-03-01 00:00"), ("sun 01:30", "2026-10-25 00:00")):
            prev = None
            for k in range(15 * 96):                                          # every 15 min for 15 days
                now = at(start) + k * 900
                s, e = week_bounds(reset, LA, now)
                self.assertTrue(s <= now < e, (reset, now))
                if prev and prev != (s, e):
                    self.assertEqual(prev[1], s)
                prev = (s, e)

    def test_timezone_falls_back_to_tz_env_then_utc(self):
        berlin = week_bounds("mon 09:00", "Europe/Berlin", NOW)
        with mock.patch.dict(os.environ, {"TZ": "Europe/Berlin"}):
            self.assertEqual(week_bounds("mon 09:00", None, NOW), berlin)
            self.assertEqual(week_bounds("mon 09:00", "Not/AZone", NOW), berlin)
        no_tz = {k: v for k, v in os.environ.items() if k != "TZ"}
        with mock.patch.dict(os.environ, no_tz, clear=True):
            self.assertEqual(week_bounds("mon 09:00", None, NOW), (utc("2026-10-05 09:00"), utc("2026-10-12 09:00")))

    def test_monthly_budgets_reset_on_reset_day_utc(self):
        from relay.usage import _month_bounds
        for now, day, start, end in (("2026-10-07 19:00", 1, "2026-10-01", "2026-11-01"),
                                     ("2026-12-15 00:00", 1, "2026-12-01", "2027-01-01"),
                                     ("2026-01-03 00:00", 5, "2025-12-05", "2026-01-05"),
                                     ("2026-01-30 12:00", 28, "2026-01-28", "2026-02-28"),
                                     ("2026-03-30 12:00", 31, "2026-03-28", "2026-04-28")):   # day capped at 28
            self.assertEqual(_month_bounds(utc(now), day), (utc(start + " 00:00"), utc(end + " 00:00")), now)

    def test_reset_formats(self):
        for s in ("Monday 9:00", "mon 9am", "MON 09:00", "mon. 9:00 AM"):
            self.assertEqual(week_bounds(s, LA, NOW), (WEEK_START, WEEK_END), s)
        self.assertEqual(week_bounds("sun 9:30pm", LA, NOW)[0], at("2026-10-04 21:30"))
        self.assertEqual(week_bounds("fri", LA, NOW)[0], at("2026-10-02 00:00"))
        for bad in ("someday", "mon 25:00", "", "09:00"):
            with self.assertRaises(ValueError, msg=bad):
                week_bounds(bad, LA, NOW)


class Transcripts(unittest.TestCase):
    """Fixture copies in a temp dir (never ~/.claude or ~/.codex), all modified 'now'."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.claude = self.tmp / "claude-home" / "projects"
        self.codex = self.tmp / "codex-home" / "sessions"
        shutil.copytree(FIX / "claude", self.claude)
        shutil.copytree(FIX / "codex", self.codex)
        for f in self.tmp.rglob("*.jsonl"):
            os.utime(f, (NOW, NOW))
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp / "claude-home"),
                                           "CODEX_HOME": str(self.tmp / "codex-home")})
        env.start()
        self.addCleanup(env.stop)


class ClaudeCodeTokens(Transcripts):
    def test_sums_dedupes_and_skips_noise(self):
        # msg_a1 is streamed as 3 rows (output 10, 10, 200: keep 200); msg_a2 reappears in a resumed session;
        # req_x has no message id (dedupe by requestId); last week's row, user rows, the zero-usage <synthetic>
        # row, the summary and a truncated line are ignored.
        self.assertEqual(claude_code_tokens(WEEK_START, str(self.claude)), {
            "input": 122, "output": 593, "cache_read": 26000, "cache_creation": 900, "total": 27615,
            "by_model": {"claude-sonnet-5": 7310, "claude-opus-5-5": 20305}, "sessions": 2})

    def test_rows_before_since_are_ignored(self):
        t = claude_code_tokens(utc("2026-10-06 10:30"), str(self.claude))
        self.assertEqual((t["total"], t["by_model"], t["sessions"]), (5500, {"claude-sonnet-5": 5500}, 1))

    def test_files_untouched_since_the_reset_are_not_read(self):
        beta = next(self.claude.glob("-Users-dev-beta/*.jsonl"))
        os.utime(beta, (WEEK_START - 60, WEEK_START - 60))
        t = claude_code_tokens(WEEK_START, str(self.claude))
        self.assertEqual((t["total"], t["sessions"]), (27615 - 5500, 1))
        with mock.patch("relay.usage.open", side_effect=AssertionError("read an old file"), create=True) as op:
            claude_code_tokens(NOW + 1, str(self.claude))
        op.assert_not_called()

    def test_missing_root_is_empty(self):
        t = claude_code_tokens(0, str(self.tmp / "nope"))
        self.assertEqual((t["total"], t["by_model"], t["sessions"]), (0, {}, 0))


class CodexTokens(Transcripts):
    def test_sums_every_schema(self):
        # cumulative info.total_token_usage (differenced; the repeated event adds nothing), info=null rate-limit
        # rows, flat token_count counts, last_token_usage only (deduped), and a plain {"usage": ...} event.
        self.assertEqual(codex_tokens(WEEK_START, str(self.codex)), {
            "input": 2550, "output": 970, "cache_read": 2450, "cache_creation": 0, "total": 5970,
            "by_model": {"gpt-5-codex": 2300, "gpt-5": 2500, "unknown": 1170}, "sessions": 2})

    def test_a_session_spanning_since_counts_only_what_came_after(self):
        t = codex_tokens(utc("2026-10-06 00:00"), str(self.codex))
        self.assertEqual((t["total"], t["by_model"]), (3670, {"gpt-5": 2500, "unknown": 1170}))

    def test_a_resumed_session_continues_its_totals_in_a_new_file(self):
        f = self.codex / "2026" / "10" / "07" / "rollout-2026-10-07T08-00-00-cccc3333.jsonl"
        f.parent.mkdir(parents=True)
        rows = [{"timestamp": "2026-10-07T08:00:00Z", "type": "session_meta",
                 "payload": {"id": "cccc3333-0000-4000-8000-000000000003"}},
                {"timestamp": "2026-10-07T08:01:00Z", "type": "event_msg", "payload": {"type": "token_count", "info": {
                    "total_token_usage": {"input_tokens": 6000, "cached_input_tokens": 3500, "output_tokens": 1000}}}}]
        f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        os.utime(f, (NOW + 1, NOW + 1))
        t = codex_tokens(WEEK_START, str(self.codex))
        self.assertEqual((t["total"], t["sessions"]), (5970 + 1100, 2))    # only the growth past 5000/3000/900


class WeeklyUsage(Transcripts):
    def setUp(self):
        super().setUp()
        oct2 = utc("2026-10-02 12:00")

        def row(i, session, event, **kw):
            return {"ts": oct2 + i, "session": session, "host": "local", "event": event, **kw}

        rows = [row(0, "s1", "agent_progress", agent=1, task_id="t1", lane="relay", usd=0.10),
                row(1, "s1", "agent_progress", agent=1, task_id="t1", lane="relay", usd=0.25),
                row(2, "s1", "agent_end", agent=1, task_id="t1", lane="relay", usd=0.30, status="limited"),
                row(3, "s1", "agent_progress", agent=1, task_id="t1", lane="relay", usd=0.12),   # retry: new attempt
                row(4, "s1", "agent_end", agent=1, task_id="t1", lane="relay", usd=0.20, status="done"),
                row(5, "s1", "agent_progress", agent=2, task_id="t2", lane="relay-mini", usd=0.05),  # still running
                row(6, "s1", "agent_end", agent=3, task_id="t3", lane="codex", usd=1.0),            # other lane
                {"ts": utc("2026-09-30 23:00"), "session": "s0", "event": "agent_end", "lane": "relay", "usd": 5.0},
                row(7, "s2", "call", provider="openai", model="oai-mini", usd=0.40),                # a night shift
                row(8, "s2", "call", provider="openai", model="oai-mini", usd=0.10),
                row(9, "s2", "call", provider="agent37", model="a37-default", usd=9.0),
                row(10, "s3", "call", provider="openai", model="oai-mini", usd=0.30),               # one spend, logged
                row(11, "s3", "agent_end", agent=1, task_id="t4", lane="relay", usd=0.30),          # twice: counts once
                row(12, "s1", "agent_end", agent=4, task_id="t5", lane="agent37", usd=2.0)]
        self.ledger = self.tmp / "events.jsonl"
        self.ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n{not json\n")
        self.cfg = {
            "week": {"reset": "mon 09:00", "timezone": LA, "target_pct": 0.97},
            "subscriptions": [
                {"name": "claude-max", "kind": "claude_code", "lane": "claude", "weekly_tokens": 100000,
                 "root": str(self.claude)},
                {"name": "codex", "kind": "codex", "lane": "codex", "weekly_tokens": 20000},
                {"name": "agent37", "kind": "agent37_budget", "lane": "agent37", "monthly_usd": 20,
                 "instance": "$AGENT37_INSTANCE_ID"},
                {"name": "openai", "kind": "api_budget", "lane": "relay", "providers": ["openai"], "monthly_usd": 20},
                {"name": "monid", "kind": "monid_credits", "lane": "monid", "credits": 1000},
                {"name": "demo-claude", "kind": "demo", "lane": "claude", "used": 62, "limit": 100, "unit": "credits",
                 "resets_in_hours": 61}],
            "lanes": [{"kind": "relay", "name": "relay-mini", "subscription": "openai"}]}

    def usages(self, live=None, monid=None) -> dict[str, Usage]:
        env = {k: v for k, v in os.environ.items() if k != "AGENT37_INSTANCE_ID"}
        with mock.patch("relay.capacity._agent37_live", return_value=live) as a37, \
                mock.patch.dict(sys.modules, {"relay.monid": monid}), mock.patch.dict(os.environ, env, clear=True):
            out = weekly_usage(self.cfg, str(self.ledger), NOW)
        a37.assert_called_once_with("")                     # an unset $AGENT37_INSTANCE_ID is never sent
        self.assertEqual(len(out), len(self.cfg["subscriptions"]))
        return {u.name: u for u in out}

    def test_every_kind(self):
        monid = types.ModuleType("relay.monid")
        monid.credits = lambda: Usage("monid-api", "?", "credits", 120.0, 1000.0, 0.0, "live")
        u = self.usages(live={"monthly_remaining_micros": 12_500_000, "credit_remaining_micros": 2_500_000}, monid=monid)
        self.assertEqual(u["claude-max"], Usage("claude-max", "claude", "tokens", 27615.0, 100000.0, WEEK_END,
                                                "transcripts", "2 sessions this week, mostly claude-opus-5-5"))
        self.assertEqual((u["codex"].used, u["codex"].limit, u["codex"].resets_at, u["codex"].source),
                         (5970.0, 20000.0, WEEK_END, "transcripts"))       # default root: $CODEX_HOME/sessions
        self.assertEqual((u["agent37"].used, u["agent37"].limit, u["agent37"].unit, u["agent37"].resets_at,
                          u["agent37"].source), (5.0, 20.0, "usd", MONTH_END, "live"))
        self.assertEqual((u["openai"].lane, u["openai"].used, u["openai"].limit, u["openai"].resets_at,
                          u["openai"].source), ("relay", 1.35, 20.0, MONTH_END, "ledger"))
        self.assertEqual(u["monid"], Usage("monid", "monid", "credits", 120.0, 1000.0, WEEK_END, "live"))
        self.assertEqual(u["demo-claude"], Usage("demo-claude", "claude", "credits", 62.0, 100.0, NOW + 61 * 3600, "demo"))

    def test_fallbacks_without_live_sources(self):
        broken = types.ModuleType("relay.monid")
        broken.credits = mock.Mock(side_effect=RuntimeError("offline"))
        for monid in (None, broken):                         # module missing, or credits() failing
            u = self.usages(live=None, monid=monid)
            self.assertEqual((u["agent37"].used, u["agent37"].limit, u["agent37"].source), (11.0, 20.0, "ledger"))
            self.assertEqual(u["monid"], Usage("monid", "monid", "credits", 0.0, 1000.0, WEEK_END, "config"))

    def test_instance_id_comes_from_the_environment(self):
        self.cfg["subscriptions"] = [s for s in self.cfg["subscriptions"] if s["kind"] == "agent37_budget"]
        with mock.patch("relay.capacity._agent37_live", return_value=None) as a37, \
                mock.patch.dict(os.environ, {"AGENT37_INSTANCE_ID": "inst_123"}):
            weekly_usage(self.cfg, str(self.ledger), NOW)
        a37.assert_called_once_with("inst_123")

    def test_missing_transcripts_unknown_kinds_and_errors(self):
        self.cfg["subscriptions"] = [
            {"name": "claude-max", "kind": "claude_code", "weekly_tokens": 100, "root": str(self.tmp / "nope")},
            {"name": "windows", "kind": "rolling_window"},
            {"name": "broken", "kind": "codex", "reset": "someday"},
            {"name": "openai", "kind": "api_budget"}]
        u = {x.name: x for x in weekly_usage(self.cfg, str(self.tmp / "missing.jsonl"), NOW)}
        self.assertEqual((u["claude-max"].source, u["claude-max"].used, u["claude-max"].lane),
                         ("config", 0.0, "claude"))
        self.assertTrue(u["claude-max"].note.startswith("no transcripts at"))
        self.assertEqual((u["windows"].source, u["windows"].note), ("config", "unknown kind 'rolling_window'"))
        self.assertEqual((u["broken"].lane, u["broken"].resets_at, u["broken"].limit), ("codex", NOW, None))
        self.assertIn("someday", u["broken"].note)
        self.assertEqual((u["openai"].used, u["openai"].limit), (0.0, 0.0))  # no budget set: nothing to spend


class Report(unittest.TestCase):
    def test_table_with_bars(self):
        us = [Usage("claude-max", "claude", "tokens", 62e6, 100e6, NOW + 61 * 3600, "transcripts", "2 sessions"),
              Usage("openai", "relay", "usd", 4.2, 20.0, NOW + 600 * 3600, "ledger", "via sk-ant-abcdefghijklmnopqrstuvwx"),
              Usage("monid", "monid", "credits", 0.0, None, NOW - 5, "config")]
        with mock.patch("relay.ui._COLOR", False):
            out = report(us, now=NOW)
        head, claude, openai, monid = out.splitlines()
        self.assertIn("subscription", head)
        self.assertIn("62.0M / 100.0M", claude)
        self.assertIn("█" * 10 + "░" * 6, claude)
        self.assertIn("62%", claude)
        self.assertIn("61h", claude)
        self.assertIn("$4.20 / $20.00", openai)
        self.assertIn("25d", openai)
        self.assertNotIn("sk-ant-", out)
        self.assertIn("[redacted]", out)
        self.assertIn("now", monid)
        with mock.patch("relay.ui._COLOR", True):
            self.assertIn("\033[", report(us, now=NOW))
        self.assertEqual(report([]), "no subscriptions configured")


if __name__ == "__main__":
    unittest.main()
