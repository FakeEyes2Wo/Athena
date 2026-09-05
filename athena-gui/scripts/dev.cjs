/** Start the Python gateway, optionally alongside the Vite development server. */

const { spawn } = require("child_process");
const path = require("path");

const guiDir = path.resolve(__dirname, "..");
const repoRoot = path.resolve(guiDir, "..");
const port = process.env.ATHENA_GUI_PORT || "17601";
const backendOnly = process.argv.includes("--backend-only");
const python = process.platform === "win32"
  ? path.join(repoRoot, ".venv", "Scripts", "python.exe")
  : path.join(repoRoot, ".venv", "bin", "python");
const children = [];

function monitor(name, child) {
  children.push(child);
  child.on("error", (error) => {
    console.error(`[dev] ${name} failed to start:`, error.message);
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

if (backendOnly) {
  backend.on("exit", (code) => process.exit(code ?? 0));
} else {
  console.error("[dev] frontend: npm run dev");
  monitor("frontend", spawn("npm", ["run", "dev"], {
    cwd: guiDir,
    stdio: "inherit",
    shell: process.platform === "win32",
  }));
}

function shutdown() {
  for (const child of children) {
    child.kill("SIGINT");
  }
  setTimeout(() => process.exit(0), 300);
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
