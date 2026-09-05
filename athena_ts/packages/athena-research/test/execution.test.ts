import { mkdtempSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, describe, expect, it } from "vitest"
import { LocalExecutionRuntime } from "../src/execution.js"

const directories: string[] = []
function workspace(): string {
  const dir = mkdtempSync(join(tmpdir(), "athena-exec-"))
  directories.push(dir)
  return dir
}
afterEach(() => {
  for (const dir of directories.splice(0)) rmSync(dir, { recursive: true, force: true })
})

describe("local command execution", () => {
  it("uses each command's workspace and preserves argv and output", async () => {
    const runtime = new LocalExecutionRuntime()
    expect(Object.keys(runtime)).toEqual([])
    expect(runtime).not.toHaveProperty("ensureEnvironment")
    for (const workdir of [workspace(), workspace()]) {
      const result = await runtime.run({ workdir, argv: [
        process.execPath, "-e",
        "process.stdout.write(JSON.stringify([process.cwd(), process.argv[1]])); process.stderr.write('warning')",
        "argument with spaces; not a shell command",
      ] })
      expect(result).toEqual({
        ok: true, exit_code: 0, stderr: "warning",
        stdout: JSON.stringify([workdir, "argument with spaces; not a shell command"]),
      })
    }
  })

  it("preserves nonzero exit status and both output streams", async () => {
    const result = await new LocalExecutionRuntime().run({ workdir: workspace(), argv: [
      process.execPath, "-e", "process.stdout.write('out'); process.stderr.write('err'); process.exit(7)",
    ] })
    expect(result).toEqual({ ok: false, stdout: "out", stderr: "err", exit_code: 7 })
  })

  it.each([null, []])("rejects empty argv without emitting a start (%j)", async (argv) => {
    const events: string[] = []
    const result = await new LocalExecutionRuntime().run({
      workdir: workspace(), argv, emit: async (kind) => { events.push(kind) },
    })
    expect(result).toEqual({ ok: false, stdout: "", stderr: "argv must be a non-empty list", exit_code: 1 })
    expect(events).toEqual([])
  })

  it("emits a start before normalizing a missing executable failure", async () => {
    const events: unknown[] = []
    const workdir = workspace()
    const argv = [join(workdir, "missing-executable")]
    const result = await new LocalExecutionRuntime().run({
      workdir, argv, emit: async (...event) => { events.push(event) },
    })
    expect(events).toEqual([["command/started", "exec:run", { command: argv }]])
    expect(result).toEqual({ ok: false, stdout: "", stderr: "", exit_code: 1 })
  })

  it("terminates a timed-out child and returns failure", async () => {
    const result = await new LocalExecutionRuntime().run({
      workdir: workspace(), timeout_s: 0.05,
      argv: [process.execPath, "-e", "setInterval(() => {}, 1000)"],
    })
    expect(result.ok).toBe(false)
    expect(result.exit_code).toBe(1)
  })
})
