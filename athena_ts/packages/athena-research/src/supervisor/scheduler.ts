/**
 * 滚动 SEARCH Plan 槽位的纯确定性调度（移植 ``research/supervisor/scheduler.py``）。
 */

import type { Hypothesis, ResearchTree } from "@athena/core"

import { EloPolicy, type HypothesisPolicy } from "./policy.js"
import { Selector } from "./ranker.js"
import type { ResearchState } from "./state.js"

const TERMINAL = new Set(["SUCCEEDED", "FAILED", "CANCELLED"])

/** 每个动作仅携带实际需要的数据；SEARCH plan ID 就是假设 ID。 */
export type ScheduleAction =
  | { readonly kind: "RESUME" | "START_NEW" | "START_NEXT_HYPOTHESIS"; readonly planId: string }
  | { readonly kind: "GENERATE"; readonly count: number }

/** 已创建的 SEARCH 尝试数：终态搜索实验 + 活跃 SEARCH Plan。 */
export function countSearchAttempts(state: ResearchState, tree: ResearchTree): number {
  const settled = tree.experiments("search").filter((experiment) => TERMINAL.has(experiment.status)).length
  const active = Object.values(state.plans).filter((plan) => plan.kind === "SEARCH").length
  return settled + active
}

export interface NextActionsOptions {
  humanNext?: string | null
  manual?: boolean
}

/** 按固定确定性顺序填充 SEARCH 并发槽位。 */
export class Scheduler {
  private readonly selector: Selector

  constructor(public readonly policy: HypothesisPolicy = new EloPolicy()) {
    this.selector = new Selector(policy)
  }

  /** 投影槽位填充动作，不修改 state/tree/policy。 */
  nextActions(
    state: ResearchState,
    tree: ResearchTree,
    runningIds: Iterable<string>,
    opts: NextActionsOptions = {}
  ): ScheduleAction[] {
    if (state.phase !== "SEARCH") return []

    const running = new Set(runningIds)
    let freeSlots = Math.max(0, state.concurrency - running.size)
    const actions: ScheduleAction[] = []

    // 1. 先恢复已就绪且未运行的现有 Plan
    for (const [planId, plan] of Object.entries(state.plans)) {
      if (freeSlots === 0) break
      if (
        plan.kind === "SEARCH" &&
        !running.has(planId) &&
        (plan.turn_limit === null || plan.turns_used < plan.turn_limit)
      ) {
        actions.push({ kind: "RESUME", planId })
        freeSlots -= 1
      }
    }

    let createBudget = state.search_limit - countSearchAttempts(state, tree)
    if (createBudget <= 0 || freeSlots === 0) return actions
    const humanNext = opts.humanNext ?? null

    // 2. 用户单次点名的下一个假设，绕过策略队列但不改优先级
    if (humanNext !== null) {
      actions.push({ kind: "START_NEXT_HYPOTHESIS", planId: humanNext })
      freeSlots -= 1
      createBudget -= 1
    }

    // 手动模式保留待选假设，只在队列为空时生成候选。
    if (opts.manual && tree.pendingHypotheses().length > 0) return actions
    if (!opts.manual) {
      // 3. 按选择器评分排序，FIFO 打破同分；本轮近似假设去重。
      for (const hypothesis of this.queued(tree, state, humanNext)) {
        if (freeSlots === 0 || createBudget === 0) break
        actions.push({ kind: "START_NEW", planId: hypothesis.id! })
        freeSlots -= 1
        createBudget -= 1
      }
    }
    // 4. 在剩余预算内为未填充槽位生成新假设。
    if (freeSlots > 0 && createBudget > 0) {
      actions.push({ kind: "GENERATE", count: Math.min(freeSlots, createBudget) })
    }
    return actions
  }

  private queued(
    tree: ResearchTree,
    state: ResearchState,
    humanNext: string | null
  ): Hypothesis[] {
    const candidates = tree.pendingHypotheses().filter(
      (hypothesis) =>
        hypothesis.id !== null &&
        !(hypothesis.id in state.plans) &&
        hypothesis.id !== humanNext &&
        tree.experimentForHypothesis(hypothesis.id) === null
    )
    // 去重只作用于本轮选择：同一轮不并行启动近似重复的新假设，但所有假设
    // 仍保留在 graph（PROPOSED）中，后续轮次仍可重新排名并选中。
    const distinct = this.selector.deduplicate(candidates, [])
    return this.selector.rank(tree, distinct)
  }
}
