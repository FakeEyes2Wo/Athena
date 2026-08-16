import { mkdtempSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { LocalArtifactStore, ResearchTree } from "@athena/core"
import {
  AutoResearchStateStore,
  BudgetService,
  emptyPool,
  MemoryConsolePort,
  ProviderRegistry,
  createDefaultGateRunner,
  createDefaultProviders,
  createDefaultStages,
  DEFAULT_RUN_SPEC,
} from "../src/index.js"
import type { StageContext } from "../src/index.js"

export function makeTmpDir(): string {
  return mkdtempSync(join(tmpdir(), "autoresearch-"))
}

export function makeContext(overrides: Partial<StageContext> = {}): StageContext {
  const runId = "ar_test"
  const stateStore = new AutoResearchStateStore(join(makeTmpDir(), "state.json"))
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
  const projectRoot = makeTmpDir()
  const ctx: StageContext = {
    runId,
    projectRoot,
    spec: DEFAULT_RUN_SPEC,
    state,
    pool: emptyPool,
    tree: new ResearchTree(),
    artifacts: new LocalArtifactStore(join(makeTmpDir(), "artifacts")),
    providers: createDefaultProviders(),
    budgets: new BudgetService(DEFAULT_RUN_SPEC.budgets, Date.now() - 1000),
    console: new MemoryConsolePort(),
    ...overrides,
  }
  return ctx
}

export { createDefaultGateRunner }
