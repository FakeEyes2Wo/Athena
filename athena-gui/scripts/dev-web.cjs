/** 启动后端(Python 网关) + 前端(Vite) 用于浏览器预览调试。 */

const { spawn } = require("child_process");
const path = require("path");

const guiDir = path.resolve(__dirname, "..");
const repoRoot = path.resolve(guiDir, "..");
const port = process.env.ATHENA_GUI_PORT || "17601";

// 直接用 .venv 里的 python，避免 `uv run` 触发环境同步/网络导致启动失败。
const python = process.platform === "win32"
  ? path.join(repoRoot, ".venv", "Scripts", "python.exe")
  : path.join(repoRoot, ".venv", "bin", "python");

console.error(`[dev-web] 后端: ${python} -m gui_gateway (port=${port})`);
console.error("[dev-web] 前端: npm run dev");

const backend = spawn(python, ["-m", "gui_gateway"], {
  cwd: repoRoot,
  env: { ...process.env, ATHENA_GUI_PORT: port },
  stdio: "inherit",
});

const frontend = spawn("npm run dev", {
  cwd: guiDir,
  stdio: "inherit",
  shell: process.platform === "win32",
});

for (const [name, child] of [["backend", backend], ["frontend", frontend]]) {
  child.on("error", (err) => {
    console.error(`[dev-web] ${name} 启动失败:`, err.message);
  });
  child.on("exit", (code, signal) => {
    console.error(`[dev-web] ${name} 已退出 (code=${code}, signal=${signal})`);
  });
}

function shutdown() {
  backend.kill("SIGINT");
  frontend.kill("SIGINT");
  setTimeout(() => process.exit(0), 300);
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
