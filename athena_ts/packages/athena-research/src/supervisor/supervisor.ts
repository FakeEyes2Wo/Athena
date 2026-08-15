/**
 * 固定流程 Supervisor：单写者编排 PREPARE → SEARCH → VALIDATE（移植 supervisor.py 的
 * 核心 SEARCH 滚动调度 + 阶段迁移，LLM worker 经注入函数接入，不依赖 M4 AgentRuntime）。
 */

import {
  EvalResultSchema,
  ExperimentPlanSchema,
  ExperimentSchema,
  HypothesisSchema,
  type ArtifactRef,
  type ArtifactStore,
  type Hypothesis,
  type ResearchTree,
} from "@athena/core"
import type { GitWorkBranch, GitWorkspace } from "@athena/core"

import { ValidationResultSchema, type ValidationResult } from "../contracts.js"
import { loadBest, type PlanTurnResult } from "./experiment.js"
import { Outcome, type Outcome as OutcomeType } from "./policy.js"
import { PlanInputSchema, PlanStateSchema, type PlanDecision, type PlanState } from "./plans.js"
import { PrepareResultSchema, type PrepareResult } from "./prepare.js"
import { Recovery } from "./recovery.js"
import { ScheduleKind, Scheduler, countSearchAttempts } from "./scheduler.js"
import { ResearchState } from "./state.js"

export type PlanTurn = (planId: string, state: PlanState) => Promise<PlanTurnResult>
export type PlanAgentTurn = (planId: string, state: PlanState) => Promise<PlanDecision | null>
export type IdeatorTurn = (count: number) => Promise<Hypothesis[]>
export type PreparePhase = () => Promise<PrepareResult>
export type ValidationPhase = (commit: string, metric: number) => Promise<ValidationResult>
export type Publish = (kind: "output" | "state", data: Record<string, unknown>) => Promise<void>

function compareMetric(
  candidate: number,
  reference: number,
  direction: "maximize" | "minimize",
  tolerance: number
): OutcomeType {
  const delta = direction === "maximize" ? candidate - reference : reference - candidate
  if (delta > tolerance) return Outcome.WIN
  if (delta < -tolerance) return Outcome.LOSS
  return Outcome.DRAW
}

export interface CompletedTurn {
  planId: string
  decision: PlanDecision | null
  result: PlanTurnResult | null
}

export interface SupervisorWorkers {
  runPlanTurn: PlanTurn
  runPlanAgentTurn: PlanAgentTurn
  runIdeatorTurn: IdeatorTurn
  runPreparePhase: PreparePhase
  runValidationPhase: ValidationPhase
  publish: Publish
}

export class FixedFlowSupervisor {
  state: ResearchState
  tree: ResearchTree
  private store: ArtifactStore
  private git: GitWorkspace
  private scheduler: Scheduler
  private recovery: Recovery
  private evaluatorRef: ArtifactRef | null
  private direction: "maximize" | "minimize"
  private tolerance: number
  private workers: SupervisorWorkers
  private branches: Record<string, GitWorkBranch> = {}
  private running: Map<string, Promise<CompletedTurn>> = new Map()
  private stopped = false
  private statePath: string
  private treePath: string

  constructor(opts: {
    projectRoot: string
    state: ResearchState
    tree: ResearchTree
    store: ArtifactStore
    git: GitWorkspace
    scheduler?: Scheduler | null
    recovery?: Recovery | null
    evaluatorRef?: ArtifactRef | null
    direction?: "maximize" | "minimize"
    tolerance?: number
    workers: SupervisorWorkers
  }) {
    this.state = opts.state
    this.tree = opts.tree
    this.store = opts.store
    this.git = opts.git
    this.scheduler = opts.scheduler ?? new Scheduler()
    this.recovery = opts.recovery ?? new Recovery()
    this.evaluatorRef = opts.evaluatorRef ?? null
    this.direction = opts.direction ?? "maximize"
    this.tolerance = opts.tolerance ?? 0.0
    this.workers = opts.workers
    this.statePath = `${opts.projectRoot}/.athena/state.json`
    this.treePath = `${opts.projectRoot}/.athena/research_tree.json`
  }

