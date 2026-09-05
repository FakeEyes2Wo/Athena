#!/usr/bin/env node
/** Athena 全项目构建/打包脚本：统一后端、前端与 Tauri 流程。 */

const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const repoRoot = path.resolve(__dirname, "..");
const args = process.argv.slice(2);

function flagValue(flag) {
  const i = args.lastIndexOf(flag);
  return i >= 0 && args[i + 1] ? args[i + 1] : null;
}

const backend = flagValue("--backend") || "python";
const release = args.includes("--release");
const packaging = args.includes("--package");
const skipFrontend = args.includes("--skip-frontend");
const skipRust = args.includes("--skip-rust");

if (backend !== "python" && backend !== "ts") {
  console.error("用法: node scripts/build.cjs --backend <python|ts> [--package|--release] [--skip-frontend] [--skip-rust]");
  process.exit(2);
}

const operation = packaging ? "release" : "build";

function run(step, command, cwd) {
  console.log(`\n=== ${step} ===`);
  console.log(`> ${command}\n   (cwd: ${cwd})`);
  const result = spawnSync(command, { cwd, stdio: "inherit", shell: true });
  if (result.status !== 0) {
    console.error(`[${operation}] ${step} 失败 (exit ${result.status ?? "signal"})`);
    process.exit(result.status ?? 1);
  }
}

if (packaging) {
  console.log(`[release] 后端打包方式: ${backend === "python" ? "Python (PyInstaller)" : "TypeScript"}`);

  if (backend === "python") {
    const python = path.join(
      repoRoot,
      ".venv",
      process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
    );
    const outDir = path.join(repoRoot, "athena-gui", "src-tauri", "resources");
    const workDir = path.join(repoRoot, "build", "pyinstaller");
    fs.mkdirSync(outDir, { recursive: true });
    run(
      "Python 后端 · PyInstaller 打包",
      `${python} -m PyInstaller --onefile --noconfirm --name gui_gateway --paths src --collect-all athena --collect-all gui_gateway --copy-metadata genai_prices --copy-metadata pydantic_ai_slim --copy-metadata pydantic_ai --copy-metadata logfire_api --copy-metadata logfire --copy-metadata qoder_agent_sdk --copy-metadata pydantic --copy-metadata openai --distpath "${outDir}" --workpath "${workDir}" --specpath "${workDir}" scripts/gui_gateway_entry.py`,
      repoRoot,
    );
    fs.rmSync(path.join(repoRoot, "build"), { recursive: true, force: true });
  } else {
    const tsRoot = path.join(repoRoot, "athena_ts");
    run("TypeScript 后端 · 安装依赖", "npm install", tsRoot);
    run("TypeScript 后端 · 编译", "npm run build", tsRoot);
  }

  run("Tauri · release 打包", "npm run tauri build", path.join(repoRoot, "athena-gui"));
  console.log("\n[release] 完成");
  console.log("  安装器输出目录: athena-gui/src-tauri/target/release/bundle/");
  process.exit(0);
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
