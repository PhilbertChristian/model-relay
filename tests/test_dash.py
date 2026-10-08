"""python3 -m unittest tests.test_dash -v"""
import io
import json
import os
import re
import signal
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from relay import dash, ui
from relay.bus import Bus
from relay.dash import DashState, attach, render, tail

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
T0 = 1_760_000_000.0
TASKS = {  # slot -> (lane, project, task)
    1: ("claude", "relay", "Add retry with jitter to the Supabase telemetry push"),
    2: ("claude", "nightshift", "Write tests for idle_intervals across DST changes"),
    3: ("codex", "greet-cli", "Add a --name flag and document it in the README"),
    4: ("agent37", "weekend-web", "Add a pricing section to the landing page"),
    5: ("claude", "api", "Add a health() endpoint with a unit test"),
    6: ("codex", "relay", "Document the burn-week config keys"),
}
NEXT = {3: ("greet-cli", "Add shell completions for bash and zsh"), 2: ("nightshift", "Handle Monday 09:00 resets across timezones"),
        6: ("relay", "Add a CHANGELOG entry for burn-week")}
LATER = (("weekend-web", "Add an FAQ section with three common questions"), ("api", "Return 404 JSON for unknown routes"),
         ("relay", "Add --json output to relay burn usage"), ("greet-cli", "Add a --shout flag that upper-cases the greeting"),
         ("nightshift", "Show the next downtime window in relay burn capacity"))
ENDS = {  # second -> (slot, status, extra agent_end fields)
    30: (3, "done", {"commit": "a1b2c3d", "diffstat": " 2 files changed, 42 insertions(+), 3 deletions(-)", "tests_ok": True}),
    40: (4, "limited", {"summary": "monthly budget exhausted (402)"}),
    50: (5, "blocked", {"summary": "BLOCKED: needs a staging DATABASE_URL to run the migration"}),
    60: (2, "done", {"commit": "9f8e7d6", "diffstat": " 3 files changed, 118 insertions(+), 20 deletions(-)", "tests_ok": True}),
    70: (6, "failed", {"summary": "tests failed: test_config_keys", "diffstat": "+4 -0", "tests_ok": False}),
}


