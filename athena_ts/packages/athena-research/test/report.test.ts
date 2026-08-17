import { describe, expect, it } from "vitest"
import {
  ExperimentPlanSchema,
  ExperimentSchema,
  HypothesisSchema,
  ResearchTree,
} from "@athena/core"
import { buildFinalReport } from "../src/report.js"

const REF = "sha256:" + "a".repeat(64)

function seededTree(): ResearchTree {
  const tree = new ResearchTree()
  const hypothesisId = tree.addHypothesis(
    HypothesisSchema.parse({
      id: "baseline",
      statement: "trusted baseline",
      intervention: "baseline",
      expected_effect: "reference",
    })
  )
  tree.addExperiment(
    "exp_baseline",
    ExperimentSchema.parse({
      hypothesis_id: hypothesisId,
      commit: "c0",
      plan: ExperimentPlanSchema.parse({
        kind: "baseline",
        change: "baseline",
        run_config_ref: REF,
        budget: {},
        acceptance_rule: "trusted score",
      }),
      gitwork: { path: "ws", branch: "main", base_commit: "c0" },
      status: "SUCCEEDED",
      eval: { experiment_id: "exp_baseline", primary: 0.8123, per_sample: REF },
    })
  )
  tree.setSota("exp_baseline")
  return tree
}

describe("buildFinalReport", () => {
  it("renders SOTA, experiment records, and pending hypotheses deterministically", () => {
    const tree = seededTree()
    tree.addHypothesis(
      HypothesisSchema.parse({
        statement: "pending idea",
        intervention: "change",
        expected_effect: "improve",
        parent_id: "exp_baseline",
      })
    )

    const report = buildFinalReport(tree, null)

    expect(report).toContain("# Athena 研究报告")
    expect(report).toContain("SOTA 实验")
    expect(report).toContain("0.8123")
    expect(report).toContain("pending idea")
    expect(report).toContain("**exp_baseline** [SUCCEEDED] trusted baseline")
  })

  it("renders the VALIDATE section when a validation dict is provided", () => {
    const report = buildFinalReport(seededTree(), {
      final_test_score: 0.79,
      generalization_gap: 0.02,
      generalization_warning: true,
    })

    expect(report).toContain("## 验证结果")
    expect(report).toContain("最终测试分数")
    expect(report).toContain("泛化差距")
    expect(report).toContain("泛化警告")
  })
})
