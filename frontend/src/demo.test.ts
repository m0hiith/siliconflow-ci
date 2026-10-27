import { describe, expect, it } from "vitest";
import { DEMO_BANNER_TEXT } from "./demo/DemoBanner";
import { TOUR } from "./demo/tourSteps";

describe("demo tour", () => {
  it("follows the README demo script", () => {
    expect(TOUR.map((s) => s.id)).toEqual([
      "problem",
      "clean",
      "details",
      "artifacts",
      "compile_error",
      "not_releasable",
      "redelivery",
      "tamper",
      "who_fixes",
      "analytics",
    ]);
  });

  it("replays only scenarios that exist", () => {
    const scenarios = new Set([
      "clean_main",
      "compile_error",
      "passed_not_releasable",
      "redelivery",
      "infra_hw",
      "tamper",
      "history",
    ]);
    for (const step of TOUR) {
      if (step.scenario) expect(scenarios).toContain(step.scenario.name);
      if (step.scenario?.after) expect(scenarios).toContain(step.scenario.after);
    }
  });

  it("waits for a build before routing to it", () => {
    const details = TOUR.find((s) => s.id === "details")!;
    expect(details.route!({})).toBeNull();
    expect(details.route!({ clean_main: { clean: "bld_X" } })).toBe("/builds/bld_X");
    const artifacts = TOUR.find((s) => s.id === "artifacts")!;
    expect(artifacts.route!({ clean_main: { clean: "bld_X" } })).toBe("/builds/bld_X?tab=artifacts");
  });

  it("labels demo mode exactly as the README promises", () => {
    expect(DEMO_BANNER_TEXT).toBe(
      "Demo mode — replayed sample inputs, processed by the real SiliconFlow backend. Not production results.",
    );
  });
});
