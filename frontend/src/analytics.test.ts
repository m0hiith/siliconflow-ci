import { describe, expect, it } from "vitest";
import { byProfile, dayOf, dayScale, failureClassLabel, flakyReasons, niceTicks, ordinalX, weeksBetween } from "./analytics";
import type { FlakyTest } from "./api/client";

describe("axes", () => {
  it("places days on a calendar scale, leaving gaps as gaps", () => {
    const x = dayScale("2026-10-01", "2026-10-11");
    expect(x("2026-10-01")).toBe(0);
    expect(x("2026-10-06")).toBe(0.5);
    expect(x("2026-10-11")).toBe(1);
    expect(dayScale("2026-10-01", "2026-10-01")("2026-10-01")).toBe(0.5);
  });

  it("takes the UTC day of an instant", () => {
    expect(dayOf("2026-10-14T23:30:00-02:00")).toBe("2026-10-15");
  });

  it("lists every week of the window, empty ones included", () => {
    expect(weeksBetween("2026-09-28", "2026-10-14")).toEqual(["2026-09-28", "2026-10-05", "2026-10-12"]);
    expect(weeksBetween("2026-10-12", "2026-10-12")).toEqual(["2026-10-12"]);
  });

  it("spaces ordinal items evenly and centres a single one", () => {
    expect([0, 1, 2].map((i) => ordinalX(i, 3))).toEqual([0, 0.5, 1]);
    expect(ordinalX(0, 1)).toBe(0.5);
  });

  it("picks round ticks that cover the data", () => {
    expect(niceTicks(0, 1)).toEqual([0, 0.5, 1]);
    expect(niceTicks(0, 100)).toEqual([0, 50, 100]);
    expect(niceTicks(7472, 7600)).toEqual([7450, 7500, 7550, 7600]);
    const flat = niceTicks(500, 500);
    expect(flat[0]!).toBeLessThan(500);
    expect(flat[flat.length - 1]!).toBeGreaterThan(500);
    expect(niceTicks(NaN, 1)).toEqual([]);
  });
});

describe("grouping and labels", () => {
  it("groups rows by profile in name order, keeping row order", () => {
    const rows = [
      { profile: "siwx917-soc", n: 1 },
      { profile: "cortex-m4", n: 2 },
      { profile: "siwx917-soc", n: 3 },
    ];
    expect(byProfile(rows)).toEqual([
      ["cortex-m4", [{ profile: "cortex-m4", n: 2 }]],
      ["siwx917-soc", [{ profile: "siwx917-soc", n: 1 }, { profile: "siwx917-soc", n: 3 }]],
    ]);
  });

  it("explains why a test is flaky", () => {
    const base: FlakyTest = {
      repository: "acme/fw", profile: "host-cmake", suite: "unit", name: "t",
      same_commit_shas: [], flips: null, examples: [],
    };
    expect(flakyReasons({ ...base, same_commit_shas: ["a"] }, 20)).toEqual(["passed and failed on the same commit"]);
    expect(flakyReasons({ ...base, same_commit_shas: ["a", "b"], flips: 5 }, 20)).toEqual([
      "passed and failed on 2 commits",
      "flipped 5× in the last 20 builds",
    ]);
  });

  it("labels failure classes", () => {
    expect(failureClassLabel("test_failure")).toBe("test failure");
    expect(failureClassLabel(null)).toBe("unclassified");
  });
});
