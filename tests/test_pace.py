"""python3 -m unittest tests.test_pace -v"""
from __future__ import annotations

import math
import unittest
from unittest import mock

from relay.contracts import PacePlan, Usage, to_dict
from relay.lanes.base import Lane
from relay.pace import DEFAULT_RATE, adjust, plan_pace, report, schedule_note
from relay.usage import weekly_usage

NOW = 1_700_000_000.0      # injected clock, years before the wall clock: Usage.hours_left must never be used
H = 3600.0


def sub(name: str, used: float, limit: float | None, hours: float, unit: str = "credits") -> Usage:
    return Usage(name, "mock", unit, used, limit, NOW + hours * H)


def lane(name: str, subscription: str | None = None, **kw) -> dict:
    return {"kind": "mock", "name": name, "subscription": subscription or name, **kw}


def by_lane(plans) -> dict:
    return {p.lane: p for p in plans}


class PlanPace(unittest.TestCase):
    def test_rate_and_agents_land_on_target(self):
        u = sub("claude-max", 248e6, 400e6, 61, "tokens")
        [p] = plan_pace([u], [lane("claude", "claude-max", max_agents=8, agent_rate=400_000)], now=NOW)
        self.assertIsInstance(p, PacePlan)
        self.assertAlmostEqual(p.target_rate, (0.97 * 400e6 - 248e6) / 61, places=3)   # 2.3M tokens/h
        self.assertEqual((p.agents, p.remaining, p.hours_left, p.unit), (6, 152e6, 61.0, "tokens"))
        self.assertEqual(schedule_note([p]), "claude-max: 38% left, 61h to reset → 6 agents")

    def test_default_rates_per_unit(self):
        plans = by_lane(plan_pace([sub("api", 0, 100, 10, "usd"), sub("tok", 0, 250e6, 10, "tokens")],
                                  [lane("api", max_agents=40), lane("tok", max_agents=40)], 40, 1.0, now=NOW))
        self.assertEqual(plans["api"].agents, math.ceil(10 / DEFAULT_RATE["usd"]))
        self.assertEqual(plans["tok"].agents, math.ceil(25e6 / DEFAULT_RATE["tokens"]))

    def test_lane_max_caps_agents_not_the_rate(self):
        [p] = plan_pace([sub("claude-max", 248e6, 400e6, 61, "tokens")],
                        [lane("claude", "claude-max", max_agents=4, agent_rate=400_000)], now=NOW)
        self.assertEqual(p.agents, 4)
        self.assertAlmostEqual(p.target_rate, (0.97 * 400e6 - 248e6) / 61, places=3)

    def test_global_cap_is_split_in_proportion_to_need(self):
        two = plan_pace([sub("a", 0, 800, 10), sub("b", 0, 400, 10)],
                        [lane("a", max_agents=20, agent_rate=10), lane("b", max_agents=20, agent_rate=10)], 6, 1.0, NOW)
        self.assertEqual([p.agents for p in two], [4, 2])                 # want 8 and 4
        three = [sub("a", 0, 3000, 10), sub("b", 0, 100, 10), sub("c", 0, 100, 10)]
        lanes = [lane(x, max_agents=40, agent_rate=10) for x in "abc"]
        self.assertEqual([p.agents for p in plan_pace(three, lanes, 4, 1.0, NOW)], [2, 1, 1])   # want 30, 1, 1
        self.assertEqual([p.agents for p in plan_pace(three, lanes, 0, 1.0, NOW)], [0, 0, 0])
        self.assertEqual([p.agents for p in plan_pace(three, lanes, 40, 1.0, NOW)], [30, 1, 1])  # under the cap

    def test_idle_lanes_get_zero_agents(self):
        us = [sub("full", 100, 100, 10), sub("near", 98, 100, 10), sub("gone", 10, 100, -1), sub("lim", 10, 100, 10),
              sub("nobudget", 0, 0, 10, "usd")]
        ls = [lane("full"), lane("near"), lane("gone"), lane("lim", limited_until=NOW + H), lane("nobudget")]
        plans = by_lane(plan_pace(us, ls, now=NOW))
        for name, note in (("full", "exhausted"), ("near", "at target"), ("gone", "reset passed"),
                           ("nobudget", "no budget")):
            self.assertEqual((plans[name].agents, plans[name].target_rate, plans[name].note), (0, 0.0, note), name)
        self.assertEqual(plans["gone"].hours_left, 0.0)
        self.assertEqual(plans["lim"].agents, 0)
        self.assertTrue(plans["lim"].note.startswith("limited until"))
        [p] = plan_pace([sub("lim", 10, 100, 10)], [lane("lim", limited_until=NOW - 1)], now=NOW)
        self.assertGreater(p.agents, 0)                                    # an expired limit no longer blocks
        [p] = plan_pace([sub("lim", 10, 100, 10)], [lane("lim", limited_until=float("inf"))], now=NOW)
        self.assertEqual((p.agents, p.note), (0, "limited until further notice"))

    def test_only_enabled_lanes_with_a_known_subscription(self):
        plans = plan_pace([sub("claude-max", 0, 100, 10)],
                          [lane("claude", "claude-max"), lane("orca"), lane("off", "claude-max", enabled=False)], now=NOW)
        self.assertEqual([p.lane for p in plans], ["claude"])

    def test_no_limit_means_the_lane_max(self):
        [p] = plan_pace([sub("x", 5, None, 10, "tokens")], [lane("x", max_agents=3)], now=NOW)
        self.assertEqual((p.agents, p.target_rate, p.note), (3, 0.0, "no limit set"))
        self.assertEqual(schedule_note([p]), "x: no limit, 10h to reset → 3 agents")

    def test_lanes_sharing_a_subscription_share_its_rate(self):
        u = [sub("shared", 0, 500, 10)]                                    # 50 units/h to burn
        a, b = (lane("a", "shared", max_agents=3, agent_rate=10), lane("b", "shared", max_agents=5, agent_rate=10))
        plans = by_lane(plan_pace(u, [a, b], target_pct=1.0, now=NOW))
        self.assertEqual((plans["a"].agents, plans["a"].target_rate, plans["b"].agents, plans["b"].target_rate),
                         (3, 30.0, 2, 20.0))
        b["max_agents"] = 1                                                # lanes can't carry it all: the last one
        plans = by_lane(plan_pace(u, [a, b], target_pct=1.0, now=NOW))    # keeps the rest of the rate
        self.assertEqual((plans["a"].agents, plans["b"].agents, plans["a"].target_rate + plans["b"].target_rate),
                         (3, 1, 50.0))
        self.assertEqual(schedule_note(plans.values()), "shared: 100% left, 10h to reset → 4 agents")

    def test_accepts_lane_objects(self):
        ln = Lane("claude", {"subscription": "claude-max", "max_agents": 3, "agent_rate": 1})
        u = [sub("claude-max", 0, 1000, 10)]
        self.assertEqual(plan_pace(u, [ln], now=NOW)[0].agents, 3)
        ln.limited_until = NOW + H
        self.assertEqual(plan_pace(u, [ln], now=NOW)[0].agents, 0)

    def test_plan_dict_carries_the_pace_event_fields(self):
        [p] = plan_pace([sub("claude-max", 62, 100, 61)], [lane("claude", "claude-max")], now=NOW)
        d = to_dict(p)
        self.assertLessEqual({"lane", "subscription", "unit", "used", "limit", "pct", "remaining", "agents",
                              "target_rate", "hours_left"}, set(d))
        self.assertEqual((d["pct"], d["used"], d["limit"], d["remaining"]), (0.62, 62, 100, 38))

    def test_demo_config_end_to_end(self):
        cfg = {"subscriptions": [
            {"name": "claude-max", "kind": "demo", "lane": "claude", "used": 248e6, "limit": 400e6, "unit": "tokens",
             "resets_in_hours": 61},
            {"name": "codex", "kind": "demo", "lane": "codex", "used": 30e6, "limit": 100e6, "unit": "tokens",
             "resets_in_hours": 61},
            {"name": "agent37", "kind": "demo", "lane": "agent37", "used": 4.2, "limit": 20, "unit": "usd",
             "resets_in_hours": 400}],
            "lanes": [{"kind": "mock", "as": "claude", "name": "claude", "subscription": "claude-max", "max_agents": 8,
                       "agent_rate": 400_000},
                      {"kind": "mock", "as": "codex", "name": "codex", "subscription": "codex", "max_agents": 3},
                      {"kind": "mock", "as": "agent37", "name": "agent37", "subscription": "agent37", "max_agents": 2},
                      {"kind": "mock", "as": "orca", "name": "orca", "max_agents": 4}]}
        plans = plan_pace(weekly_usage(cfg, now=NOW), cfg["lanes"], max_agents=6, now=NOW)
        self.assertEqual([(p.lane, p.agents) for p in plans], [("claude", 4), ("codex", 1), ("agent37", 1)])  # want 6, 1, 1


