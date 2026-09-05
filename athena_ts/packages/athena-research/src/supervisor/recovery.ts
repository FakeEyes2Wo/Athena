/** Reconcile durable plans against canonical experiment history. */

import type { ResearchTree } from "@athena/core"
import type { PlanState } from "../contracts.js"
import { parseResearchState, type ResearchState } from "./state.js"

const TERMINAL = new Set(["SUCCEEDED", "FAILED", "CANCELLED"])

/** Drop settled/orphan plans; retain unfinished plans with their frozen context. */
export function reconcilePlans(
  state: ResearchState,
  tree: ResearchTree
): ResearchState {
  const baselineSettled = tree.experiments("baseline").some((experiment) =>
    TERMINAL.has(experiment.status)
  )
  const plans: Record<string, PlanState> = {}
  for (const [planId, plan] of Object.entries(state.plans)) {
    if (plan.kind === "SEARCH") {
      const experimentId = tree.experimentForHypothesis(planId)
      // A missing canonical experiment is an orphan, not a resumable plan.
      if (experimentId === null || TERMINAL.has(tree.getExperiment(experimentId).status)) continue
    } else if (plan.kind === "PREPARE" && baselineSettled) {
      continue
    } else if (plan.kind === "VALIDATE" && state.validation !== null) {
      continue
    }
    plans[planId] = plan
  }
  return parseResearchState({ ...state, plans })
}
