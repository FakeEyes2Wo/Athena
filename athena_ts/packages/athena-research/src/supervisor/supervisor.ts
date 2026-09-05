/**
 * 固定流程 Supervisor：单写者编排 PREPARE → SEARCH → VALIDATE（移植 supervisor.py 的
 * 核心 SEARCH 滚动调度 + 阶段迁移，LLM worker 经注入函数接入，不依赖 M4 AgentRuntime）。
 */

import { statSync } from "node:fs"
import {
  ArtifactNotFoundError,
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
import { saveResearchState, researchStateToJSON, type ResearchState } from "./state.js"

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

interface CompletedTurn {
  planId: string
  decision: PlanDecision | null
  nextState: PlanState | null
}

export interface SupervisorWorkers {
  runPlanTurn(planId: string, state: PlanState): Promise<PlanTurnResult>
  runPlanAgentTurn(planId: string, state: PlanState): Promise<PlanDecision | null>
  runIdeatorTurn(count: number): Promise<Hypothesis[]>
  runPreparePhase(): Promise<PrepareResult>
  runValidationPhase(commit: string, metric: number): Promise<ValidationResult>
  publish(kind: "output" | "state", data: Record<string, unknown>): Promise<void>
}

export class FixedFlowSupervisor {
  state: ResearchState
  tree: ResearchTree
  private store: ArtifactStore
  private git: GitWorkspace
  private scheduler: Scheduler
  private settings: {
    direction: "maximize" | "minimize"
    tolerance: number
    autoValidate: boolean
  }
  private workers: SupervisorWorkers
  private running = new Map<string, Promise<CompletedTurn | { planId: string; error: unknown }>>()
  private stopped = false
  private paths: { state: string; tree: string }
  private guidance: { next: string | null; persistent: string[] } = { next: null, persistent: [] }
  private nextHypothesisId: string | null = null
  private wakeSearch: (() => void) | null = null
  private phasePromise: Promise<void> | null = null

  constructor(opts: {
    projectRoot: string
    state: ResearchState
    tree: ResearchTree
    store: ArtifactStore
    git: GitWorkspace
    scheduler?: Scheduler | null
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
    this.settings = {
      direction: opts.direction ?? "maximize",
      tolerance: opts.tolerance ?? 0.0,
      autoValidate: opts.autoValidate ?? false,
    }
    this.workers = opts.workers
    this.paths = {
      state: `${opts.projectRoot}/.athena/state.json`,
      tree: `${opts.projectRoot}/.athena/research_tree.json`,
    }
  }

  get runningPlanIds(): string[] {
    return [...this.running.keys()]
  }

  get evaluatorRef(): ArtifactRef | null {
    return this.tree.experiments("baseline")[0]?.plan.run_config_ref ?? null
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
      this.guidance.next = text
    } else if (scope === "persistent") {
      this.guidance.persistent.push(text)
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
    if (this.state.phase !== "SEARCH") {
      throw new Error(`cannot ${decision} from phase ${this.state.phase}; run SEARCH first`)
    }
    if (decision === "VALIDATE") {
      if (this.tree.bestExperimentId() === null) {
        throw new Error("VALIDATE requires a trusted SOTA baseline from completed PREPARE")
      }
      try {
        await this.stopDispatch()
        await this.runPhase(async () => {
          await this.transitionPhase("VALIDATE")
          await this.runValidation()
        })
      } catch (error) {
        await this.fail(error)
        throw error
      }
    } else {
      await this.transitionPhase("SEARCH")
      this.spawnSearch()
    }
    return { decision }
  }

  private saveState(): void {
    saveResearchState(this.paths.state, this.state)
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
        await this.runPhase(() => this.runPrepare())
      } else {
        await this.runPhase(async () => { await this.recover() })
      }
      if (!this.stopped) await this.continuePhase()
    } catch (error) {
      await this.fail(error)
    }
  }

  private async fail(error: unknown): Promise<void> {
    if (this.state.status === "FAILED") return
    this.stopped = true
    this.state.status = "FAILED"
    this.saveState()
    await Promise.allSettled([
      this.workers.publish("output", {
        source: "supervisor",
        channel: "error",
        text: `research failed: ${error instanceof Error ? error.message : String(error)}`,
      }),
      this.publishState(),
    ])
  }

  private async continuePhase(): Promise<void> {
    if (this.state.phase === "SEARCH") {
      await this.runPhase(() => this.runSearchLoop())
      if (this.stopped) return
      if (this.settings.autoValidate && this.tree.bestExperimentId() !== null) {
        await this.runPhase(async () => {
          await this.transitionPhase("VALIDATE")
          await this.runValidation()
        })
        return
      } else if (this.searchLimitReached() && this.state.status === "RUNNING") {
        this.state.status = "WAITING"
        await this.persistState()
      }
    }
    if (this.state.phase === "VALIDATE") {
      await this.runPhase(() => this.runValidation())
    }
  }

  private async runPrepare(): Promise<void> {
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
      this.tree.save(this.paths.tree)
    }
    await this.workers.publish("output", {
      source: "supervisor",
      channel: "text",
      text: `PREPARE completed with trusted metric ${result.metric}.`,
    })
    await this.transitionPhase("SEARCH")
  }

  private async runValidation(): Promise<void> {
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
    this.state = reconcilePlans(this.state, this.tree)
    for (const [planId, plan] of Object.entries(this.state.plans)) {
      try {
        await this.store.getBytes(plan.context_ref)
      } catch (error) {
        if (!(error instanceof ArtifactNotFoundError)) throw error
        this.state.status = "WAITING"
      }
      if (plan.kind === "SEARCH" && !statSync(this.workspace(planId).path, { throwIfNoEntry: false })?.isDirectory()) {
        this.state.status = "WAITING"
      }
    }
    await this.persistState()
    return this.state
  }

  private async runPhase(work: () => Promise<void>): Promise<void> {
    if (this.phasePromise !== null) return this.phasePromise
    this.stopped = false
    const task = Promise.resolve().then(work)
    this.phasePromise = task
    try {
      await task
    } finally {
      this.phasePromise = null
    }
  }

  private async runSearchLoop(): Promise<void> {
    let failure: { error: unknown } | undefined
    while (!this.stopped || this.running.size > 0) {
      try {
        if (!this.stopped && this.state.status !== "RUNNING") {
          // 暂停/等待人工决策：不再派发新 turn，直到 resume 或选择操作唤醒。
          await this.waitForWake()
          continue
        }
        const generated = this.stopped ? false : await this.fillSlots()
        if (this.running.size === 0) {
          if (this.stopped) break
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
          break
        }
        const completed = await Promise.race(this.running.values())
        const planId = completed.planId
        this.running.delete(planId)
        if ("error" in completed) throw completed.error
        await this.applyCompletedTurn(completed)
      } catch (error) {
        failure ??= { error }
        this.stopped = true
      }
    }
    if (failure) throw failure.error
  }

  private async waitForWake(): Promise<void> {
    if (this.stopped || this.state.status === "RUNNING") return
    await new Promise<void>((resolve) => {
      this.wakeSearch = resolve
    })
  }

  private wake(): void {
    const resolve = this.wakeSearch
    this.wakeSearch = null
    resolve?.()
  }

  private spawnSearch(): void {
    if (this.state.phase !== "SEARCH" || this.state.status !== "RUNNING" || this.stopped) return
    this.wake()
    if (this.phasePromise === null) this.runPhase(() => this.runSearchLoop()).catch((error) => this.fail(error))
  }

  private async fillSlots(): Promise<boolean> {
    if (this.stopped) return false
    let generated = false
    const actions = this.scheduler.nextActions(this.state, this.tree, {
      running: this.running.keys(),
      humanNext: this.nextHypothesisId,
    })
    for (const action of actions) {
      if (this.stopped) return generated
      if (action.kind === "GENERATE") {
        const hypotheses = await this.workers.runIdeatorTurn(action.count)
        generated = await this.registerHypotheses(hypotheses)
        continue
      }
      const { planId } = action
      if (action.kind !== "RESUME") {
        await this.startPlan(planId)
      }
      if (this.stopped) return generated
      if (action.kind === "START_NEXT_HYPOTHESIS") {
        this.nextHypothesisId = null
      }
      this.launchTurn(planId)
    }
    return generated
  }

  private launchTurn(planId: string): void {
    if (!this.running.has(planId)) {
      this.running.set(planId, this.runOneTurn(planId).catch((error: unknown) => ({ planId, error })))
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
      return { planId, decision: null, nextState: null }
    }
    if (decision === null) return { planId, decision: null, nextState: null }
    if (decision.decision === "abandon" && (state.best_ref ?? null) === null) {
      return { planId, decision, nextState: null }
    }
    const result = await this.workers.runPlanTurn(planId, state)
    return { planId, decision, nextState: result.next_state }
  }

  private async applyCompletedTurn(completed: CompletedTurn): Promise<void> {
    const planId = completed.planId
    let state = this.state.plans[planId]!
    if (completed.nextState !== null) {
      state = completed.nextState
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
    this.tree.save(this.paths.tree)
    delete this.state.plans[planId]
    this.saveState()
  }

  private async startPlan(hypothesisId: string): Promise<void> {
    const evaluatorRef = this.evaluatorRef
    if (evaluatorRef === null) throw new Error("SEARCH Plan requires a frozen evaluator")
    if (hypothesisId in this.state.plans) {
      if (this.tree.experimentForHypothesis(hypothesisId) === null) {
        throw new Error(
          `active plan ${hypothesisId} is missing experiment exp_${hypothesisId}; remove the plan or repair the tree`
        )
      }
      return
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
    const guidance = [...this.guidance.persistent]
    if (this.guidance.next !== null) {
      guidance.push(this.guidance.next)
      this.guidance.next = null
    }
    const treeRef = await this.store.putText(JSON.stringify(this.tree.toDict()))
    const planInput = PlanInputSchema.parse({
      hypothesis,
      reference_experiment_id: referenceId,
      reference_metric: reference.eval.primary,
      reference_priority: referenceHypothesis.priority,
      direction: this.settings.direction,
      tolerance: this.settings.tolerance,
      evaluator_ref: evaluatorRef,
      tree_ref: treeRef,
      human_context: guidance.join("\n"),
    })
    const contextRef = await this.store.putText(JSON.stringify(planInput))
    const branch = await this.git.create(reference.commit, hypothesisId)
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
    this.tree.save(this.paths.tree)
    this.state.plans[hypothesisId] = PlanStateSchema.parse({
      kind: "SEARCH",
      context_ref: contextRef,
      turns_used: 0,
      turn_limit: hypothesis.turn_limit,
      patience: hypothesis.patience,
    })
    this.saveState()
    await this.publishState()
  }

  async planInput(planId: string) {
    return PlanInputSchema.parse(JSON.parse(await this.store.getText(this.state.plans[planId]!.context_ref)))
  }

  workspace(planId: string): GitWorkBranch {
    return this.tree.getExperiment(`exp_${planId}`).gitwork
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
    this.tree.save(this.paths.tree)
    await this.publishState()
    return { hypothesis_id: hypothesisId }
  }

  private async registerHypotheses(hypotheses: Hypothesis[]): Promise<boolean> {
    const { parentId, parentHypothesis } = this.sotaParent()
    const priority = this.scheduler.policy.seed(parentHypothesis)
    for (const h of hypotheses) {
      const hypothesis = HypothesisSchema.parse({
        ...h,
        id: null,
        order: null,
        parent_id: parentId,
        priority,
      })
      this.tree.addHypothesis(hypothesis)
    }
    this.tree.save(this.paths.tree)
    await this.publishState()
    return hypotheses.length > 0
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
    this.spawnSearch()
    return this.state.status
  }

  async requestStop(): Promise<string> {
    try {
      await this.stopDispatch()
    } catch (error) {
      await this.fail(error)
    }
    if (this.state.status !== "COMPLETED" && this.state.status !== "FAILED") this.state.status = "STOPPED"
    await this.persistState()
    return this.state.status
  }

  private async stopDispatch(): Promise<void> {
    this.stopped = true
    this.wake()
    try {
      await this.phasePromise
    } finally {
      this.running.clear()
    }
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
