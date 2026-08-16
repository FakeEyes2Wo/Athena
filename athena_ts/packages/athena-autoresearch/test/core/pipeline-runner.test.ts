import { describe, expect, it } from "vitest"
import {
  AutoResearchStateStore,
  PipelineRunner,
  createDefaultGateRunner,
  createDefaultStages,
  mergeRunSpecs,
} from "../../src/index.js"
import { makeContext, makeTmpDir } from "../_support.js"

describe("PipelineRunner", () => {
  it("runs the default linear stage pipeline to COMPLETED", async () => {
    const ctx = makeContext()
    ctx.pool = {
      countQueued: () => 1,
      countByOrigin: () => 0,
    }
    const runner = new PipelineRunner(
      createDefaultStages(),
      new AutoResearchStateStore(`${makeTmpDir()}/state.json`),
      createDefaultGateRunner(),
    )
    const result = await runner.run(ctx)
    expect(result.status).toBe("COMPLETED")
    expect(ctx.state.phase).toBe("packaging")
    expect(ctx.console.compileOutputs.length).toBeGreaterThan(0)
  })

  it("stops with WAITING when no stage can enter", async () => {
    const ctx = makeContext()
    ctx.pool = { countQueued: () => 0, countByOrigin: () => 0 }
    const runner = new PipelineRunner(
      createDefaultStages(),
      new AutoResearchStateStore(`${makeTmpDir()}/state.json`),
      createDefaultGateRunner(),
    )
    const result = await runner.run(ctx)
    expect(result.status).toBe("WAITING")
    expect(ctx.state.stopped_by).toBe("NO_ENTERABLE_STAGE")
  })

  it("marks WAITING when a gate fails", async () => {
    const ctx = makeContext()
    ctx.pool = { countQueued: () => 1, countByOrigin: () => 0 }
    ctx.spec = mergeRunSpecs(ctx.spec, {
      stages: [{ id: "intake", provider: "intake", gates: ["always_fail"] }],
    })
    const runner = new PipelineRunner(
      createDefaultStages(),
      new AutoResearchStateStore(`${makeTmpDir()}/state.json`),
      createDefaultGateRunner(),
    )
    const result = await runner.run(ctx)
    expect(result.status).toBe("WAITING")
    expect(ctx.state.stopped_by).toBe("GATE_FAILED")
    expect(result.gateResults.some((gate) => !gate.pass)).toBe(true)
  })
})
