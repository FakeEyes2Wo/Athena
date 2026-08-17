/**
 * Deterministic final research report builder（移植 ``research/report.py``）。
 *
 * 报告是 ``ResearchTree`` 与可选 VALIDATE 结果字典的纯函数，因此 Supervisor
 * （VALIDATE 完成后）与 DSH 的 ``research_report`` 工具渲染完全一致的内容。
 */

import type { ResearchTree } from "@athena/core"

function fmt(value: unknown, digits = 4): string {
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : value.toFixed(digits)
  }
  return String(value)
}

/** 把研究树与可选 VALIDATE 结果渲染为 Markdown。 */
export function buildFinalReport(
  tree: ResearchTree,
  validation: Record<string, unknown> | null = null
): string {
  const data = tree.toDict()
  const executed = new Set(Object.values(data.experiments).map((experiment) => experiment.hypothesis_id))
  const lines: string[] = ["# Athena 研究报告", ""]

  const sotaId = data.sota_id
  if (sotaId !== null) {
    const sotaExperiment = data.experiments[sotaId]
    if (sotaExperiment !== undefined) {
      const primary = sotaExperiment.eval?.primary ?? null
      const hypothesis = data.hypotheses[sotaExperiment.hypothesis_id]
      lines.push("## SOTA")
      lines.push(`- **SOTA 实验**: \`${sotaId}\``)
      if (primary !== null) lines.push(`- **最佳 primary**: ${fmt(primary)}`)
      if (hypothesis !== undefined) lines.push(`- **SOTA 假设**: ${hypothesis.statement}`)
      lines.push("")
    }
  }

  lines.push(`- 实验总数: ${Object.keys(data.experiments).length}`)
  lines.push(`- 假设总数: ${Object.keys(data.hypotheses).length}`)

  if (validation !== null) {
    const finalScore = validation["final_test_score"]
    const gap = validation["generalization_gap"]
    const warning = validation["generalization_warning"]
    const hasFinalScore = finalScore !== undefined && finalScore !== null
    const hasGap = gap !== undefined && gap !== null
    if (hasFinalScore || hasGap || warning === true) {
      lines.push("", "## 验证结果")
      if (hasFinalScore) lines.push(`- **最终测试分数**: ${fmt(finalScore)}`)
      if (hasGap) lines.push(`- **泛化差距**: ${fmt(gap)}`)
      if (warning === true) {
        lines.push(
          "- **泛化警告**: 测试集表现与训练/验证集差距过大，存在过拟合风险"
        )
      }
    }
  }

  // 只列出尚未执行的假设（已创建实验的假设不算待选，例如 baseline）。
  const pending = tree.pendingHypotheses().filter((hypothesis) => hypothesis.id === null || !executed.has(hypothesis.id))
  if (pending.length > 0) {
    lines.push("", "## 待选假设")
    for (const hypothesis of pending) lines.push(`- ${hypothesis.statement}`)
  }

  lines.push("", "## 实验记录")
  for (const [experimentId, experiment] of Object.entries(data.experiments)) {
    const hypothesis = data.hypotheses[experiment.hypothesis_id]
    const primary = experiment.eval?.primary ?? null
    const suffix = primary !== null ? ` — primary ${fmt(primary)}` : ""
    lines.push(
      `- **${experimentId}** [${experiment.status}] ${hypothesis?.statement ?? experiment.hypothesis_id}${suffix}`
    )
  }
  return lines.join("\n")
}
