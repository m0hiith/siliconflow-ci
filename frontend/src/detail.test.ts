import { describe, expect, it } from "vitest";
import { formatBytes, stripAnsi, toolVersion, versionChips } from "./format";
import { splitMatches, stepTo } from "./logNav";
import { formatDelta, formatPercent, sectionRows, totalRows } from "./size";

describe("stepTo", () => {
  const errors = [8, 15, 27];
  it("moves to the next and previous line, wrapping", () => {
    expect(stepTo(errors, undefined, 1)).toBe(8);
    expect(stepTo(errors, undefined, -1)).toBe(27);
    expect(stepTo(errors, 8, 1)).toBe(15);
    expect(stepTo(errors, 10, 1)).toBe(15);
    expect(stepTo(errors, 27, 1)).toBe(8);
    expect(stepTo(errors, 15, -1)).toBe(8);
    expect(stepTo(errors, 8, -1)).toBe(27);
    expect(stepTo([], 3, 1)).toBeUndefined();
  });
});

describe("splitMatches", () => {
  it("splits case-insensitively and keeps the original text", () => {
    expect(splitMatches("FAIL: fail", "fail")).toEqual([
      { text: "FAIL", match: true },
      { text: ": ", match: false },
      { text: "fail", match: true },
    ]);
    expect(splitMatches("abc", "")).toEqual([{ text: "abc", match: false }]);
  });
});

describe("format helpers", () => {
  it("picks the version a tool prints last", () => {
    expect(toolVersion("gcc (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0")).toBe("13.3.0");
    expect(toolVersion("cmake version 3.28.3\n\nCMake suite maintained by Kitware")).toBe("3.28.3");
    expect(toolVersion(null)).toBeNull();
  });

  it("makes a chip for each reported version, in report order", () => {
    expect(
      versionChips({ simplicity_sdk: "2026.6.1", wiseconnect: "4.1.1", missing: null, empty: "" }),
    ).toEqual([
      { name: "simplicity_sdk", version: "2026.6.1", raw: "2026.6.1" },
      { name: "wiseconnect", version: "4.1.1", raw: "4.1.1" },
    ]);
    expect(versionChips({ gcc: "gcc (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0" })).toEqual([
      { name: "gcc", version: "13.3.0", raw: "gcc (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0" },
    ]);
    expect(versionChips(null)).toEqual([]);
  });

  it("formats sizes and strips colour codes", () => {
    expect(formatBytes(351)).toBe("351 B");
    expect(formatBytes(96976)).toBe("95 KiB");
    expect(formatBytes(1536)).toBe("1.5 KiB");
    expect(stripAnsi("\x1b[01;31merror:\x1b[m x")).toBe("error: x");
  });
});

describe("size deltas", () => {
  const size = (text: number, data: number, bss: number, sections: [string, number][]) => ({
    file_name: "firmware.elf",
    text_bytes: text,
    data_bytes: data,
    bss_bytes: bss,
    sections: sections.map(([name, n]) => ({ name, size: n, addr: 0 })),
  });

  it("compares totals with the base build", () => {
    const rows = totalRows(size(1100, 8, 0, []), size(1000, 8, 64, []));
    expect(rows.map((r) => [r.name, r.bytes, r.base, r.delta])).toEqual([
      ["text", 1100, 1000, 100],
      ["data", 8, 8, 0],
      ["bss", 0, 64, -64],
    ]);
    expect(rows[0]?.percent).toBe(10);
    expect(rows[2]?.percent).toBe(-100);
  });

  it("has no delta without a base, and no percentage from a zero base", () => {
    expect(totalRows(size(1, 2, 3, []), null).every((r) => r.delta === null && r.percent === null)).toBe(true);
    const [, data] = totalRows(size(0, 4, 0, []), size(0, 0, 0, []));
    expect([data?.delta, data?.percent]).toEqual([4, null]);
  });

  it("matches sections by name, keeping sections only one side has", () => {
    const rows = sectionRows(size(0, 0, 0, [[".text", 900], [".new", 4]]), size(0, 0, 0, [[".text", 1000], [".gone", 8]]));
    expect(rows.map((r) => [r.name, r.bytes, r.base, r.delta])).toEqual([
      [".text", 900, 1000, -100],
      [".new", 4, 0, 4],
      [".gone", 0, 8, -8],
    ]);
  });

  it("formats signed deltas and percentages", () => {
    expect([formatDelta(1024), formatDelta(-12), formatDelta(0), formatDelta(null)]).toEqual(["+1,024", "-12", "0", ""]);
    expect([formatPercent(10), formatPercent(-0.5), formatPercent(null)]).toEqual(["+10.00%", "-0.50%", ""]);
  });
});