  get runningPlanIds(): string[] {
    return [...this.running.keys()]
  }

  private saveState(): void {
    this.state.save(this.statePath)
  }

  private async publishState(): Promise<void> {
    await this.workers.publish("state", { type: "state", ...this.state.toJSON() })
  }

  private async persistState(): Promise<void> {
    this.saveState()
    await this.publishState()
  }

  async start(): Promise<void> {
    this.stopped = false
    if (this.state.phase === "PREPARE") {
      await this.runPrepare()
    } else {
      await this.recover()
    }
    await this.continuePhase()
  }

  async continuePhase(): Promise<void> {
    if (this.state.phase === "SEARCH") {
      await this.runSearch()
      if (this.searchLimitReached() && this.state.status === "RUNNING") {
        this.state.status = "WAITING"
        await this.persistState()
      }
    }
    if (this.state.phase === "VALIDATE") {
      await this.runValidation()
    }
  }

  async runPrepare(): Promise<void> {
    const result = await this.workers.runPreparePhase()
    const hypothesisId = "baseline"
    const experimentId = "exp_baseline"
    if (this.tree.bestExperimentId() === null) {
      this.tree.addHypothesis(
        HypothesisSchema.parse({
          id: hypothesisId,
          statement: "trusted PREPARE baseline",
          intervention: "establish the baseline implementation",
          expected_effect: "provide the SEARCH reference metric",
        })
      )
      this.tree.addExperiment(
        experimentId,
        ExperimentSchema.parse({
          hypothesis_id: hypothesisId,
          commit: result.commit,
          plan: ExperimentPlanSchema.parse({
            kind: "baseline",
            change: "prepare trusted baseline",
            run_config_ref: result.evaluator_ref,
            budget: {},
            acceptance_rule: "trusted evaluator score",
          }),
          gitwork: {
            path: this.state.eda_dir ?? ".",
            branch: "main",
            base_commit: result.commit,
          },
          status: "SUCCEEDED",
          eval: EvalResultSchema.parse({
            experiment_id: experimentId,
            primary: result.metric,
            per_sample: result.evidence_ref,
          }),
          artifacts: {
            predictions: result.predictions_ref,
            evidence: result.evidence_ref,
            report: result.report_ref,
          },
        })
      )
      this.tree.setSota(experimentId)
      this.tree.save(this.treePath)
    }
    this.evaluatorRef = result.evaluator_ref
    await this.workers.publish("output", {
      source: "supervisor",
      channel: "text",
      text: `PREPARE completed with trusted metric ${result.metric}.`,
    })
    await this.transitionPhase("SEARCH")
  }

  async runValidation(): Promise<void> {
    const sotaId = this.tree.bestExperimentId()
    if (sotaId === null) throw new Error("VALIDATE requires a frozen SOTA")
    const sota = this.tree.getExperiment(sotaId)
    if (sota.eval === null) throw new Error("VALIDATE requires a trusted SOTA metric")
    const result = await this.workers.runValidationPhase(sota.commit, sota.eval.primary)
    this.state.validation = ValidationResultSchema.parse(result) as unknown as Record<string, unknown>
    this.state.phase = "COMPLETED"
    this.state.status = "COMPLETED"
    this.saveState()
    await this.workers.publish("output", {
      source: "supervisor",
      channel: "text",
      text: `VALIDATE completed · final test score ${result.final_test_score ?? ""}`.trim(),
    })
    await this.publishState()
  }

  private async transitionPhase(phase: "SEARCH" | "VALIDATE"): Promise<void> {
    this.state.phase = phase
    this.state.status = "RUNNING"
    await this.persistState()
  }

  private searchLimitReached(): boolean {
    return countSearchAttempts(this.state, this.tree) >= this.state.search_limit && this.running.size === 0
  }

  async recover(): Promise<ResearchState> {
    const reconciled = Recovery.reconcile(this.state, this.tree, {
      workspaceExists: () => true,
      artifactExists: () => true,
    })
    this.state = reconciled
    await this.persistState()
    return this.state
  }

