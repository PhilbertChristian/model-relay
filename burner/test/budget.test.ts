import { describe, expect, it } from "vitest";
import { budgetSnapshot, currentWindow } from "../src/budget.js";
import type { BurnerConfig } from "../src/types.js";

const WEEK_MS = 7 * 24 * 60 * 60 * 1000;
const HOUR_MS = 60 * 60 * 1000;

/** Wednesday 2026-10-07 15:00:00 local. Assertions use epoch deltas, not a zone name. */
const NOW = new Date(2026, 9, 7, 15, 0, 0, 0);

function makeCfg(over: Partial<BurnerConfig> = {}): BurnerConfig {
  return {
    roots: [],
    include: [],
    exclude: [],
    maxDepth: 1,
    concurrency: { min: 1, max: 8 },
    weeklyTokenTarget: 1_000_000,
    resetDay: 0,
    resetHour: 23,
    stopAtPct: 99,
    runTimeoutMin: 25,
    lanes: { claude: true, agent37: false, orca: false, mock: true },
    claude: {
      bin: "claude",
      maxTurns: 1,
      permissionMode: "plan",
      allowedTools: [],
      disallowedTools: [],
      extraArgs: [],
    },
    agent37: { baseUrl: "http://127.0.0.1", template: "t", maxInstances: 1 },
    monid: { baseUrl: "http://127.0.0.1", enabled: false },
    orca: { enabled: false },
    dataDir: "/tmp/burner-budget-test",
    port: 0,
    demo: true,
    ...over,
  };
}

describe("currentWindow", () => {
  it("freezes Wednesday 2026-10-07 15:00 local", () => {
    expect(NOW.getFullYear()).toBe(2026);
    expect(NOW.getMonth()).toBe(9);
    expect(NOW.getDate()).toBe(7);
    expect(NOW.getHours()).toBe(15);
    expect(NOW.getMinutes()).toBe(0);
    expect(NOW.getDay()).toBe(3);
  });

  it("ends at the next local reset and starts exactly 7 days earlier", () => {
    const expectedEnd = new Date(2026, 9, 11, 23, 0, 0, 0);
    const win = currentWindow({ resetDay: expectedEnd.getDay(), resetHour: expectedEnd.getHours() }, NOW);
    const end = new Date(win.windowEnd).getTime();
    const start = new Date(win.windowStart).getTime();
    expect(end - NOW.getTime()).toBe(expectedEnd.getTime() - NOW.getTime());
    expect(end).toBe(expectedEnd.getTime());
    expect(start).toBe(end - WEEK_MS);
    expect(win.windowEnd).toBe(expectedEnd.toISOString());
    expect(win.windowStart).toBe(new Date(end - WEEK_MS).toISOString());
  });

  it("uses today's reset when that hour is still ahead", () => {
    const expectedEnd = new Date(2026, 9, 7, 18, 0, 0, 0);
    const win = currentWindow({ resetDay: expectedEnd.getDay(), resetHour: 18 }, NOW);
    expect(new Date(win.windowEnd).getTime()).toBe(expectedEnd.getTime());
    expect(new Date(win.windowStart).getTime()).toBe(expectedEnd.getTime() - WEEK_MS);
  });

  it("rolls to next week when today's reset hour has passed", () => {
    const expectedEnd = new Date(2026, 9, 14, 10, 0, 0, 0);
    const win = currentWindow({ resetDay: NOW.getDay(), resetHour: 10 }, NOW);
    expect(new Date(win.windowEnd).getTime() - NOW.getTime()).toBe(expectedEnd.getTime() - NOW.getTime());
    expect(new Date(win.windowStart).getTime()).toBe(new Date(win.windowEnd).getTime() - WEEK_MS);
  });

  it("treats the exact reset instant as the start of the next window", () => {
    const now = new Date(2026, 9, 7, 15, 0, 0, 0);
    const expectedEnd = new Date(2026, 9, 14, 15, 0, 0, 0);
    const win = currentWindow({ resetDay: now.getDay(), resetHour: 15 }, now);
    expect(new Date(win.windowEnd).getTime()).toBe(expectedEnd.getTime());
  });
});

