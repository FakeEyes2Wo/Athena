#!/usr/bin/env node
/** Athena 全项目构建脚本：后端可选 Python 或 TypeScript，再构建前端 + Rust(Tauri)。 */

const { spawnSync } = require("child_process");
const path = require("path");

const repoRoot = path.resolve(__dirname, "..");
const args = process.argv.slice(2);

function flagValue(flag) {
  const i = args.indexOf(flag);
  return i >= 0 && args[i + 1] ? args[i + 1] : null;
}

const backend = flagValue("--backend") || "python";
const release = args.includes("--release");
const skipFrontend = args.includes("--skip-frontend");
const skipRust = args.includes("--skip-rust");

if (backend !== "python" && backend !== "ts") {
  console.error("用法: node scripts/build.cjs --backend <python|ts> [--release] [--skip-frontend] [--skip-rust]");
  process.exit(2);
}

function run(step, command, cwd) {
  console.log(`\n=== ${step} ===`);
  console.log(`> ${command}\n   (cwd: ${cwd})`);
  const result = spawnSync(command, { cwd, stdio: "inherit", shell: true });
  if (result.status !== 0) {
    console.error(`[build] ${step} 失败 (exit ${result.status ?? "signal"})`);
    process.exit(result.status ?? 1);
  }
}

console.log(`[build] 后端构建方式: ${backend === "python" ? "Python" : "TypeScript"}`);

// 1. 后端
if (backend === "python") {
  run("Python 后端 · 安装依赖", "uv sync", repoRoot);
  run("Python 后端 · 构建产物", "uv build", repoRoot);
} else {
  const tsRoot = path.join(repoRoot, "athena_ts");
  run("TypeScript 后端 · 安装依赖", "npm install", tsRoot);
  run("TypeScript 后端 · 编译", "npm run build", tsRoot);
}

// 2. 前端
if (!skipFrontend) {
  run("前端 · 构建", "npm run build", path.join(repoRoot, "athena-gui"));
} else {
  console.log("\n[build] 跳过前端构建");
}

// 3. Rust (Tauri)
if (!skipRust) {
  const cargo = release ? "cargo build --release" : "cargo build";
  run(`Rust (Tauri) · ${release ? "release" : "debug"} 构建`, cargo, path.join(repoRoot, "athena-gui", "src-tauri"));
} else {
  console.log("\n[build] 跳过 Rust 构建");
}

console.log("\n[build] 全部完成");
