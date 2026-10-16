import createClient from "openapi-fetch";
import type { components, paths } from "./schema";

type Schemas = components["schemas"];

export type Build = Schemas["BuildOut"];
export type BuildDetail = Schemas["BuildDetailOut"];
export type Stage = Schemas["StageOut"];
export type StageStatus = Schemas["StageStatus"];
export type StageLog = Schemas["StageLogOut"];
export type LogSearch = Schemas["LogSearchOut"];
export type TestCase = Schemas["TestCaseOut"];
export type TestOutcome = Schemas["TestOutcome"];
export type Artifact = Schemas["ArtifactOut"];
export type GateResult = Schemas["GateResultOut"];
export type PreviousPassing = Schemas["PreviousPassingOut"];
export type BuildSize = Schemas["BuildSizeOut"];
export type BuildSizeCompare = Schemas["BuildSizeCompareOut"];
export type BuildPage = components["schemas"]["BuildPage"];
export type BuildStatus = components["schemas"]["BuildStatus"];
export type MetricsSummary = components["schemas"]["MetricsSummaryOut"];
export type Project = components["schemas"]["ProjectOut"];
export type Durations = Schemas["DurationsOut"];
export type DurationDay = Schemas["DurationDayOut"];
export type PassRate = Schemas["PassRateOut"];
export type PassRateWeek = Schemas["PassRateWeekOut"];
export type SizeTrends = Schemas["SizeTrendsOut"];
export type SizeSeries = Schemas["SizeSeriesOut"];
export type SizeCommit = Schemas["SizeCommitOut"];
export type FlakyTests = Schemas["FlakyTestsOut"];
export type FlakyTest = Schemas["FlakyTestOut"];

// Same origin: nginx (production) and the Vite dev server both proxy /api.
export const api = createClient<paths>({ baseUrl: "" });

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

/** The response body, or an ApiError carrying FastAPI's `detail`. */
export async function unwrap<T>(
  request: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  const { data, error, response } = await request;
  if (response.ok && data !== undefined) return data;
  throw new ApiError(response.status, errorMessage(error) ?? `HTTP ${response.status}`);
}

function errorMessage(error: unknown): string | undefined {
  if (typeof error !== "object" || error === null || !("detail" in error)) return undefined;
  const { detail } = error;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => (typeof d === "object" && d && "msg" in d ? String(d.msg) : String(d)))
      .join("; ");
  }
  return undefined;
}