class Adjust(unittest.TestCase):
    def test_holds_inside_the_band(self):
        self.assertEqual((adjust(5, 100, 108), adjust(5, 100, 92)), (5, 5))

    def test_moves_at_most_two_per_call(self):
        self.assertEqual((adjust(2, 10, 100), adjust(10, 100, 10)), (4, 8))

    def test_steps_half_the_gap_and_never_past_it(self):
        self.assertEqual(adjust(4, 4, 5), 5)          # ideal 5: gap 1 -> one step, lands on it
        self.assertEqual(adjust(4, 4, 4.6), 4)        # ideal 4.6: outside the band, but a step would overshoot
        self.assertEqual(adjust(4, 4, 7), 6)          # ideal 7: half of 3, rounded

    def test_bounds(self):
        self.assertEqual(adjust(11, 10, 100), 12)
        self.assertEqual(adjust(2, 100, 10), 1)
        self.assertEqual(adjust(5, 10, 100, lo=1, hi=0), 0)   # hi wins: a lane that may run nothing runs nothing
        self.assertEqual(adjust(15, 100, 100), 12)

    def test_no_rate_yet_holds_or_starts_one(self):
        self.assertEqual((adjust(4, 0, 100), adjust(0, 0, 100, lo=0), adjust(0, 50, 100, lo=0)), (4, 1, 1))

    def test_nothing_left_steps_down(self):
        self.assertEqual((adjust(5, 100, 0), adjust(1, 100, 0, lo=0), adjust(1, 100, 0)), (3, 0, 1))

    def test_extreme_rates_never_raise(self):
        self.assertEqual((adjust(4, 1e-320, 100), adjust(4, float("inf"), 100), adjust(4, float("nan"), 100)), (6, 2, 4))

    def test_settles_without_oscillating(self):
        for per_agent in (0.5, 1.0, 3.0):
            for target in (0.7, 2.0, 3.3, 5.5, 7.3, 11.0, 40.0):
                for start in range(13):
                    seq = [start]
                    for _ in range(30):
                        seq.append(adjust(seq[-1], seq[-1] * per_agent, target, lo=0, hi=12))
                    steps = [b - a for a, b in zip(seq, seq[1:]) if b != a]
                    self.assertTrue(all(abs(s) <= 2 for s in steps), seq)
                    self.assertTrue(all(s > 0 for s in steps) or all(s < 0 for s in steps), seq)   # one direction
                    n = seq[-1]
                    self.assertEqual(adjust(n, n * per_agent, target, lo=0, hi=12), n, seq)          # settled
                    ideal = target / per_agent
                    self.assertTrue(n == 12 or abs(n - ideal) < 1 or abs(ideal / n - 1) <= 0.1, (seq, ideal))


