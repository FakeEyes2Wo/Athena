import { mkdtempSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, describe, expect, it } from "vitest"
import { Context } from "cordis"
import { HypothesisSchema } from "@athena/core"
import { researchPlugin } from "../../athena-dsh/src/index.js"
import {
  AutoResearchRuntime,
  PooledHypothesisSchema,
  autoresearchPlugin,
} from "../src/index.js"

let tmp: string
afterEach(() => {
  if (tmp) rmSync(tmp, { recursive: true, force: true })
})

describe("DSH headless integration", () => {
  it("loads @athena/dsh + @athena/autoresearch and runs the full flow", async () => {
    tmp = mkdtempSync(join(tmpdir(), "autoresearch-dsh-"))

    const ctx = new Context()
    await ctx.plugin(researchPlugin({ projectRoot: tmp, searchLimit: 2, concurrency: 1 }))
    await ctx.plugin(autoresearchPlugin({ projectRoot: tmp }))

    // 两个插件提供的服务均可见。
    expect(ctx.researchTree).toBeTruthy()
    expect(ctx.researchState).toBeTruthy()
    expect(ctx.hypothesisPool).toBeTruthy()
    expect(ctx.autoResearchSpec).toBeTruthy()
    expect(ctx.autoResearchStateStore).toBeTruthy()
    expect(ctx.autoResearchProviders).toBeTruthy()
    expect(ctx.autoResearchStages).toBeTruthy()
    expect(ctx.autoResearchGateRunner).toBeTruthy()

    // 只注入一个通用探索假设，不写任何数据集特异性逻辑。
    const now = new Date().toISOString()
    ctx.hypothesisPool.upsert(
      PooledHypothesisSchema.parse({
        pool_id: "h_dsh_headless",
        hypothesis: HypothesisSchema.parse({
          id: "h_dsh_headless",
          statement: "Explore the provided data directory and establish a baseline.",
          intervention: "Run a generic first exploration of the provided data directory.",
          expected_effect: "Produce an initial result to seed the research loop.",
        }),
        pool_status: "QUEUED",
        origin: "manual",
        created_at: now,
        updated_at: now,
      }),
    )

    // 用 DSH 上下文中注册的同一批服务运行完整阶段机。
    const runtime = new AutoResearchRuntime({
      projectRoot: tmp,
      runSpec: { run_id: "ar_dsh_headless" },
      tree: ctx.researchTree,
      pool: ctx.hypothesisPool,
      artifacts: ctx.researchStore,
      providers: ctx.autoResearchProviders,
      stages: ctx.autoResearchStages,
      gateRunner: ctx.autoResearchGateRunner,
      stateStore: ctx.autoResearchStateStore,
    })

    const result = await runtime.start()
    expect(result.status).toBe("COMPLETED")
    expect(runtime.currentState.phase).toBe("packaging")
  })
})
