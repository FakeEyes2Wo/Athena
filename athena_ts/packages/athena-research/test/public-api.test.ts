import { describe, expect, it } from "vitest"
import * as api from "../src/index.js"
import { FixedFlowSupervisor } from "../src/supervisor/supervisor.js"
import { LocalExecutionRuntime } from "../src/execution.js"

describe("research public surface", () => {
  it("removes duplicate Supervisor entrypoints", () => {
    expect(FixedFlowSupervisor.prototype).not.toHaveProperty("startValidation")
    expect(FixedFlowSupervisor.prototype).not.toHaveProperty("stop")
    expect(FixedFlowSupervisor.prototype.setPhaseDecision).toBeTypeOf("function")
    expect(FixedFlowSupervisor.prototype.requestStop).toBeTypeOf("function")
  })

  it("removes the unused standalone runtime and its private adapter chain", () => {
    for (const name of [
      "ResearchRuntime", "WorkerRunner", "zodOutputType", "PlanDecisionOutputType",
      "HypothesisBatchOutputType", "makeShellTool", "commandResultDict", "ExecutionContext", "decideSettlement",
      "BundleMetadata", "ScriptRunResult",
      "ValidationService", "generalizationGap", "generalizationWarning",
      "CandidateEvaluationSchema",
      "countSearchAttempts",
      "ResearchState",
      "planStateToJSON",
      "EventProjector", "OutputEventSchema", "StateEventSchema",
      "redact", "sanitizeTerminalText", "truncateMiddle",
    ]) expect(api).not.toHaveProperty(name)
  })

  it("retains canonical services composed by DSH", () => {
    expect(api.FixedFlowSupervisor).toBe(FixedFlowSupervisor)
    expect(api.LocalExecutionRuntime).toBe(LocalExecutionRuntime)
    expect(api.runPreparePlan).toBeTypeOf("function")
    expect(api.runValidationPlan).toBeTypeOf("function")
    expect(api.parseResearchState).toBeTypeOf("function")
    expect(api.loadResearchState).toBeTypeOf("function")
    expect(api.saveResearchState).toBeTypeOf("function")
  })
})
