import { expect, test } from "@playwright/test";
import { fitsWidth, mockApi } from "./mockApi";

// The page is static: with no recorded responses, any API call it makes
// (other than the demo-mode check every page does) is collected as unmatched.
let unmatched: string[];
test.beforeEach(async ({ page }) => {
  unmatched = await mockApi(page, { build_id: "", responses: {} });
});
test.afterEach(() => {
  expect(unmatched, "requests without a recorded response").toEqual([]);
});

test("How it works is a nav tab, with diagrams and screenshots", async ({ page }) => {
  await page.goto("/how-it-works");
  await expect(
    page.getByRole("navigation", { name: "main" }).getByRole("link", { name: "How it works" }),
  ).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("binary you can prove is safe to ship");
  for (const name of ["The journey of a commit", "Verify, don't trust", "Build status lifecycle", "When a build fails, who fixes it?"]) {
    await expect(page.getByRole("heading", { name })).toBeVisible();
  }
  await expect(page.getByRole("img", { name: /^Architecture:/ })).toBeVisible();
  await expect(page.getByRole("img", { name: /^Sequence:/ })).toBeVisible();
  await expect(page.getByRole("img", { name: /^Build status lifecycle:/ })).toBeVisible();
  const shots = page.locator(".hw-shot img");
  await expect(shots).toHaveCount(2);
  for (const img of await shots.all()) {
    await img.scrollIntoViewIfNeeded();
    // Lazy-loaded: wait for it to arrive and decode.
    await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth > 0)).toBe(true);
  }
});

test("the page fits a phone screen", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/how-it-works");
  await expect(page.getByRole("heading", { name: "Key terms" })).toBeVisible();
  expect(await fitsWidth(page)).toBe(true);
});

test("the theme toggle switches and remembers the theme", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/how-it-works");
  await page.getByRole("button", { name: "Switch to dark theme" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.getByRole("button", { name: "Switch to light theme" })).toBeVisible();
});
