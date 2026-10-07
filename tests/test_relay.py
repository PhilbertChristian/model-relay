"""python3 -m unittest discover tests"""
import io
import json
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


def run_demo(name: str):
    cfg = json.loads((ROOT / "examples" / name).read_text())
    d = tempfile.mkdtemp()
    router = build_router(cfg)
    agent = Agent(router, Toolbox(d), Telemetry(d + "/.relay"), max_steps=cfg["max_steps"])
    with redirect_stdout(io.StringIO()) as out:
        final = agent.run("create hello_relay.py and run it")
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
