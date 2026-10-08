"""CLI wiring for burn week|discover|ideas|usage|demo, dash and web (handlers and sibling modules mocked),
and the existing commands still parsing exactly as before. Offline: no keys, no network, no real repos."""
import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import relay
from relay import __main__ as cli

ROOT = Path(__file__).resolve().parent.parent
NO_SUPABASE = {"SUPABASE_URL": "", "SUPABASE_KEY": "", "SUPABASE_SERVICE_ROLE_KEY": ""}   # Bus/Telemetry stay local


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err), mock.patch.dict(os.environ, NO_SUPABASE):
        try:
            rc = cli.main(argv)
        except SystemExit as e:
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


def fake_modules(**mods):
    """Stand in for relay.<name> whether or not the real sibling module exists yet."""
    stack = contextlib.ExitStack()
    for name, mod in mods.items():
        stack.enter_context(mock.patch.dict(sys.modules, {f"relay.{name}": mod}))
        stack.enter_context(mock.patch.object(relay, name, mod, create=True))
    return stack


def write_cfg(d, **cfg):
    p = os.path.join(d, "burner-week.json")
    Path(p).write_text(json.dumps(cfg))
    return p


class Wiring(unittest.TestCase):
    """Each new command reaches its handler with the parsed flags."""

    def call(self, handler, argv):
        with mock.patch.object(cli, handler, return_value=0) as h:
            rc, out, err = run(argv)
        self.assertEqual(rc, 0, out + err)
        h.assert_called_once()
        return h.call_args.args[0]

    def test_burn_week(self):
        a = self.call("_burn_week", ["burn", "week", "-b", "w.json", "--roots", "/a", "/b", "--agents", "6", "--hours",
                                     "2.5", "--ideas", "--lanes", "claude,codex", "--dry-run", "--no-dash", "--web", "3738"])
        self.assertEqual((a.burner, a.roots, a.agents, a.hours, a.ideas, a.lanes, a.dry_run, a.no_dash, a.web),
                         ("w.json", ["/a", "/b"], 6, 2.5, True, "claude,codex", True, True, 3738))
        a = self.call("_burn_week", ["burn", "week", "PLAN.md"])
        self.assertEqual((a.plan, a.burner, a.roots, a.agents, a.ideas, a.dry_run, a.no_dash, a.web),
                         ("PLAN.md", None, None, None, False, False, False, None))

    def test_burn_discover_ideas_usage(self):
        a = self.call("_burn_discover", ["burn", "discover", "--roots", "/x", "-o", "PLAN.md"])
        self.assertEqual((a.roots, a.out), (["/x"], "PLAN.md"))
        self.assertEqual(self.call("_burn_ideas", ["burn", "ideas", "--days", "7"]).days, 7.0)
        self.assertEqual(self.call("_burn_usage", ["burn", "usage", "-b", "w.json"]).burner, "w.json")

    def test_burn_demo(self):
        a = self.call("_burn_demo", ["burn", "demo", "--agents", "8", "--speed", "2", "--web", "3737",
                                     "--record", "docs/sample-events.jsonl", "--no-dash"])
        self.assertEqual((a.agents, a.speed, a.web, a.record, a.no_dash), (8, 2.0, 3737, "docs/sample-events.jsonl", True))

    def test_dash_and_web(self):
        self.assertEqual(self.call("_dash", ["dash", "--events", "e.jsonl"]).events, "e.jsonl")
        self.assertIsNone(self.call("_dash", ["dash"]).events)
        a = self.call("_web", ["web", "--port", "4000", "--events", "e.jsonl", "--supabase"])
        self.assertEqual((a.port, a.events, a.supabase), (4000, "e.jsonl", True))
        a = self.call("_web", ["web"])
        self.assertEqual((a.port, a.events, a.supabase), (3737, None, False))

    def test_flags_are_checked_per_action(self):
        for argv, want in ((["burn", "demo", "--roots", "x"], "burn demo doesn't take --roots"),
                           (["burn", "usage", "--dry-run"], "burn usage doesn't take --dry-run"),
                           (["burn", "discover", "PLAN.md"], "burn discover doesn't take a planning doc"),
                           (["burn", "week", "--now", "--no-pr"], "burn week doesn't take --no-pr, --now"),
                           (["burn", "capacity", "--dry-run"], "burn capacity doesn't take --dry-run"),
                           (["burn", "demo", "--speed", "0"], "--speed must be > 0"),
                           (["burn", "week", "--agents", "0"], "--agents at least 1")):
            rc, _, err = run(argv)
            self.assertEqual(rc, 2, argv)
            self.assertIn(want, err)

    def test_missing_sibling_says_not_built_yet(self):
        for exc, want in ((ModuleNotFoundError("No module named 'relay.swarm'", name="relay.swarm"), "relay.swarm"),
                          (ImportError("cannot import name 'usage' from 'relay' (/x/relay/__init__.py)", name="relay"),
                           "relay.usage"),
                          (AttributeError("module 'relay.pace' has no attribute 'plan_pace'"), "relay.pace.plan_pace")):
            with mock.patch.object(cli, "_burn_week", side_effect=exc):
                rc, out, _ = run(["burn", "week"])
            self.assertEqual(rc, 2)
            self.assertIn(f"{want} is not built yet", out)
        with mock.patch.object(cli, "_web", side_effect=AttributeError("'NoneType' object has no attribute 'x'")):
            with self.assertRaises(AttributeError):                  # real bugs are not hidden
                cli.main(["web"])


