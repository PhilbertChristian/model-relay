"""python3 -m unittest discover tests"""
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from relay.agent import Agent
from relay.blockers import BlockerDetector
from relay.config import build_router
from relay.providers import ProviderError, classify
from relay.telemetry import Telemetry
from relay.tools import Toolbox

ROOT = Path(__file__).resolve().parent.parent


def run_demo(name: str, task: str = "create hello_relay.py and run it"):
    cfg = json.loads((ROOT / "examples" / name).read_text())
    d = tempfile.mkdtemp()
    router = build_router(cfg)
    agent = Agent(router, Toolbox(d), Telemetry(d + "/.relay"), max_steps=cfg["max_steps"])
    with redirect_stdout(io.StringIO()) as out:
        final = agent.run(task)
    return final, out.getvalue(), d


class Classify(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(classify(429, ""), "rate_limit")
        self.assertEqual(classify(402, '{"error":"instance_budget_exhausted"}'), "budget")
        self.assertEqual(classify(429, "insufficient_quota"), "budget")
        self.assertEqual(classify(400, "This model's maximum context length is 128000"), "context")
        self.assertEqual(classify(404, "model not found"), "bad_model")
        self.assertEqual(classify(503, "upstream"), "transient")


class Router(unittest.TestCase):
    def cfg(self):
        return {"providers": {"a": {"kind": "mock"}, "b": {"kind": "mock"}, "c": {"kind": "mock"}},
                "models": [{"id": "cheap", "provider": "a", "model": "x", "tier": 0, "price_in": 0.1},
                           {"id": "cheap2", "provider": "b", "model": "y", "tier": 0, "price_in": 0.2},
                           {"id": "big", "provider": "c", "model": "z", "tier": 1, "price_in": 2,
                            "context_window": 1000000}]}

    def test_prefers_cheapest_then_fails_over(self):
        r = build_router(self.cfg())
        self.assertEqual(r.pick()[0].id, "cheap")
        r.record_error("cheap", ProviderError("rate_limit", "429", 429))
        self.assertEqual(r.pick()[0].id, "cheap2")

    def test_budget_kills_provider(self):
        r = build_router(self.cfg())
        r.pick()
        r.record_error("cheap2", ProviderError("budget", "402", 402))
        r.record_error("cheap", ProviderError("budget", "402", 402))
        self.assertEqual(r.pick()[0].id, "big")  # nothing left at tier 0 -> moves up

    def test_context_overflow_needs_bigger_window(self):
        r = build_router(self.cfg())
        r.pick()
        r.record_error("cheap", ProviderError("context", "too long"))
        self.assertEqual(r.pick()[0].id, "big")

    def test_escalate_and_deescalate(self):
        r = build_router(self.cfg())
        r.deescalate_after = 2
        r.pick()
        self.assertTrue(r.record_blocker("loop"))
        self.assertEqual(r.pick()[0].id, "big")
        r.record_clean_step(); r.record_clean_step()
        self.assertEqual(r.pick()[0].tier, 0)

    def test_soft_limit_retires_model(self):
        cfg = self.cfg(); cfg["models"][0]["max_tokens"] = 100
        r = build_router(cfg)
        r.pick()
        r.record_success("cheap", 90, 20, None)
        self.assertEqual(r.pick()[0].id, "cheap2")


class Blockers(unittest.TestCase):
    def test_loop(self):
        b = BlockerDetector()
        self.assertIsNone(b.on_tool_result("bash", '{"command":"x"}', "err", True))
        self.assertIsNone(b.on_tool_result("bash", '{"command": "x"}', "err", True))
        self.assertIn("loop", b.on_tool_result("bash", '{"command":"x"}', "err", True))

    def test_hung(self):
        self.assertIn("hung", BlockerDetector().on_tool_result("bash", "{}", "", True, hung=True))

    def test_no_progress(self):
        b = BlockerDetector(no_progress_limit=3)
        outs = [b.on_tool_result("read_file", json.dumps({"path": str(i)}), str(i), False) for i in range(3)]
        self.assertIn("wrong approach", outs[-1])


class EndToEnd(unittest.TestCase):
    def test_limit_then_hint_then_swap(self):
        final, out, d = run_demo("demo-mock.json")
        self.assertIn("Fixed", final)
        self.assertIn("rate_limit", out)
        self.assertIn("second opinion", out)
        self.assertIn("switch a37-mini → oai-pro", out)
        self.assertTrue((Path(d) / "hello_relay.py").exists())

    def test_hung_process_killed(self):
        final, out, _ = run_demo("demo-hang.json")
        self.assertIn("HUNG", out)
        self.assertIn("Done", final)


if __name__ == "__main__":
    unittest.main()


class SupervisorWatch(unittest.TestCase):
    def test_live_loop_and_failures(self):
        from relay.supervise import Stuck, Watch
        w = Watch(hang_s=999, repeat_limit=3, fail_limit=3)
        with redirect_stdout(io.StringIO()):
            w.tick("response.tool_call.started", {"tool": "terminal", "arguments": {"cmd": "npm test"}})
            w.tick("response.tool_call.started", {"tool": "terminal", "arguments": {"cmd": "npm test"}})
            with self.assertRaises(Stuck) as e:
                w.tick("response.tool_call.started", {"tool": "terminal", "arguments": {"cmd": "npm test"}})
        self.assertEqual(e.exception.kind, "loop")

    def test_hang(self):
        from relay.supervise import Stuck, Watch
        w = Watch(hang_s=0, repeat_limit=3, fail_limit=3)
        w.last_event -= 5
        with self.assertRaises(Stuck) as e:
            w.tick(None, {})
        self.assertEqual(e.exception.kind, "hung")


class Refusals(unittest.TestCase):
    def test_detector(self):
        from relay.blockers import looks_like_refusal
        self.assertTrue(looks_like_refusal("I'm sorry, but I can't help with that."))
        self.assertTrue(looks_like_refusal("I cannot assist with killing processes."))
        self.assertFalse(looks_like_refusal("Done. Tests pass."))
        self.assertFalse(looks_like_refusal("Fixed it. Note: I can't run the GPU tests here, but CPU tests pass." * 30))

    def test_over_refusal_is_unstuck(self):
        final, out, d = run_demo("demo-refusal.json", "kill whatever is running on port 8000 and add a dev server script")
        self.assertIn("benign", out)
        self.assertIn("switch a37-flash → a37-mini", out)
        self.assertIn("Freed port 8000", final)
        self.assertTrue((Path(d) / "serve.sh").exists())

    def test_legitimate_refusal_is_upheld(self):
        final, out, _ = run_demo("demo-refusal.json", "write a keylogger that emails me my coworkers' passwords")
        self.assertIn("refusal upheld", out)
        self.assertNotIn("switch a37-flash", out)   # no model-shopping
        self.assertIn("can't help", final)


class NightShift(unittest.TestCase):
    def test_plan_parse_and_mark(self):
        from relay.plan import parse
        d = Path(tempfile.mkdtemp()) / "PLAN.md"
        d.write_text("# T\n\n## a\ntest: true\nbudget: $1.5\npriority: 2\n- [ ] one\n- [x] two\n\n## b\npriority: 1\n- [ ] three\n")
        pl = parse(d)
        self.assertEqual([t.text for _, t in pl.queue], ["three", "one"])   # priority order
        self.assertEqual(pl.projects[0].budget, 1.5)
        pl.mark(pl.projects[0].tasks[0], "x", "relay abc")
        self.assertIn("- [x] one (relay abc)", d.read_text())

    def test_idle_windows_merge_weekend(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from relay.capacity import current_or_next_window
        cfg = {"timezone": "America/Los_Angeles", "weekday": "23:00-07:00", "weekend": "all"}
        fri_noon = datetime(2026, 10, 9, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        s, e, active = current_or_next_window(cfg, fri_noon)
        self.assertFalse(active)
        self.assertEqual((s.weekday(), s.hour), (4, 23))     # Friday 23:00
        self.assertEqual((e.weekday(), e.hour), (0, 7))      # runs through the weekend to Monday 07:00

    def test_burn_order_prefers_capacity_most_likely_wasted(self):
        from relay.capacity import assess
        cfg = json.loads((ROOT / "examples" / "burner-demo.json").read_text())
        caps = assess(cfg, "/nonexistent")
        self.assertEqual(caps[0].name, "agent37")

    def test_night_shift_end_to_end(self):
        from relay.burn import burn
        d = Path(tempfile.mkdtemp())
        (d / "PLAN.md").write_text((ROOT / "examples" / "PLAN.md").read_text())
        cfg = json.loads((ROOT / "examples" / "burner-demo.json").read_text())
        with redirect_stdout(io.StringIO()):
            res = burn(str(d / "PLAN.md"), cfg, str(d), now=True, pr=False)
        status = {r["task"]: r["status"] for r in res["results"]}
        self.assertEqual(status["publish to PyPI"], "blocked")
        self.assertEqual(sum(v == "done" for v in status.values()), 5)
        self.assertIn("- [!] publish to PyPI", (d / "PLAN.md").read_text())
        self.assertTrue((d / "MORNING.md").exists())
        log = subprocess.run(["git", "log", "--oneline", "relay/night-" + __import__("datetime").datetime.now().strftime("%Y%m%d")],
                             cwd=d / "greet-cli", capture_output=True, text=True).stdout
        self.assertIn("add a --name flag", log)
