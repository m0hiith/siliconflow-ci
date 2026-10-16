import { expect, test } from "@playwright/test";
import type { BuildDetail } from "../src/api/client";
import { fitsWidth, loadFixture, mockApi } from "./mockApi";

const fixture = loadFixture("passed-build");
const url = `/builds/${fixture.build_id}`;
const FIRMWARE = "6e8271fa925aae0262d2b6aa3d9496189b83e411455ea9955a05f8ff0ebda0f4";
const FIRMWARE_BIN = "ecc2cb082edfc6affe24a3f31577f7dc653002845d091c8aa1706245d1c065f6";

let unmatched: string[];
test.beforeEach(async ({ page }) => {
  unmatched = await mockApi(page, fixture);
});
test.afterEach(() => {
  expect(unmatched, "requests without a recorded response").toEqual([]);
});

test("a passed build has no failure card and an all-green timeline", async ({ page }) => {
  await page.goto(url);
  await expect(page.locator(".bhead .status")).toHaveText("passed");
  await expect(page.getByRole("heading", { name: "#1" })).toBeVisible();
  await expect(page.getByText("Add hysteresis to threshold detector")).toBeVisible();
  await expect(page.getByRole("region", { name: "failure summary" })).toHaveCount(0);
  // The five required stages and the optional coverage stage.
  await expect(page.locator(".timeline .seg--passed")).toHaveCount(6);
  await expect(page.locator('[data-stage="coverage"]')).toHaveClass(/seg--passed/);
  // No failed stage: the log tab starts at the first stage.
  await expect(page.getByRole("button", { name: "configure", exact: true, pressed: true })).toBeVisible();
  await expect(page.locator('.log__line[data-line="1"]')).toContainText("The C compiler identification is GNU 13.3.0");
});

test("clicking a stage opens its log, which can be searched", async ({ page }) => {
  await page.goto(url);
  await page.locator('[data-stage="compile"]').click();
  await expect(page).toHaveURL(/stage=compile/);
  await expect(page.getByText("15 lines")).toBeVisible();
  await expect(page.getByText("0 errors")).toBeVisible();
  await expect(page.getByRole("button", { name: "next error" })).toBeDisabled();
  const search = page.getByLabel("search log");
  await search.fill("linking");
  await expect(page.locator(".log__count")).toHaveText("7 matches");
  await search.press("Enter");
  await expect(page.locator(".log__line--active")).toHaveAttribute("data-line", "6");
});

test("artifacts show server checksums, a verify command and gate results", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto(`${url}?tab=artifacts`);

  const rows = page.getByRole("table", { name: "artifacts" }).locator("tbody tr");
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toContainText("firmware.bin");
  await expect(rows.nth(0)).toContainText("142 KiB");
  await expect(rows.nth(0)).toContainText(FIRMWARE_BIN);
  await expect(rows.nth(1).getByRole("link", { name: "firmware" })).toHaveAttribute(
    "href",
    /\/api\/v1\/artifacts\/art_[0-9A-Z]+\/download$/,
  );

  const command = `sha256sum -c <<'EOF'\n${FIRMWARE_BIN}  firmware.bin\n${FIRMWARE}  firmware\nEOF`;
  await expect(page.getByTestId("verify-command")).toHaveText(command);
  await page.getByRole("button", { name: "copy command" }).click();
  await expect(page.getByRole("button", { name: "copied" })).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(command);

  const gates = page.getByRole("region", { name: "release gates" });
  await expect(gates.getByText("release-eligible: all 11 checks passed")).toBeVisible();
  for (const i of [0, 1]) await expect(rows.nth(i).locator("td").nth(4)).toHaveText("eligible");
  const coverage = gates.locator("tr", { hasText: "line_coverage_min" });
  await expect(coverage.locator("td").nth(1)).toHaveText("pass");
  await expect(coverage.locator("td").nth(2)).toHaveText("100");
  await expect(coverage.locator("td").nth(3)).toHaveText("80");
  // The host-cmake size limit now applies to this host-cmake build.
  const size = gates.locator("tr", { hasText: "text_bytes_max" });
  await expect(size.locator("td").nth(1)).toHaveText("pass");
  await expect(size.locator("td").nth(2)).toHaveText("30063");
  await expect(size.locator("td").nth(3)).toHaveText("40960");
  const tests = gates.locator("tr", { hasText: "test_failures" });
  await expect(tests.locator("td").nth(1)).toHaveText("pass");
  await expect(gates.locator("tr", { hasText: "release_ref" }).locator("td").nth(3)).toHaveText(
    "refs/heads/main, refs/tags/v*",
  );
});

test("artifact provenance names the commit, the run and the reported versions", async ({ page }) => {
  await page.goto(`${url}?tab=artifacts`);
  const build = fixture.responses[`/api/v1/builds/${fixture.build_id}`] as BuildDetail;

  const provenance = page.getByRole("region", { name: "provenance" });
  await expect(provenance.getByRole("link", { name: build.commit_sha!.slice(0, 10) })).toHaveAttribute(
    "href",
    build.commit_url!,
  );
  await expect(provenance.getByRole("link", { name: `run ${build.gh_run_id}` })).toHaveAttribute("href", build.run_url!);
  await expect(provenance).toContainText(`profile ${build.profile}`);
  // This host build used no vendor SDK; its tool versions come from `--version`.
  await expect(provenance.getByText("gcc 13.3.0")).toBeVisible();
  await expect(provenance.getByText("cmake 3.28.3")).toBeVisible();
});

test("tests tab lists every case", async ({ page }) => {
  await page.goto(`${url}?tab=tests`);
  await expect(page.getByRole("table", { name: "test cases" }).locator("tbody tr")).toHaveCount(3);
  await expect(page.getByRole("button", { name: /^passed/ })).toContainText("3");
});

test("usable at 1280 px without horizontal scrolling", async ({ page }) => {
  for (const tab of ["logs", "tests", "artifacts"]) {
    await page.goto(`${url}?tab=${tab}`);
    await expect(page.getByRole("tab", { name: tab })).toHaveAttribute("aria-selected", "true");
    await expect(page.locator(".state--loading")).toHaveCount(0);
    expect(await fitsWidth(page), `${tab} tab fits 1280 px`).toBe(true);
  }
});
