import { expect, test } from "@playwright/test";
import { fitsWidth, loadFixture, mockApi } from "./mockApi";

const fixture = loadFixture("failed-build");
const url = `/builds/${fixture.build_id}`;
const SHA = "41d8e37920445bf2b14232549a04360c622148b8";
const PASSED_SHA = "3c9fc2af798c6d5725210693151df0d61346eb06";

let unmatched: string[];
test.beforeEach(async ({ page }) => {
  unmatched = await mockApi(page, fixture);
});
test.afterEach(() => {
  expect(unmatched, "requests without a recorded response").toEqual([]);
});

test("header shows what was built, where and with what", async ({ page }) => {
  await page.goto(url);
  const header = page.locator(".bhead");
  await expect(header.getByRole("heading", { name: "#2" })).toBeVisible();
  await expect(header.locator(".status")).toHaveText("failed");
  // The profile is the job named in the run report.
  await expect(header.getByTitle("profile")).toHaveText("host-cmake");
  await expect(header.getByText("main", { exact: true })).toBeVisible();
  await expect(header.getByRole("link", { name: SHA.slice(0, 10) })).toHaveAttribute(
    "href",
    `https://github.com/acme/fw/commit/${SHA}`,
  );
  await expect(header.getByText("Expect a second rising edge in noisy-signal test")).toBeVisible();
  await expect(header.getByText(/m0hiith · push · queued/)).toBeVisible();
  await expect(header.getByText("gcc 13.3.0")).toBeVisible();
  await expect(header.getByText("cmake 3.28.3")).toBeVisible();
  await expect(header.getByTitle("duration")).toHaveText("0:13");
  await expect(header.getByRole("link", { name: /Actions run 30433701/ })).toHaveAttribute(
    "href",
    "https://github.com/acme/fw/actions/runs/30433701/attempts/1",
  );
});

test("timeline colours stages by status and greys out skipped ones", async ({ page }) => {
  await page.goto(url);
  const segs = page.locator(".timeline .seg");
  await expect(segs).toHaveCount(5);
  await expect(page.locator('[data-stage="configure"]')).toHaveClass(/seg--passed/);
  await expect(page.locator('[data-stage="test"]')).toHaveClass(/seg--failed/);
  await expect(page.locator('[data-stage="analyze"]')).toHaveClass(/seg--skipped/);
  await expect(page.locator('[data-stage="package"]')).toHaveClass(/seg--skipped/);
  // configure took 4 s, the others under a second: it gets the widest bar.
  const configure = await page.locator('[data-stage="configure"]').boundingBox();
  const compile = await page.locator('[data-stage="compile"]').boundingBox();
  expect(configure!.width).toBeGreaterThan(compile!.width * 3);

  // Clicking a skipped stage explains why there is no log.
  await page.locator('[data-stage="analyze"]').click();
  await expect(page).toHaveURL(/stage=analyze/);
  await expect(page.getByText("The stage was skipped, so it produced no log.")).toBeVisible();
});

test("failure card names the cause, links the source and the last passing build", async ({ page }) => {
  await page.goto(url);
  const card = page.getByRole("region", { name: "failure summary" });
  await expect(card.getByText("test_failure")).toBeVisible();
  await expect(card.getByText("test_noisy_signal_around_one_threshold: Expected 2 Was 1")).toBeVisible();
  await expect(card.getByRole("link", { name: "firmware/tests/test_threshold.c:75" })).toHaveAttribute(
    "href",
    `https://github.com/acme/fw/blob/${SHA}/firmware/tests/test_threshold.c#L75`,
  );
  await expect(card.getByText("test_threshold", { exact: true })).toBeVisible();
  await expect(card.getByText("— Failed")).toBeVisible();

  const last = card.getByTestId("last-passing");
  await expect(last).toHaveText(`#1 (${PASSED_SHA.slice(0, 10)}), 1 commit between`);
  await expect(last.getByRole("link", { name: "1 commit between" })).toHaveAttribute(
    "href",
    `https://github.com/acme/fw/compare/${PASSED_SHA}...${SHA}`,
  );

  // "log line 15" opens the failed stage's log at the error.
  await card.getByRole("button", { name: "log line 15" }).click();
  await expect(page).toHaveURL(/tab=logs&stage=test&line=15/);
  await expect(page.locator('.log__line--active[data-line="15"]')).toContainText(
    "test_threshold.c:75:test_noisy_signal_around_one_threshold:FAIL: Expected 2 Was 1",
  );
});

