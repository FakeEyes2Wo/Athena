/**
 * 可替换的假设调度策略边界（移植 ``research/supervisor/policy.py``）。
 */

import type { Hypothesis } from "@athena/core"
import type { ResearchTree } from "@athena/core"

/** 候选相对其冻结参照的可信结果。 */
export const Outcome = {
  WIN: "WIN",
  DRAW: "DRAW",
  LOSS: "LOSS",
} as const
export type Outcome = (typeof Outcome)[keyof typeof Outcome]

/** Supervisor 调度器使用的窄策略。 */
export interface HypothesisPolicy {
  seed(parent: Hypothesis | null): number
  priority(hypothesis: Hypothesis, tree: ResearchTree): number
  settle(referencePriority: number, outcome: Outcome): number
}

/**
 * 单侧 Elo 更新，固定 0.5 期望得分。
 *
 * TODO(search-policy): 有足够真实搜索轨迹后，用 evidence-aware 调度策略替换
 * EloPolicy；Supervisor 只依赖 HypothesisPolicy。
 */
export class EloPolicy implements HypothesisPolicy {
  static readonly ROOT_PRIORITY = 1000.0

  private k: number

  constructor(k: number = 32.0) {
    if (!Number.isFinite(k) || k <= 0) {
      throw new Error("Elo k must be a positive finite number")
    }
    this.k = k
  }

  seed(parent: Hypothesis | null): number {
    return parent === null ? EloPolicy.ROOT_PRIORITY : parent.priority
  }

  priority(hypothesis: Hypothesis, _tree: ResearchTree): number {
    return hypothesis.priority
  }

  settle(referencePriority: number, outcome: Outcome): number {
    if (!Number.isFinite(referencePriority)) {
      throw new Error("reference priority must be finite")
    }
    const score =
      outcome === Outcome.WIN ? 1.0 : outcome === Outcome.DRAW ? 0.5 : 0.0
    return referencePriority + this.k * (score - 0.5)
  }
}

/** 优先级降序、FIFO order 升序的排序键。 */
export function queueOrder(priority: number, order: number | null): [number, number] {
  if (!Number.isFinite(priority)) {
    throw new Error("hypothesis priority must be finite")
  }
  if (order === null) {
    throw new Error("hypothesis order must be assigned before scheduling")
  }
  return [-priority, order]
}
