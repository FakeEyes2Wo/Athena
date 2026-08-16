import { describe, expect, it } from "vitest"
import { HypothesisSchema } from "@athena/core"
import {
  AutoResearchRuntime,
  HypothesisPool,
  PooledHypothesisSchema,
} from "../src/index.js"
import { makeTmpDir } from "./_support.js"

function makePool(path: string): HypothesisPool {
  const pool = new HypothesisPool(path)
  const now = new Date(0).toISOString()
  pool.upsert(
    PooledHypothesisSchema.parse({
      pool_id: "h1",
      hypothesis: HypothesisSchema.parse({
        id: "h1",
        statement: "claim",
        intervention: "change",
        expected_effect: "improve",
      }),
      pool_status: "QUEUED",
      origin: "test",
      created_at: now,
      updated_at: now,
    }),
  )
  return pool
}

describe("AutoResearchRuntime", () => {
  it("runs the default preset with a queued hypothesis", async () => {
    const projectRoot = makeTmpDir()
    const runtime = new AutoResearchRuntime({
      projectRoot,
      pool: makePool(`${projectRoot}/pool.json`),
    })
    const result = await runtime.start()
    expect(result.status).toBe("COMPLETED")
    expect(runtime.currentState.phase).toBe("packaging")
  })

  it("parks in WAITING when no stage can enter", async () => {
    const runtime = new AutoResearchRuntime({ projectRoot: makeTmpDir() })
    const result = await runtime.start()
    expect(result.status).toBe("WAITING")
    expect(runtime.currentState.stopped_by).toBe("NO_ENTERABLE_STAGE")
  })
})
