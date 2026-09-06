const assert = require("node:assert/strict");
const net = require("node:net");
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const path = require("node:path");
const test = require("node:test");

const guiRoot = path.resolve(__dirname, "..");

function stopTree(pid) {
  if (!pid) return Promise.resolve();
  if (process.platform === "win32") {
    const killer = spawn("taskkill", ["/pid", String(pid), "/t", "/f"], {
      stdio: "ignore",
    });
    return once(killer, "exit").then(() => undefined);
  }
  process.kill(-pid, "SIGTERM");
  return Promise.resolve();
}

test("dev launcher stops when its backend cannot bind the selected port", async (t) => {
  const listener = net.createServer();
  await new Promise((resolve, reject) => {
    listener.once("error", reject);
    listener.listen(0, "127.0.0.1", resolve);
  });
  const { port } = listener.address();
  const child = spawn(process.execPath, ["scripts/dev.cjs"], {
    cwd: guiRoot,
    env: { ...process.env, ATHENA_GUI_PORT: String(port) },
    stdio: ["ignore", "ignore", "pipe"],
  });
  let stderr = "";
  child.stderr.on("data", (chunk) => {
    stderr += chunk;
  });
  t.after(async () => {
    await stopTree(child.pid);
    await new Promise((resolve) => listener.close(resolve));
  });

  let deadline;
  const outcome = await Promise.race([
    once(child, "exit").then(([code, signal]) => ({ code, signal })),
    new Promise((resolve) => { deadline = setTimeout(() => resolve(null), 15_000); }),
  ]);
  clearTimeout(deadline);

  assert.notEqual(outcome, null, "launcher kept running after backend failure");
  assert.notEqual(outcome.code, 0);
  assert.match(stderr, /backend exited/);
});
