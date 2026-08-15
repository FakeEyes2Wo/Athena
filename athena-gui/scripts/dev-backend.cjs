/** 仅启动后端(Python 网关)，用于单独调试后端。 */

const { spawn } = require("child_process");
const path = require("path");

const guiDir = path.resolve(__dirname, "..");
const repoRoot = path.resolve(guiDir, "..");
const port = process.env.ATHENA_GUI_PORT || "17601";

// 直接用 .venv 里的 python，避免 `uv run` 触发环境同步/网络导致启动失败。
const python = process.platform === "win32"
  ? path.join(repoRoot, ".venv", "Scripts", "python.exe")
  : path.join(repoRoot, ".venv", "bin", "python");

console.error(`[dev-backend] 启动后端: ${python} -m gui_gateway (port=${port})`);

const child = spawn(python, ["-m", "gui_gateway"], {
  cwd: repoRoot,
  env: { ...process.env, ATHENA_GUI_PORT: port },
  stdio: "inherit",
});

child.on("error", (err) => {
  console.error("[dev-backend] 启动失败:", err.message);
});

child.on("exit", (code, signal) => {
  console.error(`[dev-backend] 后端已退出 (code=${code}, signal=${signal})`);
  process.exit(code ?? 0);
});

process.on("SIGINT", () => {
  child.kill("SIGINT");
  setTimeout(() => process.exit(0), 300);
});
process.on("SIGTERM", () => {
  child.kill("SIGINT");
});
