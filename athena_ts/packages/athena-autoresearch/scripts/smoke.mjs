import { mkdtempSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { strict as assert } from "node:assert"
import { LocalArtifactStore, ResearchTree } from "@athena/core"
import {
  AutoResearchStateStore,
  BudgetService,
  GateRunner,
  MemoryConsolePort,
  NativeSvgProvider,
  PipelineRunner,
  ProviderRegistry,
  createDefaultGateRunner,
  createDefaultProviders,
  createDefaultStages,
  parseRunSpec,
  mergeRunSpecs,
  parseDurationMs,
  DEFAULT_RUN_SPEC,
} from "../dist/index.js"

function makePool() {
  let queued = 0
  return {
    countQueued: () => queued,
    countByOrigin: () => 0,
    upsert: () => { queued = 1 },
  }
}

function tmp(prefix = "ar-smoke-") {
  return mkdtempSync(join(tmpdir(), prefix))
}

function makeContext() {
  const runId = "ar_smoke"
  const stateStore = new AutoResearchStateStore(join(tmp(), "state.json"))
  const state = stateStore.create({
    run_id: runId,
    phase: "intake",
    stage_id: "intake",
    status: "RUNNING",
    paper_spec: DEFAULT_RUN_SPEC.paper_spec,
    budgets: { ...DEFAULT_RUN_SPEC.budgets },
    started_at: new Date(0).toISOString(),
    updated_at: new Date(0).toISOString(),
    deadline: new Date(Date.now() + 3_600_000).toISOString(),
  })
  return {
    runId,
    projectRoot: tmp(),
    spec: DEFAULT_RUN_SPEC,
    state,
    pool: makePool(),
    tree: new ResearchTree(),
    artifacts: new LocalArtifactStore(join(tmp(), "artifacts")),
    providers: createDefaultProviders(),
    budgets: new BudgetService(DEFAULT_RUN_SPEC.budgets, Date.now() - 1000),
    console: new MemoryConsolePort(),
    stateStore,
  }
}

// schemas
const parsed = parseRunSpec(DEFAULT_RUN_SPEC)
assert.equal(parsed.stages.length, 6)
const merged = mergeRunSpecs(DEFAULT_RUN_SPEC, { budgets: { max_ideas: 5 }, paper_spec: { title: "T" } })
assert.equal(merged.budgets.max_ideas, 5)
assert.equal(merged.paper_spec.title, "T")

// providers
const registry = new ProviderRegistry()
const p = { id: "p1", version: "1", capabilities: ["a"] }
registry.register(p)
assert.equal(registry.get("p1"), p)
assert.throws(() => registry.register(p), /duplicate provider/)

// budgets
assert.equal(parseDurationMs("PT1H"), 3_600_000)
assert.throws(() => parseDurationMs("1H"), /unsupported duration/)

// gates
const gates = new GateRunner()
gates.register({ id: "g", retryable: false, check: async () => ({ pass: true, itemScores: [], feedback: [] }) })
const gateResults = await gates.run(["g"], {}, { status: "COMPLETED" })
assert.equal(gateResults[0].pass, true)

// native svg
const svgCtx = makeContext()
const svg = new NativeSvgProvider()
const figure = await svg.generate(svgCtx, {
  runId: svgCtx.runId,
  name: "architecture",
  kind: "architecture",
  title: "Test Architecture",
  context: { sotaPath: ["baseline", "method"], evidence: {}, draftSectionRefs: [] },
  constraints: { format: "svg", maxWidthPx: 800, maxHeightPx: 600, palette: ["#ffffff", "#333333", "#111111"], noExternalAssets: true },
})
assert.equal(figure.selfCheck.syntaxOk, true)
const svgText = await svgCtx.artifacts.getText(figure.svgRef)
assert.match(svgText, /<svg/)
assert.match(svgText, /Test Architecture/)

// pipeline full run
const ctx = makeContext()
ctx.pool.upsert({ poolId: "h1", origin: "ideation", status: "QUEUED" })
const runner = new PipelineRunner(createDefaultStages(), ctx.stateStore, createDefaultGateRunner())
const result = await runner.run(ctx)
console.log("result", result.status, "stopped_by", ctx.state.stopped_by, "phase", ctx.state.phase, "lines", ctx.console.lines)
assert.equal(result.status, "COMPLETED")
assert.equal(ctx.state.phase, "packaging")
assert.ok(ctx.console.compileOutputs.length >= 1)

console.log("smoke ok")
