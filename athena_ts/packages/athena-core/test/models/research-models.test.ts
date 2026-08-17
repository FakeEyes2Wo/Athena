import { describe, expect, it } from "vitest"
import { ComparisonVerdictSchema, EvalResultSchema, ExperimentPlanSchema, HypothesisSchema } from "../../src/models/research-models.js"
import { MetricSpecSchema, TaskMetaDataSchema } from "../../src/models/research-data-models.js"

describe("domain models retain validation and serialization", () => {
  it("hypothesis, plan, eval, verdict", () => {
    const hypothesis = HypothesisSchema.parse({
      statement: "Use stronger regularization",
      intervention: "Increase weight decay",
      expected_effect: "Improve validation accuracy",
    })
    const plan = ExperimentPlanSchema.parse({
      kind: "search",
      change: "Increase weight decay",
      run_config_ref: "artifact://run-config",
      budget: { trials: 1 },
      acceptance_rule: "Primary metric improves",
    })
    const evaluation = EvalResultSchema.parse({
      experiment_id: "experiment-1",
      primary: 0.8,
      per_sample: "artifact://samples",
    })
    const verdict = ComparisonVerdictSchema.parse({ winner: "candidate", p_value: 0.01 })
    expect(hypothesis.status).toBe("PROPOSED")
    expect(hypothesis.cost).toBe(0)
    expect(plan.kind).toBe("search")
    expect(evaluation.primary).toBe(0.8)
    expect(verdict.winner).toBe("candidate")
  })

  it("hypothesis status matches the Python contract including INCONCLUSIVE", () => {
    const hypothesis = HypothesisSchema.parse({
      statement: "claim",
      intervention: "change",
      expected_effect: "improve",
      status: "INCONCLUSIVE",
      cost: 0.25,
    })
    expect(hypothesis.status).toBe("INCONCLUSIVE")
    expect(hypothesis.cost).toBe(0.25)
  })

  it("evaluation protocol models live in research-data-models", () => {
    const metric = MetricSpecSchema.parse({ name: "accuracy", direction: "maximize" })
    const meta = TaskMetaDataSchema.parse({
      task_type: "classification",
      data_type: "tabular",
      target_vars: ["y"],
      primary_metric: metric,
    })
    expect(meta.primary_metric.direction).toBe("maximize")
  })
})