class Notes(unittest.TestCase):
    def plans(self):
        return plan_pace([sub("claude-max", 62, 100, 61), sub("codex", 8, 100, 61)],
                         [lane("claude", "claude-max", max_agents=8, agent_rate=0.1),
                          lane("codex", "codex", limited_until=NOW + H)], now=NOW)

    def test_schedule_note(self):
        self.assertTrue(schedule_note(self.plans()).startswith(
            "claude-max: 38% left, 61h to reset → 6 agents · codex: 92% left, 61h to reset → 0 agents (limited until "))
        self.assertEqual(schedule_note([PacePlan("relay", "openai", "usd", 12.5, 10, 1.0, 1)]),
                         "openai: $12.50 left, 10h to reset → 1 agent")   # a plain PacePlan: units, not %
        self.assertEqual(schedule_note([]), "no lane burns a known subscription")

    def test_report(self):
        with mock.patch("relay.ui._COLOR", False):
            lines = report(self.plans()).splitlines()
        self.assertIn("agents", lines[0])
        self.assertIn("●" * 6, lines[1])
        self.assertIn("38%", lines[1])
        self.assertIn("limited until", lines[2])
        self.assertEqual(lines[-1].split(), ["total", "6"])
        with mock.patch("relay.ui._COLOR", True):
            self.assertIn("\033[", report(self.plans()))
        self.assertEqual(report([]), "no lane burns a known subscription")


if __name__ == "__main__":
    unittest.main()
