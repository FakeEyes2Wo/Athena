import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Default 1421 matches tauri.conf.json devUrl. Windows may reserve it (see
// `netsh interface ipv4 show excludedportrange protocol=tcp`); override with
// ATHENA_WEB_PORT for browser-only development.
const port = Number(process.env.ATHENA_WEB_PORT || 1421);

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],

  // Vite options tailored for Tauri development
  clearScreen: false,
  server: {
    port,
    strictPort: true,
    watch: {
      // Tell vite to ignore watching `src-tauri`
      ignored: ["**/src-tauri/**"],
    },
  },
});