class Handlers(unittest.TestCase):
    """Handlers call the SWARM_SPEC contract functions (sibling modules faked)."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        env = mock.patch.dict(os.environ, NO_SUPABASE)
        env.start()
        self.addCleanup(env.stop)

    def test_week_cfg_falls_back_to_the_example_with_a_note(self):
        cwd = os.getcwd()
        os.chdir(self.d)
        try:
            with redirect_stdout(io.StringIO()) as out:
                cfg = cli._week_cfg(None)
            self.assertIn("note: no ./burner-week.json", out.getvalue())
            self.assertEqual(cfg, json.loads((ROOT / "examples" / "burner-week.json").read_text()))
            write_cfg(self.d, roots=["/here"])
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli._week_cfg(None), {"roots": ["/here"]})   # ./burner-week.json wins, no note
            self.assertEqual(out.getvalue(), "")
        finally:
            os.chdir(cwd)
        with redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit):
            cli._week_cfg(os.path.join(self.d, "missing.json"))
        self.assertIn("not found", out.getvalue())

    def test_example_config_is_the_documented_one(self):
        cfg = json.loads((ROOT / "examples" / "burner-week.json").read_text())
        self.assertEqual([ln["kind"] for ln in cfg["lanes"]], ["claude", "codex", "agent37", "relay", "orca"])
        self.assertEqual([ln.get("enabled", True) for ln in cfg["lanes"]], [True, True, True, True, False])
        self.assertFalse(cfg["ideas"]["enabled"])
        self.assertEqual({s["kind"] for s in cfg["subscriptions"]},
                         {"claude_code", "codex", "agent37_budget", "api_budget", "monid_credits"})
        subs = {s["name"] for s in cfg["subscriptions"]}
        self.assertTrue(all(ln.get("subscription", "") in subs | {""} for ln in cfg["lanes"]))
        self.assertTrue({"week", "roots", "max_depth", "max_projects", "per_project", "max_agents", "data_dir",
                         "monid", "providers", "models"} <= set(cfg))
        from relay.config import build_router
        build_router(cfg)                                      # the relay lane's ladder is a valid relay.json ladder

    def test_burn_week_runs_the_swarm_on_a_bus_and_prints_the_summary(self):
        cfg = write_cfg(self.d, lanes=[{"kind": "claude", "name": "claude"}, {"kind": "orca", "name": "orca", "enabled": False}],
                        ideas={"enabled": False, "since_days": 21})
        seen = {}

        def burn_week(c, **kw):
            seen.update(cfg=c, **kw)
            kw["bus"].emit("agent_start", agent=1, task_id="t1", project="orbit-api", text="add rate limiting",
                           lane="orca", branch="relay/burn/t1", worktree="/w/t1")
            kw["bus"].emit("agent_end", agent=1, task_id="t1", project="orbit-api", lane="orca", status="done",
                           summary="ok", tokens=1200, usd=0.0, commit="abc1234def", diffstat="+12 -3", tests_ok=True)
            return {"done": 1, "tokens": 1200, "report": os.path.join(self.d, "BURN.md")}

        with fake_modules(swarm=types.SimpleNamespace(burn_week=burn_week)):
            rc, out, err = run(["-C", self.d, "burn", "week", "PLAN.md", "-b", cfg, "--roots", "/r1", "--agents", "3",
                                "--hours", "1.5", "--ideas", "--lanes", "orca", "--no-dash"])
        self.assertEqual(rc, 0, out + err)
        self.assertEqual((seen["roots"], seen["max_agents"], seen["hours"], seen["plan_path"]), (["/r1"], 3, 1.5, "PLAN.md"))
        self.assertNotIn("dry_run", seen)
        self.assertTrue(seen["cfg"]["ideas"]["enabled"])                     # --ideas is the opt-in
        self.assertEqual(seen["cfg"]["lanes"], [{"kind": "orca", "name": "orca", "enabled": True}])
        self.assertEqual(Path(seen["bus"].tel.path), Path(self.d) / ".relay" / "events.jsonl")
        for want in ("▶", "orbit-api: add rate limiting", "1 done", "BURN.md", "relay/burn/t1", "abc1234"):
            self.assertIn(want, out)

    def test_burn_week_unknown_lane_is_an_error(self):
        cfg = write_cfg(self.d, lanes=[{"kind": "claude", "name": "claude"}])
        with fake_modules(swarm=types.SimpleNamespace(burn_week=mock.Mock())):
            rc, out, _ = run(["burn", "week", "-b", cfg, "--lanes", "nope"])
        self.assertEqual(rc, 2)
        self.assertIn("matches no lane", out)

    def test_burn_week_dry_run_prints_pace_and_queue(self):
        plan = {"lane": "claude", "subscription": "claude-max", "unit": "tokens", "remaining": 88e6, "hours_left": 30.0,
                "target_rate": 2.5e6, "agents": 4, "used": 312e6, "limit": 400e6, "note": ""}
        res = {"dry_run": True, "pace": [plan], "schedule": "claude-max: 22% left, 30h to reset → 4 agents",
               "lanes": [{"name": "claude", "available": True}, {"name": "codex", "available": False,
                                                                  "why": "codex not found on PATH"}],
               "projects": [{"name": "orbit-api"}],
               "queue": [{"task_id": "orbit-api-1a2b3c", "project": "orbit-api", "text": "add rate limiting",
                          "source": "todo", "priority": 1.0}]}
        swarm = types.SimpleNamespace(burn_week=mock.Mock(return_value=res))
        pace = types.SimpleNamespace(report=mock.Mock(return_value="PACE-REPORT"))
        with fake_modules(swarm=swarm, pace=pace):
            rc, out, err = run(["-C", self.d, "burn", "week", "-b", write_cfg(self.d, lanes=[]), "--dry-run",
                                "--agents", "5"])
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(swarm.burn_week.call_args.kwargs["dry_run"], True)
        self.assertEqual(swarm.burn_week.call_args.kwargs["max_agents"], 5)
        self.assertNotIn("bus", swarm.burn_week.call_args.kwargs)                  # a dry run writes no ledger
        (p,), = pace.report.call_args.args
        self.assertEqual((p.lane, p.agents, p.limit), ("claude", 4, 400e6))       # report() gets attribute access
        for want in ("PACE-REPORT", "→ 4 agents", "lane codex unavailable: codex not found", "queue · 1 tasks from 1",
                     "orbit-api", "add rate limiting", "nothing started"):
            self.assertIn(want, out)
        self.assertFalse((Path(self.d) / ".relay").exists())

    def test_burn_discover_writes_the_plan_but_never_overwrites(self):
        cfg = write_cfg(self.d, roots=["~/code"], max_depth=2, max_projects=5, per_project=2)
        disc = types.SimpleNamespace(discover_projects=mock.Mock(return_value=["P"]), report=mock.Mock(return_value="REPORT"),
                                     to_plan_md=mock.Mock(return_value="## p\n- [ ] x\n"))
        out_md = os.path.join(self.d, "PLAN.md")
        with fake_modules(discover=disc):
            rc, out, _ = run(["burn", "discover", "-b", cfg, "-o", out_md])
            self.assertEqual(rc, 0)
            self.assertEqual(Path(out_md).read_text(), "## p\n- [ ] x\n")
            rc2, out2, _ = run(["burn", "discover", "-b", cfg, "--roots", "/only", "-o", out_md])
        self.assertIn("REPORT", out)
        args, kw = disc.discover_projects.call_args_list[0]
        self.assertEqual((args[0], kw["max_depth"], kw["limit"]), ([os.path.expanduser("~/code")], 2, 5))
        disc.to_plan_md.assert_called_once_with(["P"], None, per_project=2)
        self.assertEqual(disc.discover_projects.call_args_list[1].args[0], ["/only"])
        self.assertEqual((rc2, Path(out_md).read_text()), (1, "## p\n- [ ] x\n"))
        self.assertIn("not overwriting", out2)

    def test_burn_ideas_is_the_opt_in(self):
        ideas = types.SimpleNamespace(mine=mock.Mock(return_value={}), report=mock.Mock(return_value="IDEAS"))
        with fake_modules(ideas=ideas):
            rc, out, _ = run(["burn", "ideas", "-b", write_cfg(self.d, ideas={"enabled": False, "since_days": 21}),
                              "--days", "7"])
        self.assertEqual(rc, 0)
        self.assertEqual(ideas.mine.call_args.args[0]["ideas"], {"enabled": True, "since_days": 7.0})
        self.assertIn("IDEAS", out)

    def test_burn_usage_reads_the_cwd_ledger(self):
        usage = types.SimpleNamespace(weekly_usage=mock.Mock(return_value=["u"]), report=mock.Mock(return_value="USAGE"))
        cfg = write_cfg(self.d, subscriptions=[])
        with fake_modules(usage=usage):
            rc, out, _ = run(["-C", self.d, "burn", "usage", "-b", cfg])
        self.assertEqual(rc, 0)
        usage.weekly_usage.assert_called_once_with({"subscriptions": []}, os.path.join(self.d, ".relay/events.jsonl"))
        usage.report.assert_called_once_with(["u"])
        self.assertIn("USAGE", out)

    def test_burn_demo_calls_run_demo(self):
        with mock.patch("relay.demo_week.run_demo") as rd, mock.patch.object(cli, "_block", return_value=0) as blk:
            self.assertEqual(run(["burn", "demo"])[0], 0)
            rd.assert_called_with(agents=8, speed=1.0, dash=True, web_port=None, record=None)
            blk.assert_called_with(None)
            run(["burn", "demo", "--agents", "3", "--speed", "4", "--web", "3999", "--record", "s.jsonl", "--no-dash"])
            rd.assert_called_with(agents=3, speed=4.0, dash=False, web_port=3999, record="s.jsonl")
            blk.assert_called_with("http://127.0.0.1:3999/")

    def test_dash_tails_the_cwd_ledger(self):
        dash = types.SimpleNamespace(tail=mock.Mock())
        with fake_modules(dash=dash):
            self.assertEqual(run(["-C", self.d, "dash"])[0], 0)
            run(["dash", "--events", "x.jsonl"])
        self.assertEqual([c.args[0] for c in dash.tail.call_args_list], [os.path.join(self.d, ".relay/events.jsonl"), "x.jsonl"])

    def test_web_serves_until_ctrl_c(self):
        srv = mock.Mock(url="http://127.0.0.1:4001/")
        web = types.SimpleNamespace(serve=mock.Mock(return_value=srv), Feed=mock.Mock(return_value="FEED"))
        supa = types.SimpleNamespace(available=mock.Mock(return_value=False), recent_events=mock.Mock(return_value=[]))
        with fake_modules(web=web, supa=supa), mock.patch.object(cli, "_block", return_value=0) as blk:
            self.assertEqual(run(["-C", self.d, "web", "--port", "4001"])[0], 0)
            web.serve.assert_called_with(port=4001, events_path=os.path.join(self.d, ".relay/events.jsonl"), bus=None)
            self.assertEqual(blk.call_args.args[0], "http://127.0.0.1:4001/")
            srv.shutdown.assert_called_once()
            rc, out, _ = run(["web", "--supabase"])
            self.assertEqual(rc, 2)
            self.assertIn("SUPABASE_URL", out)
            supa.available.return_value = True
            self.assertEqual(run(["web", "--supabase"])[0], 0)
            self.assertEqual(web.serve.call_args.kwargs["bus"], "FEED")
            web.Feed.call_args.args[0](12.5)                               # the Feed polls Supabase from a ts
            supa.recent_events.assert_called_with(since_ts=12.5)
            web.serve.side_effect = OSError("address in use")
            self.assertEqual(run(["web"])[0], 1)


class ExistingCommands(unittest.TestCase):
    """burn capacity|plan|run, savings, supervise, mcp, serve and stats parse and dispatch as before."""

    def test_burn_capacity_default_burner(self):
        with mock.patch.object(cli, "load", return_value={"models": []}) as ld, \
                mock.patch("relay.capacity.assess", return_value=[]) as assess, \
                mock.patch("relay.capacity.report", return_value="CAPACITY"):
            rc, out, _ = run(["-C", "/w", "burn", "capacity"])
        self.assertEqual(rc, 0)
        ld.assert_called_once_with("burner.json")
        assess.assert_called_once_with({"models": []}, os.path.join("/w", ".relay/events.jsonl"))
        self.assertIn("CAPACITY", out)

    def test_burn_plan_and_run(self):
        cfg = {"models": []}
        with mock.patch.object(cli, "load", return_value=cfg) as ld, mock.patch("relay.capacity.assess", return_value=[]), \
                mock.patch("relay.capacity.report", return_value=""):
            rc, out, _ = run(["burn", "plan", str(ROOT / "examples" / "PLAN.md"), "-b", "b.json"])
            self.assertEqual(rc, 0)
            ld.assert_called_with("b.json")
            self.assertIn("queue ·", out)
            self.assertEqual(run(["burn", "plan"])[0], 2)                      # still needs a planning doc
            with mock.patch("relay.burn.burn", return_value={"status": "finished"}) as burn:
                rc, _, _ = run(["-C", "/w", "burn", "run", "PLAN.md", "-b", "b.json", "--now", "--wait", "--hours", "2",
                                "--max-tasks", "3", "--no-pr"])
                self.assertEqual(rc, 0)
                burn.assert_called_once_with("PLAN.md", cfg, "/w", now=True, hours=2.0, max_tasks=3, wait=True, pr=False)
                burn.return_value = {"status": "stopped"}
                self.assertEqual(run(["burn", "run", "PLAN.md"])[0], 1)
                self.assertEqual(burn.call_args.kwargs, dict(now=False, hours=None, max_tasks=None, wait=False, pr=True))

    def test_other_commands(self):
        with mock.patch("relay.supervise.supervise") as sup:
            self.assertEqual(run(["supervise", "--instance", "i1", "fix", "it"])[0], 0)
        sup.assert_called_once_with("i1", "fix it", None, None, hang_s=180)
        with mock.patch("relay.mcp.main", return_value=0) as mcp:
            self.assertEqual(run(["-C", "/w", "mcp"])[0], 0)
        mcp.assert_called_once_with(["-C", "/w"])
        with mock.patch("relay.serve.main", return_value=0) as srv:
            self.assertEqual(run(["serve", "--port", "9"])[0], 0)
        srv.assert_called_once_with(["--port", "9"])
        with mock.patch("relay.value.month") as month, mock.patch("relay.value.render", return_value="SAVINGS"):
            rc, out, _ = run(["savings", "--month", "2026-09", "--no-claude"])
        self.assertEqual((rc, out.strip()), (0, "SAVINGS"))
        self.assertEqual(month.call_args.args[0], "2026-09")
        rc, out, _ = run(["-c", str(ROOT / "examples" / "demo-mock.json"), "-C", tempfile.mkdtemp(), "stats"])
        self.assertEqual((rc, out.strip()), (0, "no events yet"))
        self.assertEqual(run(["bogus"])[0], 2)


if __name__ == "__main__":
    unittest.main()
