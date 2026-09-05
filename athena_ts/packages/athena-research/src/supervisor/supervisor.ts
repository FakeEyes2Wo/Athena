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

import {
  ValidationResultSchema, PlanInputSchema, PlanStateSchema,
  type ValidationResult, type PlanDecision, type PlanState,
} from "../contracts.js"
import { buildFinalReport } from "../report.js"
import { loadBest, type PlanTurnResult } from "./experiment.js"
import { Outcome, type Outcome as OutcomeType } from "./ranker.js"
import type { PrepareResult } from "./prepare.js"
import { reconcilePlans } from "./recovery.js"
import { Scheduler, countSearchAttempts } from "./scheduler.js"
import { saveResearchState, researchStateToJSON, type ResearchState, type ResearchStatus } from "./state.js"

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

const MAX_PLAN_TURNS = 20

function finalReportText(validation: Record<string, unknown>): string {
  const parts = ["VALIDATE completed"]
  const metric = (key: string): string | null => {
    const value = validation[key]
    if (value === undefined || value === null) return null
    if (typeof value !== "number") return String(value)
    return Number.isInteger(value) ? String(value) : value.toFixed(4)
  }
  const finalScore = metric("final_test_score")
  if (finalScore !== null) parts.push(`final test score ${finalScore}`)
  const gap = metric("generalization_gap")
  if (gap !== null) parts.push(`generalization gap ${gap}`)
  if (validation["generalization_warning"] === true) parts.push("generalization warning")
  return parts.join(" · ")
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
  private frozenEvaluatorRef: ArtifactRef | null
  private direction: "maximize" | "minimize"
  private tolerance: number
  private workers: SupervisorWorkers
  private branches: Record<string, GitWorkBranch> = {}
  private running: Map<string, Promise<CompletedTurn>> = new Map()
  private stopped = false
  private statePath: string
  private treePath: string
  private autoValidate: boolean
  private nextGuidance: string | null = null
  private persistentGuidance: string[] = []
  private nextHypothesisId: string | null = null
  private wakeWaiters = new Set<() => void>()
  private searchPromise: Promise<void> | null = null

  constructor(opts: {
    projectRoot: string
    state: ResearchState
    tree: ResearchTree
    store: ArtifactStore
    git: GitWorkspace
    scheduler?: Scheduler | null
    evaluatorRef?: ArtifactRef | null
    direction?: "maximize" | "minimize"
    tolerance?: number
    autoValidate?: boolean
    workers: SupervisorWorkers
  }) {
    this.state = opts.state
    this.tree = opts.tree
    this.store = opts.store
    this.git = opts.git
    this.scheduler = opts.scheduler ?? new Scheduler()
    this.frozenEvaluatorRef = opts.evaluatorRef ?? null
    this.direction = opts.direction ?? "maximize"
    this.tolerance = opts.tolerance ?? 0.0
    this.autoValidate = opts.autoValidate ?? false
    this.workers = opts.workers
    this.statePath = `${opts.projectRoot}/.athena/state.json`
    this.treePath = `${opts.projectRoot}/.athena/research_tree.json`
  }

  get runningPlanIds(): string[] {
    return [...this.running.keys()]
  }

  get evaluatorRef(): ArtifactRef | null {
    return this.frozenEvaluatorRef
  }

  /** 替换 worker 接线（例如 research_run 工具在拿到 DSH ``exec.agent`` 后注入）。 */
  setWorkers(workers: SupervisorWorkers): void {
    this.workers = workers
  }

  /** 持久化 SupervisorAgent 的结构化任务理解（核心设计：进入 SEARCH 前的冻结输入）。 */
  async recordTaskUnderstanding(payload: Record<string, unknown>): Promise<{ recorded: boolean; task_understanding: Record<string, unknown> }> {
    this.state.task_understanding = { ...payload }
    await this.persistState()
    return { recorded: true, task_understanding: this.state.task_understanding }
  }

  /** 记录只冻结进后续 Plan 输入的 guidance（next 一次性 / persistent 持久）。 */
  async recordGuidance(text: string, scope: "next" | "persistent"): Promise<{ text: string; scope: string }> {
    if (!text.trim()) throw new Error("guidance text must be nonblank")
    if (scope === "next") {
      this.nextGuidance = text
    } else if (scope === "persistent") {
      this.persistentGuidance.push(text)
    } else {
      throw new Error(`unsupported guidance scope: ${scope}`)
    }
    return { text, scope }
  }

  /** 只读快照：待选假设 / SOTA / 已用与剩余搜索预算。 */
  async readHypotheses(): Promise<Record<string, unknown>> {
    const pending = this.tree.pendingHypotheses().map((hypothesis) => ({
      id: hypothesis.id,
      statement: hypothesis.statement,
      priority: hypothesis.priority,
    }))
    return {
      pending,
      sota: this.tree.bestExperimentId(),
      attempts: countSearchAttempts(this.state, this.tree),
      search_limit: this.state.search_limit,
    }
  }

  /** 调整 SEARCH 预算；交互模式下预算用尽停在 WAITING 时追加预算会恢复 RUNNING。 */
  async configureSearch(payload: { search_limit?: number; concurrency?: number }): Promise<Record<string, unknown>> {
    const searchLimit = payload.search_limit
    const concurrency = payload.concurrency
    if (searchLimit !== undefined) {
      if (!Number.isInteger(searchLimit) || searchLimit < 1) {
        throw new Error("search_limit must be at least 1")
      }
      this.state.search_limit = searchLimit
    }
    if (concurrency !== undefined) {
      if (!Number.isInteger(concurrency) || concurrency < 1) {
        throw new Error("concurrency must be at least 1")
      }
      this.state.concurrency = concurrency
    }
    if (this.state.phase === "SEARCH" && this.state.status === "WAITING") {
      this.state.status = "RUNNING"
    }
    await this.persistState()
    this.spawnSearch()
    return {
      search_limit: this.state.search_limit,
      concurrency: this.state.concurrency,
    }
  }

  /** 在 auto 与 manual 之间切换 SEARCH 调度（manual 等待 selectNextHypothesis 点名）。 */
  async setManualMode(manual: boolean): Promise<{ manual_mode: boolean }> {
    this.state.manual_mode = manual
    if (this.state.status === "WAITING") {
      this.state.status = "RUNNING"
    }
    await this.persistState()
    this.wake()
    this.spawnSearch()
    return { manual_mode: this.state.manual_mode }
  }

  /** 给已耗尽预算的等待 Plan 追加 turn_limit 或 patience。 */
  async updateWaitingPlanBudget(payload: {
    plan_id?: unknown
    turn_limit?: number
    unlimited_turns?: boolean
    patience?: number
  }): Promise<Record<string, unknown>> {
    const planId = String(payload.plan_id ?? "")
    const plan = this.state.plans[planId]
    if (plan === undefined) throw new Error(`unknown Plan: ${planId}`)
    const exhausted = plan.turn_limit !== null && plan.turns_used >= plan.turn_limit
    if (!exhausted) throw new Error(`Plan is not waiting: ${planId}`)
    if (payload.turn_limit !== undefined && payload.unlimited_turns === true) {
      throw new Error("turn_limit and unlimited_turns are mutually exclusive")
    }
    const updates: Record<string, unknown> = {}
    if (payload.turn_limit !== undefined) {
      const value = payload.turn_limit
      if (!Number.isInteger(value) || value <= plan.turns_used) {
        throw new Error("turn_limit must exceed turns already used")
      }
      if (value > MAX_PLAN_TURNS) {
        throw new Error(`turn_limit exceeds configured maximum ${MAX_PLAN_TURNS}`)
      }
      updates["turn_limit"] = value
    } else if (payload.unlimited_turns === true) {
      updates["turn_limit"] = null
    }
    if (payload.patience !== undefined) {
      const value = payload.patience
      if (!Number.isInteger(value) || value < 1 || value > 5) {
        throw new Error("patience must be between 1 and 5")
      }
      updates["patience"] = value
    }
    if (Object.keys(updates).length === 0) {
      throw new Error("at least one Plan budget must be provided")
    }
    this.state.plans[planId] = PlanStateSchema.parse({ ...plan, ...updates })
    this.state.status = "RUNNING"
    await this.persistState()
    this.spawnSearch()
    return { plan_id: planId, ...updates }
  }

  /** 交互路径的阶段决策：切 SEARCH 重启调度循环，切 VALIDATE 直接跑完。 */
  async setPhaseDecision(decision: "SEARCH" | "VALIDATE"): Promise<{ decision: string }> {
    if (decision === "VALIDATE") {
      if (this.tree.bestExperimentId() === null) {
        throw new Error("VALIDATE requires a trusted SOTA baseline from completed PREPARE")
      }
      if (this.state.phase !== "SEARCH") {
        throw new Error(`cannot VALIDATE from phase ${this.state.phase}; run SEARCH first`)
      }
    }
    await this.transitionPhase(decision)
    if (decision === "SEARCH") {
      this.spawnSearch()
    } else {
      await this.runValidation()
    }
    return { decision }
  }

  /** 交互式快捷入口：SEARCH 达到预算/人工决策后显式推进 VALIDATE。 */
  async startValidation(): Promise<{ status: ResearchStatus }> {
    await this.setPhaseDecision("VALIDATE")
    return { status: this.state.status }
  }

  private saveState(): void {
    saveResearchState(this.statePath, this.state)
  }

  private async publishState(): Promise<void> {
    await this.workers.publish("state", { type: "state", ...researchStateToJSON(this.state) })
  }

  private async persistState(): Promise<void> {
    this.saveState()
    await this.publishState()
  }

  async start(): Promise<void> {
    this.stopped = false
    try {
      if (this.state.phase === "PREPARE") {
        await this.runPrepare()
      } else {
        await this.recover()
      }
      await this.continuePhase()
    } catch (error) {
      // 阶段执行失败统一观测：置 FAILED 并发布，避免后台驱动把异常吞掉后
      // 状态永远卡在 PREPARE/SEARCH。
      this.state.status = "FAILED"
      this.saveState()
      await this.workers.publish("output", {
        source: "supervisor",
        channel: "error",
        text: `research failed: ${error instanceof Error ? error.message : String(error)}`,
      })
      await this.publishState()
    }
  }

  async continuePhase(): Promise<void> {
    if (this.state.phase === "SEARCH") {
      await this.runSearch()
      if (this.stopped) return
      if (this.autoValidate && this.tree.bestExperimentId() !== null) {
        await this.transitionPhase("VALIDATE")
      } else if (this.searchLimitReached() && this.state.status === "RUNNING") {
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
    this.frozenEvaluatorRef = result.evaluator_ref
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
    const validation = ValidationResultSchema.parse(result) as unknown as Record<string, unknown>
    // VALIDATE 完成后生成并持久化最终报告：内容寻址 artifact 供 GUI/审计读取，
    // ``report_ref`` 写入 state.validation，最终文本也发布到会话流。
    validation["report_ref"] = await this.store.putText(
      buildFinalReport(this.tree, validation)
    )
    this.state.validation = validation
    this.state.phase = "COMPLETED"
    this.state.status = "COMPLETED"
    this.saveState()
    await this.workers.publish("output", {
      source: "supervisor",
      channel: "text",
      text: finalReportText(validation),
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
    const reconciled = reconcilePlans(this.state, this.tree, {
      workspaceExists: () => true,
      artifactExists: () => true,
    })
    this.state = reconciled
    await this.persistState()
    return this.state
  }

  async runSearch(): Promise<void> {
    if (this.searchPromise !== null) return this.searchPromise
    const loop = this.runSearchLoop()
    this.searchPromise = loop
    try {
      await loop
    } finally {
      this.searchPromise = null
    }
  }

  private async runSearchLoop(): Promise<void> {
    this.stopped = false
    while (!this.stopped) {
      if (this.state.status !== "RUNNING") {
        // 暂停/等待人工决策：不再派发新 turn，直到 resume 或选择操作唤醒。
        await this.waitForWake()
        continue
      }
      const generated = await this.fillSlots()
      if (this.running.size === 0) {
        if (generated) continue
        if (
          this.state.manual_mode &&
          this.nextHypothesisId === null &&
          this.tree.pendingHypotheses().length > 0
        ) {
          this.state.status = "WAITING"
          await this.persistState()
          await this.waitForWake()
          continue
        }
        return
      }
      const completed = await Promise.race(this.running.values())
      const planId = completed.planId
      this.running.delete(planId)
      await this.applyCompletedTurn(completed)
    }
  }

  private async waitForWake(): Promise<void> {
    if (this.stopped) return
    await new Promise<void>((resolve) => {
      this.wakeWaiters.add(resolve)
    })
  }

  private wake(): void {
    for (const resolve of [...this.wakeWaiters]) {
      this.wakeWaiters.delete(resolve)
      resolve()
    }
  }

  private spawnSearch(): void {
    if (
      this.state.phase === "SEARCH" &&
      this.state.status === "RUNNING" &&
      !this.stopped &&
      this.searchPromise === null
    ) {
      this.wake()
      const search = this.runSearch()
      search.catch(() => {})
    }
  }

  private async fillSlots(): Promise<boolean> {
    if (this.stopped) return false
    let generated = false
    const actions = this.scheduler.nextActions(this.state, this.tree, this.running.keys(), {
      humanNext: this.nextHypothesisId,
      manual: this.state.manual_mode,
    })
    for (const action of actions) {
      if (this.stopped) return generated
      if (action.kind === "GENERATE") {
        const hypotheses = await this.workers.runIdeatorTurn(action.count)
        const registered = await this.registerHypotheses(hypotheses)
        generated = registered.hypothesis_ids.length > 0
        continue
      }
      const { planId } = action
      if (action.kind !== "RESUME") {
        await this.startPlan(planId)
      }
      if (action.kind === "START_NEXT_HYPOTHESIS") {
        this.nextHypothesisId = null
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
      await this.settlePlan(planId, null)
      await this.publishState()
      return
    }
    const settlement = this.decideSettlement(state, completed.decision)
    if (settlement.action === "continue") {
      this.saveState()
    } else if (settlement.action === "wait") {
      this.state.status = "WAITING"
      this.saveState()
    } else {
      await this.settlePlan(planId, settlement.best_ref)
    }
    await this.publishState()
  }

  private decideSettlement(
    state: PlanState,
    decision: PlanDecision
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
    bestRef: ArtifactRef | null
  ): Promise<void> {
    const planInput = PlanInputSchema.parse(JSON.parse(await this.store.getText(this.state.plans[planId]!.context_ref)))
    const hypothesis = this.tree.getHypothesis(planId)
    const experimentId = `exp_${planId}`
    let primary: number | null = null
    if (bestRef === null) {
      this.tree.transitionExperiment(experimentId, "FAILED", { error: "settled without a trusted result" })
      // 实验无有效证据：标记 INCONCLUSIVE，不按胜负更新评级（对齐 Python）。
      this.tree.updateHypothesisStatus(planId, "INCONCLUSIVE")
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
      hypothesis.priority = this.scheduler.policy.settle(planInput.reference_priority, outcome)
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
    if (this.frozenEvaluatorRef === null) {
      const baselines = this.tree.experiments("baseline")
      if (baselines.length === 0) throw new Error("SEARCH Plan requires a frozen evaluator")
      this.frozenEvaluatorRef = baselines[0]!.plan.run_config_ref
    }
    if (hypothesisId in this.state.plans) {
      if (this.tree.experimentForHypothesis(hypothesisId) === null) {
        throw new Error(
          `active plan ${hypothesisId} is missing experiment exp_${hypothesisId}; remove the plan or repair the tree`
        )
      }
      return hypothesisId
    }
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
    const guidance = [...this.persistentGuidance]
    if (this.nextGuidance !== null) {
      guidance.push(this.nextGuidance)
      this.nextGuidance = null
    }
    const treeRef = await this.store.putText(JSON.stringify(this.tree.toDict()))
    const planInput = PlanInputSchema.parse({
      hypothesis,
      reference_experiment_id: referenceId,
      reference_metric: reference.eval.primary,
      reference_priority: referenceHypothesis.priority,
      direction: this.direction,
      tolerance: this.tolerance,
      evaluator_ref: this.frozenEvaluatorRef,
      tree_ref: treeRef,
      human_context: guidance.join("\n"),
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
    this.tree.save(this.treePath)
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
      priority: this.scheduler.policy.seed(parentHypothesis),
    })
    const hypothesisId = this.tree.addHypothesis(hypothesis)
    this.tree.save(this.treePath)
    await this.publishState()
    return { hypothesis_id: hypothesisId }
  }

  async registerHypotheses(hypotheses: Hypothesis[]): Promise<{ hypothesis_ids: string[] }> {
    const { parentId, parentHypothesis } = this.sotaParent()
    const priority = this.scheduler.policy.seed(parentHypothesis)
    const ids: string[] = []
    for (const h of hypotheses) {
      const hypothesis = HypothesisSchema.parse({
        ...h,
        id: null,
        order: null,
        parent_id: parentId,
        priority,
      })
      ids.push(this.tree.addHypothesis(hypothesis))
    }
    this.tree.save(this.treePath)
    await this.publishState()
    return { hypothesis_ids: ids }
  }

  async selectNextHypothesis(hypothesisId: string): Promise<{ selected: string }> {
    this.tree.getHypothesis(hypothesisId)
    const existing = this.tree.experimentForHypothesis(hypothesisId)
    if (existing !== null) {
      const status = this.tree.getExperiment(existing).status
      if (status === "SUCCEEDED" || status === "FAILED") {
        throw new Error(`hypothesis already settled: ${hypothesisId}`)
      }
    }
    this.nextHypothesisId = hypothesisId
    if (this.state.status === "WAITING") {
      this.state.status = "RUNNING"
      this.saveState()
    }
    this.wake()
    this.spawnSearch()
    return { selected: hypothesisId }
  }

  async pause(): Promise<string> {
    this.state.status = "WAITING"
    await this.persistState()
    this.wake()
    return this.state.status
  }

  async resume(): Promise<string> {
    this.state.status = "RUNNING"
    await this.persistState()
    this.wake()
    this.spawnSearch()
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
    this.wake()
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
      task_understanding: this.state.task_understanding,
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
