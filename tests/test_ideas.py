"""python3 -m unittest tests.test_ideas -v

Small synthetic transcripts in tests/fixtures/ideas only: the real ~/.claude and ~/.codex are never read
(every test passes explicit roots, and resolving a default root fails the test)."""
import json
import os
import re
import shutil
import tempfile
import time
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from unittest import mock

from relay import ideas

FIX = Path(__file__).resolve().parent / "fixtures" / "ideas"
NOW = 1791417600.0                     # 2026-10-08T00:00:00Z: the fixtures are dated the week before
FX = "/nonexistent/relay-fixture"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
SHOP_IDEAS = ["Add retries to the payment client", "Add a regression test for refunds over $100",
              "Cache the tax-rate lookup per region", "Wire the refund webhook to the ledger service",
              "Add a dark mode toggle in the admin panel", "Next step: rotate the key [redacted] in the deploy config"]
CHATGPT_IDEAS = {"Sync streaks across devices", "Add an export of all habits to CSV", "Add push reminders for streaks at risk"}


def day(when) -> str:
    """Local date of an epoch or a UTC ISO time, as digest() prints it (so any timezone passes)."""
    ts = datetime.fromisoformat(when + "+00:00").timestamp() if isinstance(when, str) else when
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def session(path: Path, *said: tuple[str, str]) -> None:
    """A Claude Code transcript of user lines: (cwd, text) pairs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({"type": "user", "cwd": cwd, "timestamp": "2026-10-06T10:00:00.000Z",
                                        "message": {"role": "user", "content": text}}) + "\n" for cwd, text in said))


class Base(unittest.TestCase):
    def setUp(self):
        for name, kw in (("_now", {"return_value": NOW}),
                         ("_default_root", {"side_effect": AssertionError("tests must pass explicit roots")})):
            p = mock.patch.object(ideas, name, **kw)
            p.start()
            self.addCleanup(p.stop)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def cfg(self, **over) -> dict:
        return {"ideas": {"enabled": True, "claude_root": str(FIX / "claude"), "codex_root": str(FIX / "codex"),
                          "chatgpt_export": str(FIX / "chatgpt"), **over}}


class ClaudeCode(Base):
    def test_unfinished_ideas_and_plans(self):
        got = ideas.mine_claude_code(str(FIX / "claude"))
        texts = [i.text for i in got]
        for want in SHOP_IDEAS:
            self.assertIn(want, texts)
        pricing = [t for t in texts if t.startswith("Rewrite the whole pricing engine")]
        self.assertTrue(pricing and len(pricing[0]) <= 200 and pricing[0].endswith("…"))
        # acted on (renamed; "yes, do both"), routine, questions, code, logs, tool output, thinking, sidechains,
        # too-deep subagent files, burn-week's own agent session, and older than since_days
        for gone in ("utils.py", "Run the test suite", "rules linter", "caching", "refactor the whole checkout", "fence",
                     "thinking", "tool output", "retry later", "subagent", "jitter", "uploader", "RSS", "sk-fixture"):
            self.assertFalse([t for t in texts if gone in t], gone)
        self.assertEqual({(i.project_dir, i.source) for i in got}, {(f"{FX}/shop", "claude-code")})
        self.assertTrue(all(ideas.classify(t) in ideas.ACTIONABLE and len(t) <= 200 for t in texts))
        self.assertEqual(got, sorted(got, key=lambda i: -i.score))

    def test_all_kinds(self):
        by: dict[str, list[str]] = {}
        for i in ideas.mine_claude_code(str(FIX / "claude"), kinds=ideas.KINDS):
            by.setdefault(ideas.classify(i.text), []).append(i.text)
        self.assertEqual(sorted(by["principle"]), ["Always keep the payment client free of global state",
                                                   "Decision: keep money as integer cents everywhere",
                                                   "We decided to use SQLite instead of Postgres for the cache"])
        self.assertEqual(sorted(by["history"]), ["Added a regression test for the half-cent case",
                                                 "Fixed the rounding bug in `totals.py`",
                                                 "Renamed `utils.py` to `helpers.py` and updated the imports"])
        self.assertEqual(by["summary"], ["Summary: Checkout rounding fixes"])

    def test_since_days_and_limit(self):
        rss = [i for i in ideas.mine_claude_code(str(FIX / "claude"), since_days=120) if "RSS" in i.text]
        self.assertEqual([(i.text, i.project_dir) for i in rss], [("Add an RSS feed to the blog", f"{FX}/blog")])
        self.assertEqual(len(ideas.mine_claude_code(str(FIX / "claude"), limit=2)), 2)

    def test_skips_secret_named_files_and_sessions_in_the_data_dir(self):
        root, data = self.tmp / "projects", self.tmp / "burn-data"
        shutil.copytree(FIX / "claude", root)
        session(root / "-shop" / "credentials.jsonl", (f"{FX}/shop", "we should leak this secret idea later"))
        session(root / "-burn" / "s.jsonl", (str(data / "worktrees" / "shop" / "t1"), "we should add the worktree idea later"))
        opened, real = [], ideas._rows
        with mock.patch.object(ideas, "_rows", side_effect=lambda p: (opened.append(str(p)), real(p))[1]):
            texts = [i.text for i in ideas.mine_claude_code(str(root), data_dir=str(data))]
        self.assertFalse([t for t in texts if "leak" in t or "worktree idea" in t])
        self.assertFalse([p for p in opened if p.endswith("credentials.jsonl")])
        self.assertIn("Add the worktree idea", [i.text for i in ideas.mine_claude_code(str(root))])   # other data dir


class Codex(Base):
    def test_new_and_old_rollouts(self):
        got = {i.text: i for i in ideas.mine_codex(str(FIX / "codex"))}
        self.assertEqual(set(got), {"Follow up on the flaky auth test", "Add a CSV export endpoint",
                                    "Bump the minimum Python version to 3.11"})
        self.assertEqual(got["Follow up on the flaky auth test"].project_dir, f"{FX}/api")
        old = got["Bump the minimum Python version to 3.11"]
        self.assertEqual((old.project_dir, old.ts, old.source), (f"{FX}/cli", 1790928000.0, "codex"))   # header time
        everything = [i.text for i in ideas.mine_codex(str(FIX / "codex"), kinds=ideas.KINDS)]
        self.assertIn("Added cursor pagination to `/v1/orders`", everything)
        self.assertFalse([t for t in everything if "never mine" in t or "cursor` parameter" in t])


class ChatGPT(Base):
    def test_json_folder_and_zip(self):
        zipped = self.tmp / "export.zip"
        with zipfile.ZipFile(zipped, "w") as z:
            z.write(FIX / "chatgpt" / "conversations.json", "export/conversations.json")
            z.writestr("export/user.json", '{"email": "someone@example.com"}')
        for path in (FIX / "chatgpt" / "conversations.json", FIX / "chatgpt", zipped):
            got = ideas.mine_chatgpt_export(str(path))
            self.assertEqual({i.text for i in got}, CHATGPT_IDEAS, path)
            self.assertEqual({(i.project_dir, i.source) for i in got}, {(None, "chatgpt")})

    def test_kept_branch_kinds_and_since_days(self):
        everything = {i.text for i in ideas.mine_chatgpt_export(str(FIX / "chatgpt"), kinds=ideas.KINDS)}
        self.assertLessEqual({"Always store streak dates in UTC", "Summary: Habit tracker sync"}, everything)
        self.assertFalse([t for t in everything if "abandoned branch" in t or "OAuth" in t])
        self.assertIn("Add OAuth login", {i.text for i in ideas.mine_chatgpt_export(str(FIX / "chatgpt"), since_days=200)})


class Mine(Base):
    def test_opt_in_reads_nothing_when_disabled(self):
        with mock.patch.object(ideas, "_files", side_effect=AssertionError("read while disabled")), \
                mock.patch.object(ideas, "_export", side_effect=AssertionError("read while disabled")):
            for cfg in (None, {}, {"ideas": {}}, {"ideas": {"enabled": False, "claude_root": str(FIX / "claude")}}):
                self.assertEqual(ideas.mine(cfg), {})

    def test_keyed_by_project_deduped_actionable_by_default(self):
        got = ideas.mine(self.cfg())
        self.assertEqual(set(got), {f"{FX}/shop", f"{FX}/api", f"{FX}/cli", ""})
        flat = [i for xs in got.values() for i in xs]
        self.assertTrue(all(ideas.classify(i.text) in ideas.ACTIONABLE for i in flat))
        self.assertEqual(len({(i.project_dir, i.text) for i in flat}), len(flat))
        self.assertEqual({i.text for i in got[""]}, CHATGPT_IDEAS)
        self.assertTrue(all(xs == sorted(xs, key=lambda i: -i.score) for xs in got.values()))
        self.assertEqual(sum(map(len, ideas.mine(self.cfg(limit=3)).values())), 3)
        self.assertEqual(set(ideas.mine(self.cfg(sources=["codex"]))), {f"{FX}/api", f"{FX}/cli"})

    def test_dedupes_across_sources(self):
        codex = self.tmp / "codex" / "2026" / "10" / "06" / "rollout-x.jsonl"
        codex.parent.mkdir(parents=True)
        codex.write_text("".join(json.dumps(r) + "\n" for r in (
            {"timestamp": "2026-10-06T10:00:00Z", "type": "session_meta", "payload": {"cwd": f"{FX}/shop"}},
            {"timestamp": "2026-10-06T10:00:01Z", "type": "event_msg",
             "payload": {"type": "user_message", "message": "we should add retries to the payment client later"}})))
        shop = [i.text for i in ideas.mine(self.cfg(codex_root=str(self.tmp / "codex")))[f"{FX}/shop"]]
        self.assertEqual(shop.count("Add retries to the payment client"), 1)

    def test_cwd_maps_to_the_repo_and_worktrees_to_the_main_checkout(self):
        repo, wt = self.tmp / "repo", self.tmp / "wt"
        (repo / ".git").mkdir(parents=True)
        (repo / "src").mkdir()
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {repo}/.git/worktrees/wt\n")
        session(self.tmp / "projects" / "-repo" / "a.jsonl", (str(repo / "src"), "we should add a changelog later"),
                (str(wt), "we should add a worktree badge later"))
        got = ideas.mine({"ideas": {"enabled": True, "claude_root": str(self.tmp / "projects"), "sources": ["claude-code"]}})
        self.assertEqual({d: sorted(i.text for i in xs) for d, xs in got.items()},
                         {os.path.realpath(repo): ["Add a changelog", "Add a worktree badge"]})


class ClassifyDigestReport(Base):
    def test_classify(self):
        for text, kind in [("Add retries to the payment client", "idea"), ("Plan the migration to Postgres", "idea"),
                           ("Next step: rotate the deploy key", "plan"), ("Tomorrow we'll add the CSV export", "plan"),
                           ("Always store dates in UTC", "principle"), ("Decision: keep money in cents", "principle"),
                           ("We decided to use SQLite for the cache", "principle"), ("Fixed the rounding bug", "history"),
                           ("Summary: Checkout fixes", "summary"), ("TL;DR: it works", "summary")]:
            self.assertEqual(ideas.classify(text), kind, text)

    def test_digest_and_report_group_by_kind(self):
        dg = ideas.digest(ideas.mine(self.cfg(), kinds=ideas.KINDS))
        self.assertEqual(list(dg), list(ideas.KINDS))
        self.assertIn(f"Always store streak dates in UTC (no project, {day(1791028800 + 120)})", dg["principle"])
        self.assertIn(f"{day('2026-10-05T09:02:00')} {FX}/shop: Fixed the rounding bug in `totals.py`", dg["history"])
        self.assertEqual(dg["summary"], [f"{day('2026-10-05T09:36:00')} {FX}/shop: Checkout rounding fixes",
                                         f"{day(1791028800 + 200)} no project: Habit tracker sync"])
        full = ANSI.sub("", ideas.report(ideas.mine(self.cfg(), kinds=ideas.KINDS)))
        for title in ("abandoned ideas", "future plans", "design principles", "history", "summaries", "never uploaded"):
            self.assertIn(title, full)
        default = ANSI.sub("", ideas.report(ideas.mine(self.cfg())))
        self.assertIn("abandoned ideas", default)
        self.assertNotIn("design principles", default)
        self.assertIn("opt-in", ANSI.sub("", ideas.report({})))


if __name__ == "__main__":
    unittest.main()
