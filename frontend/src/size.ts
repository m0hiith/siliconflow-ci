import type { BuildSize } from "./api/client";

export const SIZE_TOTALS = [
  ["text", "text_bytes"],
  ["data", "data_bytes"],
  ["bss", "bss_bytes"],
] as const;

export interface SizeRow {
  name: string;
  bytes: number | null;
  base: number | null;
  /** bytes - base; null unless both are known. */
  delta: number | null;
  /** delta as a percentage of base; null when base is unknown or zero. */
  percent: number | null;
}

function row(name: string, bytes: number | null, base: number | null): SizeRow {
  const delta = bytes != null && base != null ? bytes - base : null;
  const percent = delta != null && base ? (100 * delta) / base : null;
  return { name, bytes, base, delta, percent };
}

/** text, data and bss of a build next to its base build's. */
export function totalRows(size: BuildSize, base: BuildSize | null): SizeRow[] {
  return SIZE_TOTALS.map(([name, key]) => row(name, size[key], base ? base[key] : null));
}

/** Sections of both builds, in the build's order, then sections only the base had. */
export function sectionRows(size: BuildSize, base: BuildSize | null): SizeRow[] {
  const baseBy = new Map((base?.sections ?? []).map((s) => [s.name, s.size]));
  const rows = size.sections.map((s) => row(s.name, s.size, base ? (baseBy.get(s.name) ?? 0) : null));
  const seen = new Set(size.sections.map((s) => s.name));
  for (const s of base?.sections ?? []) if (!seen.has(s.name)) rows.push(row(s.name, 0, s.size));
  return rows;
}

/** "+1,024", "-12", "0"; empty when unknown. */
export function formatDelta(delta: number | null): string {
  if (delta == null) return "";
  return `${delta > 0 ? "+" : ""}${delta.toLocaleString("en-US")}`;
}

export function formatPercent(percent: number | null): string {
  if (percent == null) return "";
  return `${percent > 0 ? "+" : ""}${percent.toFixed(2)}%`;
}
