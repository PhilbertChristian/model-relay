import type { BudgetSnapshot, BurnerConfig } from "./types.js";
import { clamp } from "./util.js";

const WEEK_MS = 7 * 24 * 60 * 60 * 1000;
const HOUR_MS = 60 * 60 * 1000;

/** Next local reset strictly after `now`. The instant of reset itself opens the following window. */
function nextReset(resetDay: number, resetHour: number, now: Date): Date {
  const day = ((resetDay % 7) + 7) % 7;
  for (let i = 0; i <= 7; i++) {
    const candidate = new Date(now.getFullYear(), now.getMonth(), now.getDate() + i, resetHour, 0, 0, 0);
    if (candidate.getDay() === day && candidate.getTime() > now.getTime()) return candidate;
  }
  return new Date(now.getFullYear(), now.getMonth(), now.getDate() + 7, resetHour, 0, 0, 0);
}

export function currentWindow(
  cfg: Pick<BurnerConfig, "resetDay" | "resetHour">,
  now?: Date,
): { windowStart: string; windowEnd: string } {
  const at = now ?? new Date();
  const end = nextReset(cfg.resetDay, cfg.resetHour, at);
  const start = new Date(end.getTime() - WEEK_MS);
  return { windowStart: start.toISOString(), windowEnd: end.toISOString() };
}

export function budgetSnapshot(args: {
  cfg: BurnerConfig;
  tokensUsed: number;
  burnRatePerHour: number;
  limited?: boolean;
  limitResetsAt?: string;
  now?: Date;
}): BudgetSnapshot {
  const { cfg, tokensUsed, burnRatePerHour } = args;
  const now = args.now ?? new Date();
  const limited = args.limited ?? false;
  const { windowStart, windowEnd } = currentWindow(cfg, now);

  const tokensTarget = cfg.weeklyTokenTarget;
  const rawPct = tokensTarget > 0 ? (tokensUsed / tokensTarget) * 100 : tokensUsed > 0 ? 100 : 0;
  const pctUsed = Math.min(100, rawPct);
  const rawHoursLeft = (new Date(windowEnd).getTime() - now.getTime()) / HOUR_MS;
  const hoursLeft = Math.max(0.25, rawHoursLeft);
  const remaining = Math.max(0, tokensTarget * (cfg.stopAtPct / 100) - tokensUsed);
  const neededRatePerHour = remaining / hoursLeft;

  // Pace the fleet toward stopAtPct just before reset. A lane usage limit stops
  // launches (concurrency 0): no retry loop and no account switching.
  let recommended = 0;
  const stopped = limited || pctUsed >= cfg.stopAtPct || hoursLeft <= 0 || rawHoursLeft <= 0;
  if (!stopped) {
    if (burnRatePerHour >= neededRatePerHour * 1.15) recommended = cfg.concurrency.min;
    else if (burnRatePerHour < neededRatePerHour * 0.5) recommended = cfg.concurrency.max;
    else recommended = (cfg.concurrency.min + cfg.concurrency.max) / 2;
    if (remaining > 0) recommended = Math.max(recommended, cfg.concurrency.min);
  }
  const recommendedConcurrency = clamp(recommended, 0, cfg.concurrency.max);

  const snap: BudgetSnapshot = {
    windowStart,
    windowEnd,
    tokensUsed,
    tokensTarget,
    pctUsed,
    hoursLeft,
    burnRatePerHour,
    neededRatePerHour,
    recommendedConcurrency,
    limited,
  };
  if (args.limitResetsAt !== undefined) snap.limitResetsAt = args.limitResetsAt;
  return snap;
}
