import type { BuildStatus, StageStatus } from "./api/client";

export type Tone = "passed" | "failed" | "running" | "neutral";

/** Every status, in lifecycle order. `satisfies` breaks the build if the API adds one. */
export const BUILD_STATUSES = [
  "queued",
  "running",
  "passed",
  "failed",
  "cancelled",
  "timed_out",
  "errored",
] as const satisfies readonly BuildStatus[];

type Missing = Exclude<BuildStatus, (typeof BUILD_STATUSES)[number]>;
const _exhaustive: [Missing] extends [never] ? true : Missing = true;
void _exhaustive;

const TONES: Record<BuildStatus, Tone> = {
  queued: "running",
  running: "running",
  passed: "passed",
  failed: "failed",
  timed_out: "failed",
  // errored = CI infrastructure failed; no verdict on the firmware.
  errored: "neutral",
  cancelled: "neutral",
};

export function statusTone(status: BuildStatus): Tone {
  return TONES[status];
}

export function isTerminal(status: BuildStatus): boolean {
  return status !== "queued" && status !== "running";
}

export function isBuildStatus(value: string): value is BuildStatus {
  return (BUILD_STATUSES as readonly string[]).includes(value);
}

const STAGE_TONES: Record<StageStatus, Tone | "skipped"> = {
  passed: "passed",
  failed: "failed",
  running: "running",
  pending: "neutral",
  cancelled: "neutral",
  skipped: "skipped",
};

export function stageTone(status: StageStatus): Tone | "skipped" {
  return STAGE_TONES[status];
}
