/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  // Playwright owns e2e/.
  test: { include: ["src/**/*.test.ts"] },
  server: {
    // The API is same-origin in production (nginx proxies /api); mirror that in dev.
    proxy: { "/api": process.env.SFCI_API_URL ?? "http://localhost:8000" },
  },
});