def sample_rows(end: bool = False, seconds: int = 90) -> list[dict]:
    """A synthetic burn-week run: 3 lanes, 6 agents on 8 slots, a usage limit, every completion status."""
    rows: list[dict] = []

    def emit(t: float, event: str, **data) -> None:
        rows.append({"ts": T0 + t, "session": "s1", "host": "local", "event": event, **data})

    resets = T0 + 41.5 * 3600
    emit(0, "swarm_start", run="bw-1009-7f3a", max_agents=8, queue=14, resets_at=resets,
         lanes=[{"name": "claude", "kind": "mock", "subscription": "claude-max", "as": "claude"},
                {"name": "codex", "kind": "codex", "subscription": "codex", "as": "codex"},
                {"name": "agent37", "kind": "agent37", "subscription": "agent37", "as": "agent37"}],
         usages=[{"name": "claude-max", "lane": "claude", "unit": "tokens", "used": 241e6, "limit": 400e6, "resets_at": resets},
                 {"name": "codex", "lane": "codex", "unit": "tokens", "used": 61e6, "limit": 100e6, "resets_at": resets},
                 {"name": "agent37", "lane": "agent37", "unit": "usd", "used": 3.1, "limit": 20.0, "resets_at": resets}])
    for lane, sub, agents, rate, used, limit, unit in (("claude", "claude-max", 4, 3.6e6, 241e6, 400e6, "tokens"),
                                                       ("codex", "codex", 2, 0.86e6, 61e6, 100e6, "tokens"),
                                                       ("agent37", "agent37", 1, 0.38, 3.1, 20.0, "usd")):
        emit(0.5, "pace", lane=lane, subscription=sub, unit=unit, used=used, limit=limit, pct=used / limit,
             remaining=limit - used, agents=agents, target_rate=rate, hours_left=41.5)
    queue = [(f"{p}-{k:06x}", p, x) for k, (_, p, x) in TASKS.items()] + [
        (f"{p}-{k + 100:06x}", p, x) for k, (p, x) in NEXT.items()] + [(f"{p}-{i:06x}", p, x) for i, (p, x) in enumerate(LATER, 200)]
    for tid, project, text in queue:
        emit(0.6, "task_queued", task_id=tid, project=project, text=text, source="todo")
    live = {}
    for k, (lane, project, text) in TASKS.items():
        emit(k, "agent_start", agent=k, task_id=f"{project}-{k:06x}", project=project, text=text, lane=lane,
             branch=f"relay/burn/{project}-{k:06x}", worktree=f"/tmp/wt/{project}-{k:06x}")
        live[k] = [lane, project, f"{project}-{k:06x}", 0, 0.0, k]
    for t in range(2, seconds + 1):
        if t in ENDS:
            k, status, extra = ENDS[t][0], ENDS[t][1], dict(ENDS[t][2])
            lane, project, tid, tokens, usd, _ = live.pop(k)
            emit(t, "agent_end", agent=k, task_id=tid, project=project, lane=lane, status=status, tokens=tokens, usd=round(usd, 4),
                 seconds=t - k, summary=extra.pop("summary", "done"), commit=extra.pop("commit", None), **extra)
            if status == "limited":
                emit(t, "lane_limited", lane=lane, until=T0 + 3 * 3600, reason="monthly budget exhausted (402)")
            elif k in NEXT:
                project, text = NEXT[k]
                tid = f"{project}-{k + 100:06x}"
                emit(t + 0.5, "agent_start", agent=k, task_id=tid, project=project, text=text, lane=lane, branch=f"relay/burn/{tid}")
                live[k] = [lane, project, tid, 0, 0.0, t]
        for k, a in sorted(live.items()):
            if t > a[5] and (t + k) % 2 == 0:
                a[3] += 4000 * k + 1500 * ((t * k) % 7)
                a[4] = a[3] * 1e-5 if a[0] == "agent37" else 0.0
                emit(t + k / 10, "agent_progress", agent=k, task_id=a[2], lane=a[0], tokens=a[3], usd=round(a[4], 4),
                     note=("reading files", "editing src", "running tests")[(t // 6 + k) % 3])
    if end:
        emit(seconds + 2, "swarm_end", run="bw-1009-7f3a", done=2, blocked=1, failed=1, limited=1,
             tokens=sum(r.get("tokens", 0) for r in rows if r["event"] == "agent_end"), by_lane={},
             usd=round(sum(r.get("usd", 0) for r in rows if r["event"] == "agent_end"), 4),
             report="BURN.md", seconds=seconds + 2, reason="queue empty")
    return rows


def folded(rows=None) -> DashState:
    st = DashState()
    for r in sample_rows() if rows is None else rows:
        st.apply(r)
    return st


def plain(frame: str) -> list[str]:
    return ANSI.sub("", frame).split("\n")


class Fold(unittest.TestCase):
    def test_counts_tokens_and_slots(self):
        st = folded()
        self.assertEqual(st.run, "bw-1009-7f3a")
        self.assertEqual(st.counts, {"done": 2, "blocked": 1, "failed": 1, "limited": 1})
        self.assertEqual(len(st.slots), 8)
        self.assertEqual(sum(s.status == "running" for s in st.slots.values()), 4)
        self.assertEqual(st.slots[7].status, "idle")
        self.assertEqual(st.tokens, sum(st.burn[lane][0] for lane in st.burn))
        self.assertEqual(st.slots[3].text, NEXT[3][1])
        self.assertEqual(st.landed[0]["diff"], (2, 42, 3))
        self.assertEqual(st.landed[-1]["diff"], (None, 4, 0))
        self.assertIn("agent37", st.limited)
        self.assertEqual(st.queued, 14)
        self.assertEqual([p for p, _ in st.pending.values()], [p for p, _ in LATER])

    def test_replayed_rows_do_not_double_count(self):
        rows = sample_rows(end=True)
        st = folded(rows[:-1] + [r for r in rows if r["event"] in ("agent_end", "agent_progress")] + rows[-1:])
        self.assertEqual((st.counts, st.tokens), (folded(rows).counts, folded(rows).tokens))
        retry = {"agent": 4, "task_id": "weekend-web-000004", "lane": "codex"}       # the limited task, on another lane
        st.apply({"ts": T0 + 200, "event": "agent_start", **retry, "project": "weekend-web", "text": "retry"})
        st.apply({"ts": T0 + 201, "event": "agent_progress", **retry, "tokens": 1000})
        self.assertEqual((st.slots[4].status, st.tokens), ("running", folded(rows).tokens + 1000))

    def test_meter_never_runs_backwards_on_a_stale_pace_row(self):
        st = folded()
        m = st.meters["claude-max"]
        before = st.used(m)
        self.assertGreater(before, 241e6)
        st.apply({"ts": st.now + 1, "event": "pace", "lane": "claude", "subscription": "claude-max", "used": 241e6, "agents": 4})
        self.assertEqual(st.used(m), before)
        st.apply({"ts": st.now + 1, "event": "pace", "lane": "claude", "subscription": "claude-max", "used": before + 5e6})
        self.assertEqual(st.used(m), before + 5e6)

    def test_snapshot_is_compact_json(self):
        st = folded(sample_rows(end=True))
        snap = json.loads(json.dumps(st.snapshot(), allow_nan=False))
        self.assertEqual(snap["totals"], {"tokens": st.tokens, "usd": st.usd, "running": 4, "queued": 5,
                                          "done": 2, "blocked": 1, "failed": 1, "limited": 1})
        self.assertEqual((snap["next_reset"]["plan"], snap["landed"][0]["status"]), ("claude-max", "failed"))
        self.assertAlmostEqual(snap["meters"]["claude-max"]["pct"], st.used(st.meters["claude-max"]) / 400e6)
        self.assertLess(len(json.dumps(snap)), 20_000)                         # no sample logs in it

    def test_swarm_end_and_new_run(self):
        st = folded(sample_rows(end=True))
        self.assertEqual(st.end["report"], "BURN.md")
        st.apply({"ts": T0 + 999, "event": "swarm_start", "run": "next", "max_agents": 2})
        self.assertEqual((st.run, len(st.slots), st.counts["done"], st.end), ("next", 2, 0, None))

    def test_malformed_rows_are_ignored(self):
        st = folded()
        for row in (None, "x", {}, {"event": 3}, {"event": "agent_progress", "agent": "x", "tokens": "lots"},
                    {"event": "pace", "used": "?"}, {"event": "agent_end", "ts": "soon", "seconds": None},
                    {"event": "swarm_start", "lanes": "nope", "usages": [None, 1]}, {"event": "unknown", "ts": T0}):
            st.apply(row)
        self.assertIsInstance(render(st), str)

    def test_untrusted_text_is_sanitised(self):
        st = folded()
        st.apply({"ts": T0 + 99, "event": "agent_start", "agent": 7, "task_id": "x", "lane": "codex", "project": "p\x1b[31m",
                  "text": "evil \x1b[2J\x07 task api_key=sk-ant-abcdefghijklmnopqrstuvwxyz"})
        st.apply({"ts": T0 + 99, "event": "lane_limited", "lane": "evil\x1b]0;pwned\x07", "until": T0 + 999})
        with mock.patch.object(ui, "_COLOR", True):
            frame = render(st)
        self.assertNotIn("\x1b[2J", frame)
        self.assertNotIn("\x1b]", frame)
        self.assertNotIn("\x07", frame)
        self.assertNotIn("abcdefghijklmnop", frame)
        self.assertEqual(set(re.findall(r"\x1b\[[0-9;?]*([A-Za-z])", frame)), {"m"})   # colors only, no cursor moves

    def test_number_and_diffstat_formats(self):
        self.assertEqual([dash._si(v) for v in (0, 999, 84_200, 999_999, 1_240_000, 400e6)], ["0", "999", "84.2k", "1.00M", "1.24M", "400M"])
        self.assertEqual((dash._si(3.6e6, trim=True), dash._si(20, "usd", trim=True), dash._si(6.63, "usd")), ("3.6M", "$20", "$6.63"))
        self.assertEqual(dash._diffstat({"diffstat": " 1 file changed, 1 insertion(+)"}), (1, 1, 0))
        self.assertEqual(dash._diffstat({"diffstat": "+12 -3", "files": 2}), (2, 12, 3))
        self.assertEqual(dash._diffstat({"insertions": 5, "deletions": 1}), (None, 5, 1))


class Render(unittest.TestCase):
    def check_bounds(self, frame: str, w: int, h: int):
        lines = plain(frame)
        self.assertLessEqual(len(lines), h)
        for line in lines:
            self.assertLessEqual(len(line), w, repr(line))
            self.assertLessEqual(dash._w(line), w, repr(line))

    def test_key_content(self):
        frame = "\n".join(plain(render(folded(), 120, 34)))
        for needle in ("relay burn week", "bw-1009-7f3a", "resets in 1d", "claude-max", "codex", "agent37", "target 97%",
                       "4/8 running", "greet-cli", "Add shell completions", "relay/burn/", "nightshift-000002",
                       "+118", "tests ✓", "tests ✗", "⊘ limited", "◆", "✗", "✓", "Σ", "5 queued", "burn rate",
                       "tok/s", "recently landed", "working", "DATABASE_URL"):
            self.assertIn(needle, frame)

    def test_every_plan_shows_its_own_reset_and_the_header_the_soonest(self):
        st = folded()
        self.assertEqual("\n".join(plain(render(st))).count("(41h)"), 3)        # one countdown per meter
        st.meters["codex"].resets_at = st.now + 5 * 3600                        # codex now resets first
        st.meters["agent37"].resets_at = st.now + 20 * 86400
        lines = plain(render(st, 120, 34))
        self.assertIn("codex resets in 05:00:00", lines[0])
        self.assertIn("(5h)", next(x for x in lines if "│ codex" in x))
        self.assertIn("(20d)", next(x for x in lines if "│ agent37" in x))
        self.assertIn("↻ 5h", "\n".join(plain(render(st, 48, 14))))           # narrow: still a clock per plan
        st.lanes.pop("codex")                                                   # not burned by this swarm: skipped
        st.burn.pop("codex")
        self.assertIn("claude-max resets in", plain(render(st, 120, 34))[0])

    def test_bounds_at_many_sizes_with_and_without_color(self):
        states = (DashState(), folded(), folded(sample_rows(end=True)), folded(sample_rows(seconds=8)))
        for color in (False, True):
            with mock.patch.object(ui, "_COLOR", color):
                for st in states:
                    for w, h in ((120, 34), (160, 50), (100, 30), (96, 24), (80, 24), (60, 20), (48, 12), (40, 12),
                                 (30, 8), (20, 5), (10, 3), (1, 1)):
                        self.check_bounds(render(st, w, h), w, h)

    def test_full_size_frame_fills_the_screen(self):
        with mock.patch.object(ui, "_COLOR", True):
            frame = render(folded(), 120, 34)
        self.assertIn("\x1b[", frame)
        lines = plain(frame)
        self.assertEqual(len(lines), 34)
        self.assertTrue(all(len(line) == 120 for line in lines if line), [len(x) for x in lines])

    def test_deterministic(self):
        st = folded()
        self.assertEqual(render(st), render(st))
        self.assertEqual(render(st, 90, 28), render(folded(), 90, 28))

    def test_narrow_degrades(self):
        frame = "\n".join(plain(render(folded(), 72, 22)))
        self.assertIn("relay burn week", frame)
        self.assertIn("claude-max", frame)
        self.assertIn("idle slots", frame)
        with mock.patch.object(ui, "_COLOR", False):
            self.assertIn("░", render(folded()))                       # meters stay readable without color


class Tail(unittest.TestCase):
    def test_once_folds_a_file_with_garbage_and_a_partial_line(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "events.jsonl"
            body = "\n".join(json.dumps(r) for r in sample_rows(end=True))
            p.write_text("not json\n" + body + '\n{"ts": 1, "event": "agent_pro')
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"COLUMNS": "110", "LINES": "30"}), redirect_stdout(out):
                self.assertIsNone(tail(str(p), once=True))
        lines = plain(out.getvalue().rstrip("\n"))
        self.assertLessEqual(len(lines), 30)
        self.assertTrue(all(len(line) <= 110 for line in lines))
        text = "\n".join(lines)
        self.assertIn("DONE", text)
        self.assertIn("BURN.md", text)

    def test_once_on_a_missing_file(self):
        out = io.StringIO()
        with redirect_stdout(out):
            tail("/nonexistent/relay/events.jsonl", once=True)
        self.assertIn("waiting for events", ANSI.sub("", out.getvalue()))

    def test_follow_reader_waits_for_whole_lines(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "events.jsonl"
            feed = dash._Follow(str(p))
            self.assertEqual(feed.read(), [])
            p.write_text('{"event": "task_queued", "ts": 1}\n{"event": "task_q')
            self.assertEqual([r["event"] for r in feed.read()], ["task_queued"])
            with p.open("a") as f:
                f.write('ueued", "ts": 2}\n')
            self.assertEqual([r["ts"] for r in feed.read()], [2])
            p.write_text('{"event": "swarm_start", "ts": 3}\n')          # truncated: start over
            self.assertEqual([r["ts"] for r in feed.read()], [3])


class Attach(unittest.TestCase):
    def test_noop_without_a_tty(self):
        bus, out = mock.Mock(), io.StringIO()
        with redirect_stdout(out):
            stop = attach(bus)
            stop()
        bus.subscribe.assert_not_called()
        self.assertEqual(out.getvalue(), "")

    def test_forced_attach_draws_and_restores_the_terminal(self):
        offline = {"SUPABASE_URL": "", "SUPABASE_KEY": "", "SUPABASE_SERVICE_ROLE_KEY": ""}
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, offline):
            bus, out = Bus(log_dir=d), io.StringIO()
            rows = sample_rows(end=True)
            bus.emit("swarm_start", **{k: v for k, v in rows[0].items() if k not in ("ts", "session", "host", "event")})
            before = signal.getsignal(signal.SIGWINCH) if hasattr(signal, "SIGWINCH") else None
            with redirect_stdout(out):
                stop = attach(bus, refresh=0.01, force=True)
                for r in rows[1:40]:
                    bus.emit(r["event"], **{k: v for k, v in r.items() if k not in ("ts", "session", "host", "event")})
                time.sleep(0.08)
                stop()
                stop()                                                   # idempotent
            self.assertEqual(len(bus._subs), 0)
            if before is not None:
                self.assertEqual(signal.getsignal(signal.SIGWINCH), before)
        text = out.getvalue()
        self.assertTrue(text.startswith("\x1b[?1049h\x1b[?25l"))
        self.assertIn("\x1b[?7h\x1b[?25h\x1b[?1049l", text)
        final = ANSI.sub("", text.rsplit("\x1b[?1049l", 1)[1])
        self.assertIn("bw-1009-7f3a", final)
        self.assertIn("relay burn week", final)

    def test_screen_repaints_only_changed_lines(self):
        out, st = io.StringIO(), folded()
        screen = dash._Screen(out)
        with mock.patch.object(dash._Screen, "size", staticmethod(lambda: (120, 34))):
            screen.paint(st)
            first = out.getvalue()
            screen.paint(st)
            self.assertEqual(out.getvalue(), first)                     # nothing changed, nothing written
            st.tick(st.now + 0.5)                                       # spinners and clock move
            screen.paint(st)
        delta = out.getvalue()[len(first):]
        self.assertTrue(0 < delta.count(";1H") < 34)


if __name__ == "__main__":
    unittest.main()
