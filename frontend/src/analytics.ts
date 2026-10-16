import type { FlakyTest } from "./api/client";

const DAY_MS = 86_400_000;

/** "2026-10-14" -> its UTC midnight in ms. */
export function utcDay(day: string): number {
  return Date.parse(`${day}T00:00:00Z`);
}

/** The UTC day (YYYY-MM-DD) an instant falls on. */
export function dayOf(iso: string): string {
  return new Date(iso).toISOString().slice(0, 10);
}

/**
 * Position (0..1) of each UTC day from `firstDay` to `lastDay` inclusive. A
 * day without data still has its slot, so gaps stay visible as gaps.
 */
export function dayScale(firstDay: string, lastDay: string): (day: string) => number {
  const start = utcDay(firstDay);
  const span = utcDay(lastDay) - start;
  return (day) => (span > 0 ? (utcDay(day) - start) / span : 0.5);
}

/** Every Monday from `firstWeek` (a Monday) to the week containing `lastDay`. */
export function weeksBetween(firstWeek: string, lastDay: string): string[] {
  const weeks: string[] = [];
  const end = utcDay(lastDay);
  for (let t = utcDay(firstWeek); t <= end; t += 7 * DAY_MS) {
    weeks.push(new Date(t).toISOString().slice(0, 10));
  }
  return weeks;
}

/** Position of the i-th of n evenly spaced items (commits, weeks). */
export function ordinalX(i: number, n: number): number {
  return n <= 1 ? 0.5 : i / (n - 1);
}

/** Rows grouped by profile, profiles in name order, rows in their given order. */
export function byProfile<T extends { profile: string }>(rows: T[]): [string, T[]][] {
  const groups = new Map<string, T[]>();
  for (const r of rows) groups.set(r.profile, [...(groups.get(r.profile) ?? []), r]);
  return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b));
}

/**
 * Round tick values covering [lo, hi]: steps of 1, 2 or 5 x 10^k. The
 * domain is the outer ticks, so every point is inside the plot.
 */
export function niceTicks(lo: number, hi: number, count = 4): number[] {
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return [];
  if (hi < lo) [lo, hi] = [hi, lo];
  if (hi === lo) {
    const pad = Math.abs(hi) * 0.01 || 1;
    lo -= pad;
    hi += pad;
  }
  const raw = (hi - lo) / Math.max(1, count);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw)!;
  const ticks: number[] = [];
  for (let t = Math.floor(lo / step) * step; t <= hi + step * 1e-9; t += step) ticks.push(t);
  if (ticks[ticks.length - 1]! < hi) ticks.push(ticks[ticks.length - 1]! + step);
  // Strip float noise (0.30000000000000004).
  return ticks.map((t) => Number(t.toPrecision(12)));
}

/** Why a test is listed, in words. */
export function flakyReasons(test: FlakyTest, recentBuilds: number): string[] {
  const reasons: string[] = [];
  const n = test.same_commit_shas.length;
  if (n > 0) reasons.push(`passed and failed on ${n === 1 ? "the same commit" : `${n} commits`}`);
  if (test.flips != null) reasons.push(`flipped ${test.flips}× in the last ${recentBuilds} builds`);
  return reasons;
}

/** "test_failure" -> "test failure"; null -> "unclassified". */
export function failureClassLabel(failureClass: string | null): string {
  return failureClass ? failureClass.replace(/_/g, " ") : "unclassified";
}
