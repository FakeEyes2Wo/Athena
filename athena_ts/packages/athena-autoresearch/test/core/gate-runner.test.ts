import { describe, expect, it } from "vitest"
import { GateRunner, PassGate, FailGate } from "../../src/index.js"

describe("GateRunner", () => {
  it("runs registered gates in order", async () => {
    const runner = new GateRunner()
    runner.register(new PassGate())
    runner.register(new FailGate())
    const results = await runner.run(["always_pass", "always_fail"], {}, { status: "COMPLETED" })
    expect(results.map((r) => r.pass)).toEqual([true, false])
  })

  it("throws on unknown gate", async () => {
    const runner = new GateRunner()
    await expect(runner.run(["missing"], {}, { status: "COMPLETED" })).rejects.toThrow(
      /unknown gate/,
    )
  })
})