  async runSearch(): Promise<void> {
    this.stopped = false
    while (!this.stopped) {
      const generated = await this.fillSlots()
      if (this.running.size === 0) {
        if (generated) continue
        return
      }
      const completed = await Promise.race(this.running.values())
      const planId = completed.planId
      this.running.delete(planId)
      await this.applyCompletedTurn(completed)
    }
  }

  private async fillSlots(): Promise<boolean> {
    if (this.stopped) return false
    let generated = false
    const actions = this.scheduler.nextActions(this.state, this.tree, this.running.keys())
    for (const action of actions) {
      if (this.stopped) return generated
      if (action.kind === ScheduleKind.GENERATE) {
        const hypotheses = await this.workers.runIdeatorTurn(action.count)
        await this.registerHypotheses(hypotheses)
        generated = hypotheses.length > 0
        continue
      }
      const planId = action.planId ?? action.hypothesisId
      if (planId === null) continue
      if (action.kind === ScheduleKind.START_NEW || action.kind === ScheduleKind.START_NEXT_HYPOTHESIS) {
        await this.startPlan(planId)
      }
      this.launchTurn(planId)
    }
    return generated
  }

  private launchTurn(planId: string): void {
    if (!this.running.has(planId)) {
      this.running.set(planId, this.runOneTurn(planId))
    }
  }

  private async runOneTurn(planId: string): Promise<CompletedTurn> {
    const current = this.state.plans[planId]!
    const state = PlanStateSchema.parse({ ...current, turns_used: current.turns_used + 1 })
    this.state.plans[planId] = state
    await this.persistState()

    let decision: PlanDecision | null
    try {
      decision = await this.workers.runPlanAgentTurn(planId, state)
    } catch {
      return { planId, decision: null, result: null }
    }
    if (decision === null) return { planId, decision: null, result: null }
    if (decision.decision === "abandon" && (state.best_ref ?? null) === null) {
      return { planId, decision, result: null }
    }
    const result = await this.workers.runPlanTurn(planId, state)
    return { planId, decision, result }
  }

  private async applyCompletedTurn(completed: CompletedTurn): Promise<void> {
    const planId = completed.planId
    let state = this.state.plans[planId]!
    if (completed.result !== null && completed.result.next_state !== null) {
      state = completed.result.next_state
      this.state.plans[planId] = state
    }
    if (completed.decision === null) {
      if (state.turn_limit !== null && state.turns_used >= state.turn_limit) {
        this.state.status = "WAITING"
      }
      await this.persistState()
      return
    }
    if (completed.decision.decision === "abandon" && (state.best_ref ?? null) === null) {
      await this.settlePlan(planId, null, completed.result)
      await this.publishState()
      return
    }
    const settlement = this.decideSettlement(state, completed.decision, completed.result?.report_ref ?? null)
    if (settlement.action === "continue") {
      this.saveState()
    } else if (settlement.action === "wait") {
      this.state.status = "WAITING"
      this.saveState()
    } else {
      await this.settlePlan(planId, settlement.best_ref, completed.result)
    }
    await this.publishState()
  }

  private decideSettlement(
    state: PlanState,
    decision: PlanDecision,
    reportRef: ArtifactRef | null
  ): { action: "settle" | "wait" | "continue"; best_ref: ArtifactRef | null } {
    const hasBest = state.best_ref !== undefined && state.best_ref !== null
    const patienceExhausted =
      state.patience !== undefined && state.patience !== null && (state.stale_rounds ?? 0) >= state.patience
    const turnsExhausted = state.turn_limit !== null && state.turns_used >= state.turn_limit
    if (decision.decision === "submit" || decision.decision === "abandon") {
      return { action: "settle", best_ref: state.best_ref ?? null }
    }
    if (patienceExhausted) return { action: "settle", best_ref: state.best_ref ?? null }
    if (turnsExhausted) {
      return hasBest
        ? { action: "settle", best_ref: state.best_ref ?? null }
        : { action: "wait", best_ref: null }
    }
    return { action: "continue", best_ref: null }
  }

