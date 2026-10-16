import { readFileSync } from "node:fs";
import type { Page } from "@playwright/test";

export interface Fixture {
  build_id: string;
  /** API responses keyed by path (log searches by path + "?q=..."). */
  responses: Record<string, unknown>;
}

export function loadFixture(name: string): Fixture {
  return JSON.parse(readFileSync(new URL(`./fixtures/${name}.json`, import.meta.url), "utf8"));
}

const LOG_PATH = /^\/api\/v1\/builds\/[^/]+\/logs\/[^/]+$/;

/**
 * Serve /api/v1 from a fixture. Logs are stored whole and paged here the
 * way the API pages them; anything else must match a recorded path exactly.
 * Requests with no recorded response are collected so tests can assert none.
 */
export async function mockApi(page: Page, fixture: Fixture): Promise<string[]> {
  const unmatched: string[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const { responses } = fixture;
    let key = url.pathname;
    if (url.pathname.endsWith("/search")) key += `?q=${url.searchParams.get("q")}`;
    const body = responses[key] as Record<string, unknown> | undefined;

    if (body === undefined) {
      unmatched.push(`${url.pathname}${url.search}`);
      return route.fulfill({ status: 404, json: { detail: "no fixture for this request" } });
    }
    if (typeof body === "object" && body !== null && "status" in body && typeof body.status === "number") {
      return route.fulfill({ status: body.status, json: { detail: body.detail } });
    }
    if (LOG_PATH.test(url.pathname)) {
      const offset = Number(url.searchParams.get("offset") ?? 0);
      const limit = Number(url.searchParams.get("limit") ?? 1000);
      const lines = body.lines as string[];
      return route.fulfill({ json: { ...body, offset, lines: lines.slice(offset, offset + limit) } });
    }
    return route.fulfill({ json: body });
  });
  return unmatched;
}

/** True when the page itself does not scroll sideways. */
export async function fitsWidth(page: Page): Promise<boolean> {
  return page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth);
}
