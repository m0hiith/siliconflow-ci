import { describe, expect, it } from "vitest";
import type { Build } from "./api/client";
import { parseFilters, toSearchParams } from "./filters";
import { buildSeconds, formatDuration, formatUtc } from "./format";
import { isTerminal, statusTone } from "./status";

describe("filters", () => {
  it("round-trips through the query string", () => {
    const qs = "project=prj_1&branch=main&profile=host-cmake&status=running&status=failed";
    const filters = parseFilters(new URLSearchParams(qs));
    expect(filters).toEqual({
      project: "prj_1",
      branch: "main",
      profile: "host-cmake",
      status: ["running", "failed"],
    });
    expect(parseFilters(toSearchParams(filters))).toEqual(filters);
  });

  it("drops unknown statuses, duplicates and blank values", () => {
    const filters = parseFilters(new URLSearchParams("status=bogus&status=passed&status=passed&branch=+"));
    expect(filters).toEqual({ status: ["passed"] });
    expect(toSearchParams({ status: [], branch: " " }).toString()).toBe("");
  });
});

describe("status", () => {
  it("only queued and running are non-terminal", () => {
    expect(isTerminal("queued")).toBe(false);
    expect(isTerminal("running")).toBe(false);
    for (const s of ["passed", "failed", "cancelled", "timed_out", "errored"] as const) {
      expect(isTerminal(s)).toBe(true);
    }
  });

  it("errored is an infrastructure outcome, not a firmware failure", () => {
    expect(statusTone("errored")).toBe("neutral");
    expect(statusTone("timed_out")).toBe("failed");
  });
});

describe("format", () => {
  it("formats durations", () => {
    expect(formatDuration(0)).toBe("0:00");
    expect(formatDuration(187.4)).toBe("3:07");
    expect(formatDuration(3725)).toBe("1:02:05");
  });

  it("measures running builds up to now and unstarted builds as null", () => {
    const base = { started_at: "2026-10-14T09:00:00Z", finished_at: null } as Build;
    expect(buildSeconds(base, Date.parse("2026-10-14T09:01:30Z"))).toBe(90);
    expect(buildSeconds({ ...base, started_at: null }, Date.now())).toBeNull();
  });

  it("formats UTC timestamps", () => {
    expect(formatUtc("2026-10-14T09:00:12.345+00:00")).toBe("2026-10-14 09:00:12Z");
  });
});