  private async settlePlan(
    planId: string,
    bestRef: ArtifactRef | null,
    result: PlanTurnResult | null
  ): Promise<void> {
    const planInput = PlanInputSchema.parse(JSON.parse(await this.store.getText(this.state.plans[planId]!.context_ref)))
    const hypothesis = this.tree.getHypothesis(planId)
    const experimentId = `exp_${planId}`
    let primary: number | null = null
    if (bestRef === null) {
      this.tree.transitionExperiment(experimentId, "FAILED", { error: "settled without a trusted result" })
    } else {
      const best = await loadBest(bestRef, this.store)
      const reference = planInput.reference_metric
      const outcome =
        reference === null
          ? Outcome.WIN
          : compareMetric(best.metric, reference, planInput.direction, planInput.tolerance)
      const evidenceRef = best.evidence_ref
      const artifacts: Record<string, string> = { evidence: evidenceRef }
      primary = best.metric
      this.tree.completeExperiment(experimentId, {
        eval: EvalResultSchema.parse({
          experiment_id: experimentId,
          primary: best.metric,
          per_sample: evidenceRef,
        }),
        verdict: null,
        artifacts,
        commit: best.commit,
      })
      hypothesis.priority = this.scheduler.settle(planInput.reference_priority, outcome)
      this.tree.updateHypothesisStatus(planId, outcome === Outcome.WIN ? "SUPPORTED" : "REFUTED")
      if (primary !== null) {
        const sotaId = this.tree.bestExperimentId()
        if (sotaId === null) {
          this.tree.setSota(experimentId)
        } else {
          const sota = this.tree.getExperiment(sotaId)
          if (
            sota.eval === null ||
            compareMetric(primary, sota.eval.primary, planInput.direction, planInput.tolerance) === Outcome.WIN
          ) {
            this.tree.setSota(experimentId)
          }
        }
      }
    }
    this.tree.save(this.treePath)
    delete this.state.plans[planId]
    this.saveState()
  }

  async startPlan(hypothesisId: string): Promise<string> {
    if (this.evaluatorRef === null) {
      const baselines = this.tree.experiments("baseline")
      if (baselines.length === 0) throw new Error("SEARCH Plan requires a frozen evaluator")
      this.evaluatorRef = baselines[0]!.plan.run_config_ref
    }
    if (hypothesisId in this.state.plans) return hypothesisId
    const hypothesis = this.tree.getHypothesis(hypothesisId)
    const existing = this.tree.experimentForHypothesis(hypothesisId)
    if (existing !== null) {
      const status = this.tree.getExperiment(existing).status
      if (status === "SUCCEEDED" || status === "FAILED") {
        throw new Error(`hypothesis already settled: ${hypothesisId}`)
      }
    }
    const referenceId = hypothesis.parent_id ?? this.tree.bestExperimentId()
    if (referenceId === null) throw new Error("SEARCH Plan requires a frozen reference experiment")
    const reference = this.tree.getExperiment(referenceId)
    if (reference.eval === null) throw new Error("reference experiment requires a trusted metric")
    const referenceHypothesis = this.tree.getHypothesis(reference.hypothesis_id)
    const active = this.tree.activeHypotheses(referenceId, hypothesis).slice(0, -1)
    const treeRef = await this.store.putText(JSON.stringify(this.tree.toDict()))
    const planInput = PlanInputSchema.parse({
      hypothesis,
      active_ancestor_hypotheses: active,
      reference_experiment_id: referenceId,
      reference_metric: reference.eval.primary,
      reference_priority: referenceHypothesis.priority,
      direction: this.direction,
      tolerance: this.tolerance,
      evaluator_ref: this.evaluatorRef,
      tree_ref: treeRef,
      initial_turn_limit: hypothesis.turn_limit,
      initial_patience: hypothesis.patience,
    })
    const contextRef = await this.store.putText(JSON.stringify(planInput))
    const branch = await this.git.create(reference.commit, hypothesisId)
    this.branches[hypothesisId] = branch
    const experimentId = `exp_${hypothesisId}`
    this.tree.addExperiment(
      experimentId,
      ExperimentSchema.parse({
        parent_id: hypothesis.parent_id,
        hypothesis_id: hypothesisId,
        commit: reference.commit,
        plan: ExperimentPlanSchema.parse({
          kind: "search",
          change: hypothesis.intervention,
          run_config_ref: contextRef,
          budget: {},
          acceptance_rule: "trusted score",
        }),
        gitwork: branch,
      })
    )
    this.tree.transitionExperiment(experimentId, "RUNNING")
    this.state.plans[hypothesisId] = PlanStateSchema.parse({
      kind: "SEARCH",
      context_ref: contextRef,
      turns_used: 0,
      turn_limit: hypothesis.turn_limit,
      patience: hypothesis.patience,
    })
    this.saveState()
    await this.publishState()
    return hypothesisId
  }