test("log viewer opens the failed stage, searches and steps through errors", async ({ page }) => {
  await page.goto(url);
  await expect(page.getByRole("button", { name: "test", exact: true, pressed: true })).toBeVisible();
  await expect(page.getByText("29 lines")).toBeVisible();
  await expect(page.locator('.log__line--error[data-line="8"]')).toBeVisible();

  // Errors are at lines 8, 15 and 27.
  await expect(page.getByText("3 errors")).toBeVisible();
  const next = page.getByRole("button", { name: "next error" });
  await next.click();
  await expect(page).toHaveURL(/line=8/);
  await next.click();
  await next.click();
  await expect(page.locator(".log__line--active")).toHaveAttribute("data-line", "27");
  await next.click(); // wraps
  await expect(page.locator(".log__line--active")).toHaveAttribute("data-line", "8");
  await page.getByRole("button", { name: "previous error" }).click();
  await expect(page.locator(".log__line--active")).toHaveAttribute("data-line", "27");
  await expect(page.getByRole("button", { name: "next warning" })).toBeDisabled();

  // Search is server-side; Enter jumps to the next match after the current line.
  const search = page.getByLabel("search log");
  await search.fill("fail:");
  await search.press("Enter");
  await expect(page.locator(".log__line--active")).toHaveAttribute("data-line", "15");
  await expect(page.locator(".log__count")).toHaveText("1/1 match");
  await expect(page.locator('.log__line[data-line="15"] mark')).toHaveText("FAIL:");

  await search.fill("threshold");
  // Line 15 (still active) is the 9th of 11 matches.
  await expect(page.locator(".log__count")).toHaveText("9/11 matches");
  await page.getByRole("button", { name: "next match" }).click();
  await expect(page.locator(".log__line--active")).toHaveAttribute("data-line", "16");
  await expect(page.locator(".log__count")).toHaveText("10/11 matches");
});

test("a deep link opens a log line", async ({ page }) => {
  await page.goto(`${url}?tab=logs&stage=test&line=27`);
  await expect(page.locator(".log__line--active")).toHaveAttribute("data-line", "27");
  await expect(page.locator(".log__line--active")).toContainText("The following tests FAILED:");
});

test("tests tab filters by outcome", async ({ page }) => {
  await page.goto(url);
  await page.getByRole("tab", { name: "tests" }).click();
  const rows = page.getByRole("table", { name: "test cases" }).locator("tbody tr");
  await expect(rows).toHaveCount(3);
  await page.getByRole("button", { name: /^failed/ }).click();
  await expect(page).toHaveURL(/outcome=failed/);
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toContainText("test_threshold");
  await expect(rows.first()).toContainText("Failed");
});

test("artifacts tab explains why there is nothing to release", async ({ page }) => {
  await page.goto(`${url}?tab=artifacts`);
  await expect(page.getByText("No artifacts", { exact: true })).toBeVisible();
  await expect(page.getByText("Gates are evaluated only for passed builds")).toBeVisible();
});

test("size tab says when a build reported no size", async ({ page }) => {
  // The recorded host-cmake run predates size reporting.
  await page.goto(`${url}?tab=size`);
  await expect(page.getByText("No size reported", { exact: true })).toBeVisible();
});

test("usable at 1280 px without horizontal scrolling", async ({ page }) => {
  for (const tab of ["logs", "tests", "artifacts"]) {
    await page.goto(`${url}?tab=${tab}`);
    await expect(page.getByRole("tab", { name: tab })).toHaveAttribute("aria-selected", "true");
    await expect(page.locator(".state--loading")).toHaveCount(0);
    expect(await fitsWidth(page), `${tab} tab fits 1280 px`).toBe(true);
  }
});
