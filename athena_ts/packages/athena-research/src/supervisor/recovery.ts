/**
 * 崩溃对账（移植 ``research/supervisor/recovery.py``）。
 */

import type { ResearchTree } from "@athena/core"

import type { PlanState } from "./plans.js"
import { ResearchState } from "./state.js"

const TERMINAL = new Set(["SUCCEEDED", "FAILED", "CANCELLED"])

function hasFinalBaseline(tree: ResearchTree): boolean {
  return tree.experiments("baseline").some((experiment) => TERMINAL.has(experiment.status))
}

function isSettled(
  planId: string,
  plan: PlanState,
  tree: ResearchTree,
  hasFinalBaselineValue: boolean,
  validation: Record<string, unknown> | null
): boolean {
  if (plan.kind === "SEARCH") {
    const experimentId = tree.experimentForHypothesis(planId)
    if (experimentId === null) return true
    return TERMINAL.has(tree.getExperiment(experimentId).status)
  }
  if (plan.kind === "PREPARE") return hasFinalBaselineValue
  if (plan.kind === "VALIDATE") return validation !== null
  return false
}

export interface ReconcileOptions {
  workspaceExists(planId: string): boolean
  artifactExists(ref: string): boolean
}

/** 对账未完成 Plan，不重建冻结的执行上下文。 */
export class Recovery {
  /** 移除已了结 Plan，并把缺失恢复前置条件暴露为 waiting。 */
  static reconcile(
    state: ResearchState,
    tree: ResearchTree,
    opts: ReconcileOptions
  ): ResearchState {
    const hasFinalBaselineValue = hasFinalBaseline(tree)
    const plans: Record<string, PlanState> = {}
    let waiting = false
    for (const [planId, plan] of Object.entries(state.plans)) {
      if (isSettled(planId, plan, tree, hasFinalBaselineValue, state.validation)) {
        continue
      }
      if (!opts.artifactExists(plan.context_ref) || !opts.workspaceExists(planId)) {
        waiting = true
      }
      plans[planId] = plan
    }
    return new ResearchState({
      status: waiting ? "WAITING" : state.status,
      phase: state.phase,
      search_limit: state.search_limit,
      concurrency: state.concurrency,
      manual_mode: state.manual_mode,
      plans,
      validation: state.validation,
      eda_dir: state.eda_dir,
    })
  }
}
