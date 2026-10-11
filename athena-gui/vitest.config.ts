import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    // scripts/dev.test.cjs 是 node:test 用例，不归 Vitest 运行。
    include: ["src/**/*.test.{ts,tsx}"],
    // @primer/react 的 ESM 产物直接 import .css，需交给 Vite 转换，不能交给 Node 加载。
    server: { deps: { inline: [/@primer\/react/] } },
  },
});
