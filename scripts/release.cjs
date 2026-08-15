#!/usr/bin/env node
/** Athena release 打包脚本：后端(Python→PyInstaller / TS→tsc) + Tauri release 安装器。 */

const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const repoRoot = path.resolve(__dirname, "..");
const args = process.argv.slice(2);

function flagValue(flag) {
  const i = args.indexOf(flag);
  return i >= 0 && args[i + 1] ? args[i + 1] : null;
}

const backend = flagValue("--backend") || "python";
if (backend !== "python" && backend !== "ts") {
  console.error("用法: node scripts/release.cjs --backend <python|ts>");
  process.exit(2);
}

function run(step, command, cwd) {
  console.log(`\n=== ${step} ===`);
  console.log(`> ${command}\n   (cwd: ${cwd})`);
  const result = spawnSync(command, { cwd, stdio: "inherit", shell: true });
  if (result.status !== 0) {
    console.error(`[release] ${step} 失败 (exit ${result.status ?? "signal"})`);
    process.exit(result.status ?? 1);
  }
}

console.log(`[release] 后端打包方式: ${backend === "python" ? "Python (PyInstaller)" : "TypeScript"}`);

// 1. 后端
if (backend === "python") {
  const python = process.platform === "win32"
    ? path.join(repoRoot, ".venv", "Scripts", "python.exe")
    : path.join(repoRoot, ".venv", "bin", "python");
  // 打包成单文件 exe，输出到 athena-gui/src-tauri/resources/gui_gateway(.exe)
  const outDir = path.join(repoRoot, "athena-gui", "src-tauri", "resources");
  fs.mkdirSync(outDir, { recursive: true });
  const onefile = process.platform === "win32" ? "--onefile" : "--onefile";
  run(
    "Python 后端 · PyInstaller 打包",
    `${python} -m PyInstaller --onefile --noconfirm --name gui_gateway --paths src --collect-all athena --collect-all gui_gateway --copy-metadata genai_prices --copy-metadata pydantic_ai_slim --copy-metadata pydantic_ai --copy-metadata logfire_api --copy-metadata logfire --copy-metadata qoder_agent_sdk --copy-metadata pydantic --copy-metadata openai --distpath "${outDir}" --workpath "${path.join(repoRoot, "build", "pyinstaller")}" --specpath "${path.join(repoRoot, "build", "pyinstaller")}" scripts/gui_gateway_entry.py`,
    repoRoot,
  );
  // 清理 PyInstaller 的 build 中间产物（保留 dist 里的 exe）
  fs.rmSync(path.join(repoRoot, "build"), { recursive: true, force: true });
} else {
  const tsRoot = path.join(repoRoot, "athena_ts");
  run("TypeScript 后端 · 安装依赖", "npm install", tsRoot);
  run("TypeScript 后端 · 编译", "npm run build", tsRoot);
}

// 2. Tauri release（会触发 beforeBuildCommand=npm run build + cargo build --release + 打安装器）
run("Tauri · release 打包", "npm run tauri build", path.join(repoRoot, "athena-gui"));

console.log("\n[release] 完成");
console.log("  安装器输出目录: athena-gui/src-tauri/target/release/bundle/");
