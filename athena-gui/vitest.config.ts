import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    // scripts/*.test.cjs are node:test suites, run by ``npm run test:scripts``.
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
  },
});
