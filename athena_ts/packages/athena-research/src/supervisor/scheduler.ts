/**
 * 滚动 SEARCH Plan 槽位的纯确定性调度（移植 ``research/supervisor/scheduler.py``）。
 */

import type { Hypothesis, ResearchTree } from "@athena/core"

import { EloPolicy, queueOrder, type HypothesisPolicy, type Outcome } from "./policy.js"
import type { ResearchState } from "./state.js"

const TERMINAL = new Set(["SUCCEEDED", "FAILED", "CANCELLED"])

/** 填充一个 SEARCH 槽位时发出的一种调度动作。 */
export const ScheduleKind = {
  RESUME: "RESUME",
  START_NEW: "START_NEW",
  START_NEXT_HYPOTHESIS: "START_NEXT_HYPOTHESIS",
  GENERATE: "GENERATE",
} as const
export type ScheduleKind = (typeof ScheduleKind)[keyof typeof ScheduleKind]

/** 一个确定性的槽位填充动作。 */
export class ScheduleAction {
  constructor(
    public readonly kind: ScheduleKind,
    public readonly planId: string | null = null,
    public readonly hypothesisId: string | null = null,
    public readonly count: number = 0
  ) {}

  static resume(planId: string): ScheduleAction {
    return new ScheduleAction(ScheduleKind.RESUME, planId)
  }

  static startNew(hypothesisId: string): ScheduleAction {
    return new ScheduleAction(ScheduleKind.START_NEW, null, hypothesisId)
  }

  static startNextHypothesis(hypothesisId: string): ScheduleAction {
    return new ScheduleAction(ScheduleKind.START_NEXT_HYPOTHESIS, null, hypothesisId)
  }

  static generate(count: number): ScheduleAction {
    return new ScheduleAction(ScheduleKind.GENERATE, null, null, count)
  }
}

/** 已创建的 SEARCH 尝试数：终态搜索实验 + 活跃 SEARCH Plan。 */
export function countSearchAttempts(state: ResearchState, tree: ResearchTree): number {
  const settled = tree.experiments("search").filter((experiment) => TERMINAL.has(experiment.status)).length
  const active = Object.values(state.plans).filter((plan) => plan.kind === "SEARCH").length
  return settled + active
}

function isReady(turnsUsed: number, turnLimit: number | null): boolean {
  return turnLimit === null || turnsUsed < turnLimit
}

export interface NextActionsOptions {
  humanNext?: string | null
  manual?: boolean
}

/** 按固定确定性顺序填充 SEARCH 并发槽位。 */
export class Scheduler {
  private policy: HypothesisPolicy

  constructor(policy: HypothesisPolicy | null = null) {
    this.policy = policy ?? new EloPolicy()
  }

  seed(parent: Hypothesis | null): number {
    return this.policy.seed(parent)
  }

  settle(referencePriority: number, outcome: Outcome): number {
    return this.policy.settle(referencePriority, outcome)
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
        isReady(plan.turns_used, plan.turn_limit)
      ) {
        actions.push(ScheduleAction.resume(planId))
        freeSlots -= 1
      }
    }

    let createBudget = state.search_limit - countSearchAttempts(state, tree)
    if (createBudget > 0 && freeSlots > 0) {
      const humanNext = opts.humanNext ?? null
      const manual = opts.manual ?? false

      // 2. 用户单次点名的下一个假设，绕过策略队列但不改优先级
      if (humanNext !== null) {
        actions.push(ScheduleAction.startNextHypothesis(humanNext))
        freeSlots -= 1
        createBudget -= 1
      }

      if (manual) {
        // 手动模式：跳过按优先级自动出队，仅当无待选假设时生成候选。
        if (tree.pendingHypotheses().length === 0 && freeSlots > 0 && createBudget > 0) {
          actions.push(ScheduleAction.generate(Math.min(freeSlots, createBudget)))
        }
      } else {
        // 3. 策略优先级降序、FIFO 顺序升序的新假设队列
        for (const hypothesis of this.queued(tree, state, humanNext)) {
          if (freeSlots === 0 || createBudget === 0) break
          actions.push(ScheduleAction.startNew(hypothesis.id!))
          freeSlots -= 1
          createBudget -= 1
        }
        // 4. 剩余空位全部请求生成新假设
        if (freeSlots > 0 && createBudget > 0) {
          actions.push(ScheduleAction.generate(Math.min(freeSlots, createBudget)))
        }
      }
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
    return candidates.sort((a, b) => {
      const [pa, oa] = queueOrder(this.policy.priority(a, tree), a.order)
      const [pb, ob] = queueOrder(this.policy.priority(b, tree), b.order)
      if (pa !== pb) return pa - pb
      return oa - ob
    })
  }
}
