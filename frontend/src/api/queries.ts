import { keepPreviousData, useInfiniteQuery, useQuery } from "@tanstack/react-query";
import type { BuildFilters } from "../filters";
import { isTerminal } from "../status";
import { api, type BuildDetail, unwrap } from "./client";

export const POLL_MS = 3000;
const PAGE_SIZE = 50;

export function useMetricsSummary() {
  return useQuery({
    queryKey: ["metrics", "summary"],
    queryFn: ({ signal }) => unwrap(api.GET("/api/v1/metrics/summary", { signal })),
    // Numbers only move while something is queued or running.
    refetchInterval: (query) => ((query.state.data?.active_builds ?? 0) > 0 ? POLL_MS : false),
  });
}

export function useProjects() {
  return useQuery({
    queryKey: ["projects"],
    queryFn: ({ signal }) => unwrap(api.GET("/api/v1/projects", { signal })),
    staleTime: 60_000,
  });
}

// --------------------------------------------------------------- analytics
// Computed from stored builds on request; a minute's staleness is fine.
const ANALYTICS_STALE_MS = 60_000;

export interface AnalyticsScope {
  project?: string;
  /** Days back, today included; the weekly pass rate covers the weeks they touch. */
  days: number;
}

export function useDurations({ project, days }: AnalyticsScope) {
  return useQuery({
    queryKey: ["analytics", "durations", project, days],
    queryFn: ({ signal }) =>
      unwrap(api.GET("/api/v1/analytics/durations", { signal, params: { query: { project_id: project, days } } })),
    staleTime: ANALYTICS_STALE_MS,
  });
}

export function usePassRate({ project, days }: AnalyticsScope) {
  const weeks = Math.ceil(days / 7);
  return useQuery({
    queryKey: ["analytics", "pass-rate", project, weeks],
    queryFn: ({ signal }) =>
      unwrap(api.GET("/api/v1/analytics/pass-rate", { signal, params: { query: { project_id: project, weeks } } })),
    staleTime: ANALYTICS_STALE_MS,
  });
}

export function useSizeTrends({ project }: Pick<AnalyticsScope, "project">) {
  return useQuery({
    queryKey: ["analytics", "sizes", project],
    queryFn: ({ signal }) =>
      unwrap(api.GET("/api/v1/analytics/sizes", { signal, params: { query: { project_id: project } } })),
    staleTime: ANALYTICS_STALE_MS,
  });
}

export function useFlakyTests({ project, days }: AnalyticsScope) {
  return useQuery({
    queryKey: ["analytics", "flaky-tests", project, days],
    queryFn: ({ signal }) =>
      unwrap(api.GET("/api/v1/analytics/flaky-tests", { signal, params: { query: { project_id: project, days } } })),
    staleTime: ANALYTICS_STALE_MS,
  });
}

export function useBuilds(filters: BuildFilters) {
  return useInfiniteQuery({
    queryKey: ["builds", filters],
    queryFn: ({ signal, pageParam }) =>
      unwrap(
        api.GET("/api/v1/builds", {
          signal,
          params: {
            query: {
              project_id: filters.project,
              status: filters.status.length ? filters.status : undefined,
              branch: filters.branch,
              profile: filters.profile,
              limit: PAGE_SIZE,
              cursor: pageParam,
            },
          },
        }),
      ),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    placeholderData: keepPreviousData,
    refetchInterval: (query) => {
      const pages = query.state.data?.pages ?? [];
      return pages.some((p) => p.items.some((b) => !isTerminal(b.status))) ? POLL_MS : false;
    },
  });
}

// ------------------------------------------------------------ build details

/** Lines per log request; the virtualized viewer fetches only visible chunks. */
export const LOG_CHUNK = 1000;

/** Poll while the build runs, and while its logs and artifacts are still being ingested. */
function detailPollMs(build: BuildDetail | undefined): number | false {
  if (!build) return false;
  return !isTerminal(build.status) || build.artifacts_state === "pending" ? POLL_MS : false;
}

export function useBuild(buildId: string) {
  return useQuery({
    queryKey: ["build", buildId],
    queryFn: ({ signal }) =>
      unwrap(api.GET("/api/v1/builds/{build_id}", { signal, params: { path: { build_id: buildId } } })),
    refetchInterval: (query) => detailPollMs(query.state.data),
  });
}

/** Per-build resources refresh with the build itself (same polling rule). */
function useBuildResource<T>(build: BuildDetail | undefined, name: string, fetch: (signal: AbortSignal) => Promise<T>, enabled = true) {
  return useQuery({
    queryKey: ["build", build?.id, name, build?.status, build?.artifacts_state],
    queryFn: ({ signal }) => fetch(signal),
    enabled: !!build && enabled,
    placeholderData: keepPreviousData,
    refetchInterval: detailPollMs(build),
  });
}

export function useStages(build: BuildDetail | undefined) {
  return useBuildResource(build, "stages", (signal) =>
    unwrap(api.GET("/api/v1/builds/{build_id}/stages", { signal, params: { path: { build_id: build!.id } } })),
  );
}

export function useTests(build: BuildDetail | undefined) {
  return useBuildResource(build, "tests", (signal) =>
    unwrap(api.GET("/api/v1/builds/{build_id}/tests", { signal, params: { path: { build_id: build!.id } } })),
  );
}

export function useArtifacts(build: BuildDetail | undefined) {
  return useBuildResource(build, "artifacts", (signal) =>
    unwrap(api.GET("/api/v1/builds/{build_id}/artifacts", { signal, params: { path: { build_id: build!.id } } })),
  );
}

export function useGates(build: BuildDetail | undefined) {
  return useBuildResource(build, "gates", (signal) =>
    unwrap(api.GET("/api/v1/builds/{build_id}/gates", { signal, params: { path: { build_id: build!.id } } })),
  );
}

export function useBuildSize(build: BuildDetail | undefined) {
  return useBuildResource(build, "size", (signal) =>
    unwrap(api.GET("/api/v1/builds/{build_id}/size", { signal, params: { path: { build_id: build!.id } } })),
  );
}

export function usePreviousPassing(build: BuildDetail | undefined, enabled: boolean) {
  return useBuildResource(
    build,
    "previous-passing",
    (signal) =>
      unwrap(
        api.GET("/api/v1/builds/{build_id}/previous-passing", {
          signal,
          params: { path: { build_id: build!.id } },
        }),
      ),
    enabled,
  );
}

export function logChunkQuery(buildId: string, stage: string, chunk: number) {
  return {
    queryKey: ["log", buildId, stage, chunk],
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      unwrap(
        api.GET("/api/v1/builds/{build_id}/logs/{stage}", {
          signal,
          params: {
            path: { build_id: buildId, stage },
            query: { offset: chunk * LOG_CHUNK, limit: LOG_CHUNK },
          },
        }),
      ),
    // A stored log never changes.
    staleTime: Infinity,
  };
}

/** The first chunk of a stage's log; it also carries the whole log's index. */
export function useLogHead(buildId: string, stage: string | undefined) {
  return useQuery({ ...logChunkQuery(buildId, stage ?? "", 0), enabled: !!stage });
}

export function useLogSearch(buildId: string, stage: string, q: string) {
  return useQuery({
    queryKey: ["log-search", buildId, stage, q],
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/api/v1/builds/{build_id}/logs/{stage}/search", {
          signal,
          params: { path: { build_id: buildId, stage }, query: { q, limit: 5000 } },
        }),
      ),
    enabled: q.length > 0,
    staleTime: Infinity,
    placeholderData: keepPreviousData,
  });
}
