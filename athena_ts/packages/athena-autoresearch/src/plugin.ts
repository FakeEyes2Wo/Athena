import { join, resolve } from "node:path"
import { HypothesisSchema, LocalArtifactStore, ResearchTree } from "@athena/core"
import type { Context } from "@deepseek-ai/cordis"
import {
  createDefaultGateRunner,
  createDefaultProviders,
  createDefaultStages,
} from "./bootstrap.js"
import { HypothesisPool } from "./core/hypothesis-pool.js"
import { PooledHypothesisSchema } from "./schemas/pool.js"
import { AutoResearchStateStore } from "./schemas/state.js"
import { DEFAULT_RUN_SPEC, mergeRunSpecs, type RunSpec, type RunSpecOverride } from "./schemas/run-spec.js"
import { AutoResearchRuntime } from "./runtime.js"

export interface AutoResearchPluginConfig {
  projectRoot?: string
  runSpec?: RunSpecOverride
}

declare module "@deepseek-ai/cordis" {
  interface Context {
    autoResearchSpec: RunSpec
    autoResearchStateStore: AutoResearchStateStore
    hypothesisPool: HypothesisPool
    autoResearchGateRunner: ReturnType<typeof createDefaultGateRunner>
    autoResearchProviders: ReturnType<typeof createDefaultProviders>
    autoResearchStages: ReturnType<typeof createDefaultStages>
    autoResearchRuntime: AutoResearchRuntime
  }
}

interface ToolRegistry {
  register(def: unknown): void
}

function objectSchema(properties: Record<string, unknown>, required: string[]): Record<string, unknown> {
  return { type: "object", properties, required, additionalProperties: false }
}

function renderJson(_args: unknown, value: unknown): unknown {
  return [{ type: "text", text: JSON.stringify(value) }]
}

function seedIfEmpty(pool: HypothesisPool): void {
  if (pool.countQueued() > 0) return
  const now = new Date().toISOString()
  pool.upsert(
    PooledHypothesisSchema.parse({
      pool_id: `h_${Date.now().toString(36)}`,
      hypothesis: HypothesisSchema.parse({
        id: null,
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
}

export function autoresearchPlugin(config: AutoResearchPluginConfig = {}) {
  return (ctx: Context): void => {
    const projectRoot = resolve(config.projectRoot ?? ".")
    const autoResearchDir = join(projectRoot, ".athena", "autoresearch")
    const spec = mergeRunSpecs(DEFAULT_RUN_SPEC, config.runSpec ?? {})

    const stateStore = new AutoResearchStateStore(join(autoResearchDir, "state.json"))
    const pool = new HypothesisPool(join(autoResearchDir, "hypothesis_pool.json"))
    pool.load()

    const providers = createDefaultProviders()
    const stages = createDefaultStages()
    const gateRunner = createDefaultGateRunner()

    const getCtx = ctx as unknown as { get(name: string, strict?: boolean): unknown }
    const researchTree = (getCtx.get("researchTree", false) as ResearchTree | undefined) ?? new ResearchTree()
    const researchStore =
      (getCtx.get("researchStore", false) as LocalArtifactStore | undefined) ??
      new LocalArtifactStore(join(autoResearchDir, "artifacts"))

    const runtime = new AutoResearchRuntime({
      projectRoot,
      runSpec: config.runSpec ?? { run_id: `ar_${Date.now().toString(36)}` },
      tree: researchTree,
      pool,
      artifacts: researchStore,
      providers,
      stages,
      gateRunner,
      stateStore,
    })

    ctx.provide("autoResearchSpec", spec)
    ctx.provide("autoResearchStateStore", stateStore)
    ctx.provide("hypothesisPool", pool)
    ctx.provide("autoResearchGateRunner", gateRunner)
    ctx.provide("autoResearchProviders", providers)
    ctx.provide("autoResearchStages", stages)
    ctx.provide("autoResearchRuntime", runtime)

    const tools = getCtx.get("tools", false) as ToolRegistry | undefined
    if (!tools) return
    tools.register({
      name: "autoresearch_status",
      description: "Read the current AutoResearch run status, phase, budgets, and SOTA experiment.",
      parameters: objectSchema({}, []),
      output: { schema: { type: "object" }, render: renderJson },
      async execute() {
        return {
          run_id: runtime.currentState.run_id,
          phase: runtime.currentState.phase,
          status: runtime.currentState.status,
          stopped_by: runtime.currentState.stopped_by ?? null,
          sota_experiment_id: researchTree.bestExperimentId(),
          queued_hypotheses: pool.countQueued(),
        }
      },
    })

    tools.register({
      name: "autoresearch_run",
      description: "Start or resume the full AutoResearch flow (intake → ideation → experiment → writing → refinement → packaging).",
      parameters: objectSchema({}, []),
      output: { schema: { type: "object" }, render: renderJson },
      async execute() {
        seedIfEmpty(pool)
        const result = await runtime.start()
        return {
          run_id: runtime.currentState.run_id,
          phase: runtime.currentState.phase,
          status: result.status,
          stopped_by: runtime.currentState.stopped_by ?? null,
          sota_experiment_id: researchTree.bestExperimentId(),
        }
      },
    })
  }
}
