import type { Build } from "./api/client";

export function shortSha(sha: string | null | undefined): string {
  return sha ? sha.slice(0, 10) : "";
}

/** 0:42, 3:07, 1:02:05 */
export function formatDuration(totalSeconds: number): string {
  const s = Math.max(0, Math.round(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, "0");
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}

/** Seconds from start to finish, or to `now` while running; null if never started. */
export function buildSeconds(build: Build, now: number): number | null {
  if (!build.started_at) return null;
  const end = build.finished_at ? Date.parse(build.finished_at) : now;
  return (end - Date.parse(build.started_at)) / 1000;
}

export function formatPercent(ratio: number): string {
  return `${(ratio * 100).toFixed(1)}%`;
}

/** 2026-10-14 09:00:12Z */
export function formatUtc(iso: string): string {
  return new Date(iso).toISOString().replace("T", " ").replace(/\.\d+Z$/, "Z");
}

export function formatAgo(iso: string, now: number): string {
  const s = Math.round((now - Date.parse(iso)) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  const units = ["KiB", "MiB", "GiB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
}

/** "gcc (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0" -> "13.3.0"; the version a tool prints last. */
export function toolVersion(raw: string | null | undefined): string | null {
  if (!raw) return null;
  const firstLine = raw.split("\n")[0] ?? "";
  const versions = firstLine.match(/\d+\.\d+(?:\.\d+)?(?:[-+.\w]*)?/g);
  return versions ? versions[versions.length - 1]! : firstLine.trim() || null;
}

export type VersionChip = { name: string; version: string; raw: string };

/** Reported versions worth a chip: those with a value, in report order. */
export function versionChips(versions: Record<string, string | null> | null | undefined): VersionChip[] {
  return Object.entries(versions ?? {}).flatMap(([name, raw]) => {
    const version = toolVersion(raw);
    return raw && version ? [{ name, version, raw }] : [];
  });
}

// eslint-disable-next-line no-control-regex
const ANSI = /\x1b\[[0-9;?]*[ -/]*[@-~]/g;

export function stripAnsi(line: string): string {
  return line.replace(ANSI, "");
}

export function firstLine(text: string | null | undefined): string {
  return text?.split("\n")[0] ?? "";
}
