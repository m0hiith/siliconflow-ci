import type { BuildStatus } from "./api/client";
import { BUILD_STATUSES, isBuildStatus } from "./status";

/** Builds page filters. They live in the URL query string, nowhere else. */
export interface BuildFilters {
  project?: string;
  status: BuildStatus[];
  branch?: string;
  profile?: string;
}

const TEXT_KEYS = ["project", "branch", "profile"] as const;

export function parseFilters(params: URLSearchParams): BuildFilters {
  const filters: BuildFilters = { status: [] };
  for (const key of TEXT_KEYS) {
    const value = params.get(key)?.trim();
    if (value) filters[key] = value;
  }
  // Unknown statuses are dropped rather than sent to the API to be rejected.
  const wanted = new Set(params.getAll("status").filter(isBuildStatus));
  filters.status = BUILD_STATUSES.filter((s) => wanted.has(s));
  return filters;
}

export function toSearchParams(filters: BuildFilters): URLSearchParams {
  const params = new URLSearchParams();
  for (const key of TEXT_KEYS) {
    const value = filters[key]?.trim();
    if (value) params.set(key, value);
  }
  for (const s of filters.status) params.append("status", s);
  return params;
}

export function hasFilters(filters: BuildFilters): boolean {
  return toSearchParams(filters).size > 0;
}