describe("budgetSnapshot", () => {
  const cfg = makeCfg();

  function neededFor(tokensUsed: number, now = NOW): number {
    const end = new Date(currentWindow(cfg, now).windowEnd).getTime();
    const hoursLeft = Math.max(0.25, (end - now.getTime()) / HOUR_MS);
    const remaining = Math.max(0, cfg.weeklyTokenTarget * (cfg.stopAtPct / 100) - tokensUsed);
    return remaining / hoursLeft;
  }

  it("under pace recommends max concurrency", () => {
    const tokensUsed = 1_000;
    const needed = neededFor(tokensUsed);
    const burnRatePerHour = 0;
    const snap = budgetSnapshot({ cfg, tokensUsed, burnRatePerHour, now: NOW });
    expect(burnRatePerHour).toBeLessThan(needed * 0.5);
    expect(snap.tokensTarget).toBe(cfg.weeklyTokenTarget);
    expect(snap.tokensUsed).toBe(tokensUsed);
    expect(snap.burnRatePerHour).toBe(burnRatePerHour);
    expect(snap.pctUsed).toBe((tokensUsed / cfg.weeklyTokenTarget) * 100);
    expect(snap.hoursLeft).toBeGreaterThan(0.25);
    expect(snap.neededRatePerHour).toBeCloseTo(needed);
    expect(snap.limited).toBe(false);
    expect(snap.recommendedConcurrency).toBe(cfg.concurrency.max);
    expect(snap.windowEnd).toBe(currentWindow(cfg, NOW).windowEnd);
    expect(new Date(snap.windowStart).getTime()).toBe(new Date(snap.windowEnd).getTime() - WEEK_MS);
  });

  it("over pace recommends min concurrency", () => {
    const tokensUsed = 100_000;
    const needed = neededFor(tokensUsed);
    const burnRatePerHour = needed * 1.15;
    const snap = budgetSnapshot({ cfg, tokensUsed, burnRatePerHour, now: NOW });
    expect(snap.pctUsed).toBeLessThan(cfg.stopAtPct);
    expect(snap.neededRatePerHour).toBeCloseTo(needed);
    expect(snap.recommendedConcurrency).toBe(cfg.concurrency.min);
  });

  it("holds the midpoint when pace is between the bands", () => {
    const tokensUsed = 100_000;
    const needed = neededFor(tokensUsed);
    const burnRatePerHour = needed * 0.8;
    const snap = budgetSnapshot({ cfg, tokensUsed, burnRatePerHour, now: NOW });
    expect(burnRatePerHour).toBeGreaterThanOrEqual(needed * 0.5);
    expect(burnRatePerHour).toBeLessThan(needed * 1.15);
    expect(snap.recommendedConcurrency).toBe((cfg.concurrency.min + cfg.concurrency.max) / 2);
  });

  it("limited lane forces concurrency 0 and keeps the reset hint", () => {
    const limitResetsAt = new Date(NOW.getTime() + 3 * HOUR_MS).toISOString();
    const snap = budgetSnapshot({
      cfg,
      tokensUsed: 0,
      burnRatePerHour: 0,
      limited: true,
      limitResetsAt,
      now: NOW,
    });
    expect(snap.limited).toBe(true);
    expect(snap.limitResetsAt).toBe(limitResetsAt);
    expect(snap.recommendedConcurrency).toBe(0);
    expect(snap.pctUsed).toBe(0);
    expect(snap.neededRatePerHour).toBeGreaterThan(0);
  });

  it("at stopAtPct recommends 0", () => {
    const tokensUsed = cfg.weeklyTokenTarget * (cfg.stopAtPct / 100);
    const snap = budgetSnapshot({ cfg, tokensUsed, burnRatePerHour: 0, now: NOW });
    expect(snap.pctUsed).toBe(cfg.stopAtPct);
    expect(snap.neededRatePerHour).toBe(0);
    expect(snap.recommendedConcurrency).toBe(0);
  });

  it("caps pctUsed at 100 past the target", () => {
    const snap = budgetSnapshot({ cfg, tokensUsed: cfg.weeklyTokenTarget * 3, burnRatePerHour: 1, now: NOW });
    expect(snap.pctUsed).toBe(100);
    expect(snap.recommendedConcurrency).toBe(0);
  });

  it("floors hoursLeft at 0.25", () => {
    const end = new Date(2026, 9, 7, 15, 0, 0, 0);
    const now = new Date(end.getTime() - 1000);
    const near = makeCfg({ resetDay: end.getDay(), resetHour: end.getHours() });
    const snap = budgetSnapshot({ cfg: near, tokensUsed: 0, burnRatePerHour: 0, now });
    expect(new Date(snap.windowEnd).getTime() - now.getTime()).toBe(1000);
    expect(snap.hoursLeft).toBe(0.25);
    expect(snap.recommendedConcurrency).toBe(near.concurrency.max);
  });
});
