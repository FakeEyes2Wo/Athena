/** Start the Python gateway, optionally alongside the Vite development server. */

const { spawn, spawnSync } = require("child_process");
const path = require("path");

const guiDir = path.resolve(__dirname, "..");
const repoRoot = path.resolve(guiDir, "..");
const port = process.env.ATHENA_GUI_PORT || "17601";
const backendOnly = process.argv.includes("--backend-only");
const python = process.platform === "win32"
  ? path.join(repoRoot, ".venv", "Scripts", "python.exe")
  : path.join(repoRoot, ".venv", "bin", "python");
const children = [];
let shuttingDown = false;

function monitor(name, child) {
  children.push(child);
  child.on("error", (error) => {
    console.error(`[dev] ${name} failed to start:`, error.message);
    shutdown(1);
  });
  child.on("exit", (code, signal) => {
    console.error(`[dev] ${name} exited (code=${code}, signal=${signal})`);
  });
  return child;
}

console.error(`[dev] backend: ${python} -m gui_gateway (port=${port})`);
const backend = monitor("backend", spawn(python, ["-m", "gui_gateway"], {
  cwd: repoRoot,
  env: { ...process.env, ATHENA_GUI_PORT: port },
  stdio: "inherit",
}));
backend.on("exit", (code) => {
  if (shuttingDown) return;
  console.error("[dev] backend is unavailable; stopping the frontend.");
  shutdown(code || 1);
});

if (!backendOnly) {
  console.error("[dev] frontend: npm run dev");
  monitor("frontend", spawn("npm", ["run", "dev"], {
    cwd: guiDir,
    env: { ...process.env, VITE_GUI_PORT: port },
    stdio: "inherit",
    shell: process.platform === "win32",
  }));
}

function shutdown(code = 0) {
  if (shuttingDown) return;
  shuttingDown = true;
  for (const child of children) {
    if (!child.pid || child.exitCode !== null || child.signalCode !== null) continue;
    if (process.platform === "win32") {
      spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], {
        stdio: "ignore", windowsHide: true,
      });
    } else {
      child.kill("SIGTERM");
    }
  }
  setTimeout(() => process.exit(code), 300);
}

process.on("SIGINT", () => shutdown(0));
process.on("SIGTERM", () => shutdown(0));