  async planInput(planId: string) {
    return PlanInputSchema.parse(JSON.parse(await this.store.getText(this.state.plans[planId]!.context_ref)))
  }

  workspacePath(planId: string): string {
    return this.branches[planId]!.path
  }

  workspace(planId: string): GitWorkBranch {
    return this.branches[planId]!
  }

  private sotaParent(): { parentId: string; parentHypothesis: Hypothesis } {
    const parentId = this.tree.bestExperimentId()
    if (parentId === null) throw new Error("cannot propose SEARCH hypotheses without a SOTA")
    const parentExperiment = this.tree.getExperiment(parentId)
    return { parentId, parentHypothesis: this.tree.getHypothesis(parentExperiment.hypothesis_id) }
  }

  async proposeHypothesis(payload: Record<string, unknown>): Promise<{ hypothesis_id: string }> {
    const { parentId, parentHypothesis } = this.sotaParent()
    const hypothesis = HypothesisSchema.parse({
      ...payload,
      parent_id: parentId,
      priority: this.scheduler.seed(parentHypothesis),
    })
    const hypothesisId = this.tree.addHypothesis(hypothesis)
    this.tree.save(this.treePath)
    await this.publishState()
    return { hypothesis_id: hypothesisId }
  }

  async registerHypotheses(hypotheses: Hypothesis[]): Promise<{ hypothesis_ids: string[] }> {
    const { parentId, parentHypothesis } = this.sotaParent()
    const ids: string[] = []
    for (const h of hypotheses) {
      const hypothesis = HypothesisSchema.parse({
        ...h,
        id: null,
        order: null,
        parent_id: parentId,
        priority: this.scheduler.seed(parentHypothesis),
      })
      ids.push(this.tree.addHypothesis(hypothesis))
    }
    this.tree.save(this.treePath)
    await this.publishState()
    return { hypothesis_ids: ids }
  }

  async selectNextHypothesis(hypothesisId: string): Promise<{ selected: string }> {
    this.tree.getHypothesis(hypothesisId)
    if (this.state.status === "WAITING") {
      this.state.status = "RUNNING"
      this.saveState()
    }
    return { selected: hypothesisId }
  }

  async pause(): Promise<string> {
    this.state.status = "WAITING"
    await this.persistState()
    return this.state.status
  }

  async resume(): Promise<string> {
    this.state.status = "RUNNING"
    await this.persistState()
    return this.state.status
  }

  async requestStop(): Promise<string> {
    await this.stop()
    this.state.status = "STOPPED"
    await this.persistState()
    return this.state.status
  }

  async stop(): Promise<void> {
    this.stopped = true
    await Promise.allSettled(this.running.values())
    this.running.clear()
  }

  async readState(): Promise<Record<string, unknown>> {
    return {
      phase: this.state.phase,
      status: this.state.status,
      search_limit: this.state.search_limit,
      concurrency: this.state.concurrency,
      manual_mode: this.state.manual_mode,
      validation_pending: this.state.validation !== null,
    }
  }

  async readPlans(): Promise<{ plans: Array<Record<string, unknown>>; running: string[] }> {
    return {
      plans: Object.entries(this.state.plans).map(([planId, plan]) => ({
        plan_id: planId,
        kind: plan.kind,
        turns_used: plan.turns_used,
        turn_limit: plan.turn_limit,
        patience: plan.patience ?? null,
      })),
      running: [...this.running.keys()],
    }
  }
}
