const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../build.cjs"), "utf8");

function simulate(args, platform = "win32", failure = 0) {
  const calls = [];
  const removals = [];
  let exitCode;
  const fakeRoot = path.resolve("checkout with spaces");
  const stop = {};
  const context = {
    __dirname: path.join(fakeRoot, "scripts"),
    console: { log() {}, error() {} },
    process: {
      argv: ["node", "build.cjs", ...args],
      platform,
      exit(code) { exitCode = code; throw stop; },
    },
    require(name) {
      if (name === "child_process") return {
        spawnSync(...call) { calls.push(call); return { status: failure }; },
      };
      if (name === "fs") return {
        mkdirSync() {},
        rmSync(target) { removals.push(target); },
      };
      if (name === "path") return path;
      throw new Error(`unexpected dependency: ${name}`);
    },
  };
  try { vm.runInNewContext(source, context); }
  catch (error) { if (error !== stop) throw error; }
  return { calls, removals, exitCode, fakeRoot };
}

test("packaging preserves executable and argument boundaries in paths with spaces", () => {
  const { calls, fakeRoot, exitCode } = simulate(["--package"]);
  assert.equal(exitCode, 0);
  assert.equal(calls[0][0], path.join(fakeRoot, ".venv", "Scripts", "python.exe"));
  assert.ok(Array.isArray(calls[0][1]));
  assert.equal(calls[0][2].shell, false);
  const index = calls[0][1].indexOf("--distpath");
  assert.equal(calls[0][1][index + 1], path.join(fakeRoot, "athena-gui", "src-tauri", "resources"));
});

test("packaging cleanup owns only its PyInstaller work directory", () => {
  const { removals, fakeRoot } = simulate(["--package"]);
  assert.deepEqual(removals, [path.join(fakeRoot, "build", "pyinstaller")]);
});

test("a failed packaging step preserves work files and stops later steps", () => {
  const result = simulate(["--package"], "win32", 7);
  assert.equal(result.exitCode, 7);
  assert.equal(result.calls.length, 1);
  assert.deepEqual(result.removals, []);
});

test("invalid backend fails before executing commands", () => {
  const result = simulate(["--backend", "unsupported"]);
  assert.equal(result.exitCode, 2);
  assert.equal(result.calls.length, 0);
});

test("skipped frontend and native builds do not spawn them", () => {
  const result = simulate(["--skip-frontend", "--skip-rust"], "linux");
  assert.equal(result.calls.length, 2);
  assert.equal(result.calls[0][0], "uv");
  assert.deepEqual(Array.from(result.calls[1][1]), ["build"]);
});
