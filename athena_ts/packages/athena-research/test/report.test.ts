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
  it("renders an empty tree without optional sections", () => {
    expect(buildFinalReport(new ResearchTree())).toBe([
      "# Athena 研究报告", "", "- 实验总数: 0", "- 假设总数: 0", "", "## 实验记录",
    ].join("\n"))
  })

  it("omits an empty validation payload and preserves zero values", () => {
    const tree = new ResearchTree()
    expect(buildFinalReport(tree, {
      final_test_score: null, generalization_gap: null, generalization_warning: false,
    })).toBe(buildFinalReport(tree))
    expect(buildFinalReport(tree, { final_test_score: 0, generalization_gap: 0 })).toContain(
      "## 验证结果\n- **最终测试分数**: 0\n- **泛化差距**: 0",
    )
  })

  it("formats fractional values to four places and accepts legacy text values", () => {
    const report = buildFinalReport(new ResearchTree(), {
      final_test_score: 0.123456, generalization_gap: "not available",
    })
    expect(report).toContain("最终测试分数**: 0.1235")
    expect(report).toContain("泛化差距**: not available")
  })

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

    const before = tree.toDict()
    const report = buildFinalReport(tree, null)

    expect(report).toContain("# Athena 研究报告")
    expect(report).toContain("SOTA 实验")
    expect(report).toContain("0.8123")
    expect(report).toContain("pending idea")
    expect(report).toContain("**exp_baseline** [SUCCEEDED] trusted baseline")
    expect(report.split("## 待选假设")[1]!.split("## 实验记录")[0]).not.toContain("trusted baseline")
    expect(buildFinalReport(tree, null)).toBe(report)
    expect(tree.toDict()).toEqual(before)
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
