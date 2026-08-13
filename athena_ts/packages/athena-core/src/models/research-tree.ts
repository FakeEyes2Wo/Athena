import fs from "node:fs"
import { z } from "zod"
import { AthenaValidationError, parseOrThrow, toJSON } from "../errors.js"
import { newId } from "../id.js"
import { ArtifactRef, CommitHash } from "./contracts.js"
import {
  ComparisonVerdictSchema,
  EvalResultSchema,
  ExperimentPlanSchema,
  HypothesisSchema,
} from "./research-models.js"
import type {
  ComparisonVerdict,
  EvalResult,
  Hypothesis,
  HypothesisStatus,
} from "./research-models.js"
import { atomicWriteJson } from "../services/persistence.js"
import { GitWorkBranchSchema } from "../services/workspace.js"

/** 实验执行的生命周期状态。 */
export const ExperimentStatus = [
  "PENDING",
  "RUNNING",
  "SUCCEEDED",
  "FAILED",
  "CANCELLED",
] as const
export type ExperimentStatus = (typeof ExperimentStatus)[number]

export const SAVE_VERSION = 3
const _LOAD_VERSIONS = new Set([2, SAVE_VERSION])

/** 单次实验执行记录；其映射键即实验 ID。 */
export const ExperimentSchema = z
  .object({
    parent_id: z.string().nullable().default(null),
    hypothesis_id: z.string(),
    commit: CommitHash,
    plan: ExperimentPlanSchema,
    gitwork: GitWorkBranchSchema,
    status: z.enum(ExperimentStatus).default("PENDING"),
    eval: EvalResultSchema.nullable().default(null),
    verdict: ComparisonVerdictSchema.nullable().default(null),
    artifacts: z.record(z.string(), ArtifactRef).default({}),
    error: z.string().nullable().default(null),
  })
  .superRefine((exp, ctx) => {
    if (!exp.gitwork.path.trim() || !exp.gitwork.branch.trim()) {
      ctx.addIssue({
        code: "custom",
        message: "experiment worktree path and branch must be nonblank",
      })
      return
    }
    if (exp.status === "SUCCEEDED") {
      if (exp.eval === null) {
        ctx.addIssue({
          code: "custom",
          message: "successful experiment requires evaluation",
        })
        return
      }
      if (!Number.isFinite(exp.eval.primary)) {
        ctx.addIssue({
          code: "custom",
          message: "successful experiment primary metric must be finite",
        })
        return
      }
      if (!exp.eval.per_sample.trim()) {
        ctx.addIssue({
          code: "custom",
          message: "successful experiment requires per-sample evidence",
        })
        return
      }
    } else if (exp.eval !== null || exp.verdict !== null) {
      ctx.addIssue({
        code: "custom",
        message: "only successful experiments may contain evaluation results",
      })
      return
    }

    if (exp.status === "FAILED") {
      if (exp.error === null || !exp.error.trim()) {
        ctx.addIssue({
          code: "custom",
          message: "failed experiment requires a nonblank error",
        })
        return
      }
    } else if (exp.error !== null) {
      ctx.addIssue({
        code: "custom",
        message: "only failed experiments may contain an error",
      })
      return
    }
  })

export type Experiment = z.infer<typeof ExperimentSchema>

const ALLOWED_TRANSITIONS: Record<ExperimentStatus, ReadonlySet<ExperimentStatus>> = {
  PENDING: new Set(["RUNNING", "CANCELLED"]),
  RUNNING: new Set(["SUCCEEDED", "FAILED", "CANCELLED"]),
  SUCCEEDED: new Set(),
  FAILED: new Set(),
  CANCELLED: new Set(),
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function pyRepr(value: string | null): string {
  return value === null ? "None" : `'${value}'`
}

/** 返回从 ``experimentId`` 到根节点的祖先 id 链；含起点；父链成环时报错。 */
function parentChain(
  experimentId: string,
  experiments: Map<string, Experiment>,
): string[] {
  const path: string[] = []
  const visited = new Set<string>()
  let currentId: string | null = experimentId
  while (currentId !== null) {
    if (visited.has(currentId)) {
      throw new AthenaValidationError("experiment parent cycle")
    }
    visited.add(currentId)
    path.push(currentId)
    currentId = experiments.get(currentId)!.parent_id
  }
  return path
}

/** 持有假设、实验记录、父子关系以及选中的 SOTA。 */
export class ResearchTree {
  private _hypotheses: Map<string, Hypothesis> = new Map()
  private _experiments: Map<string, Experiment> = new Map()
  private _childrenIndex: Map<string, string[]> = new Map()
  private _sotaId: string | null = null

  /** 登记假设；重复 id 时报错，返回规范化后的 id。 */
  addHypothesis(hypothesis: Hypothesis): string {
    const hypothesisId = hypothesis.id ?? newId("hyp")
    if (this._hypotheses.has(hypothesisId)) {
      throw new AthenaValidationError(`duplicate hypothesis id: ${hypothesisId}`)
    }
    if (
      hypothesis.parent_id !== null &&
      !this._experiments.has(hypothesis.parent_id)
    ) {
      throw new AthenaValidationError(
        `unknown parent experiment id: ${hypothesis.parent_id}`,
      )
    }
    let order = hypothesis.order
    if (order === null) {
      order =
        Math.max(
          ...[...this._hypotheses.values()]
            .map((existing) => existing.order)
            .filter((o): o is number => o !== null),
          -1,
        ) + 1
    } else if (
      [...this._hypotheses.values()].some((existing) => existing.order === order)
    ) {
      throw new AthenaValidationError(`duplicate hypothesis order: ${order}`)
    }
    const stored = parseOrThrow(HypothesisSchema, {
      ...toJSON(hypothesis),
      id: hypothesisId,
      order,
    })
    this._validateSupersedes(stored, stored.parent_id)
    this._hypotheses.set(hypothesisId, stored)
    return hypothesisId
  }

  /** 按 id 取假设；未知 id 报 KeyError。 */
  getHypothesis(hypothesisId: string): Hypothesis {
    const hypothesis = this._hypotheses.get(hypothesisId)
    if (hypothesis === undefined) {
      throw new AthenaValidationError(`unknown hypothesis id: ${hypothesisId}`)
    }
    return hypothesis
  }

  /** 返回状态为 PROPOSED 的待选假设。 */
  pendingHypotheses(): Hypothesis[] {
    return [...this._hypotheses.values()].filter(
      (hypothesis) => hypothesis.status === "PROPOSED",
    )
  }

  /** 原地更新假设状态。 */
  updateHypothesisStatus(
    hypothesisId: string,
    status: HypothesisStatus,
  ): void {
    const hypothesis = this.getHypothesis(hypothesisId)
    this._hypotheses.set(
      hypothesisId,
      parseOrThrow(HypothesisSchema, { ...toJSON(hypothesis), status }),
    )
  }

  /** 登记实验记录并维护父子索引；重复 id / 未知假设或父实验时报错。 */
  addExperiment(experimentId: string, experiment: Experiment): void {
    if (!experimentId.trim()) {
      throw new AthenaValidationError("experiment id must be nonblank")
    }
    if (this._experiments.has(experimentId)) {
      throw new AthenaValidationError(`duplicate experiment id: ${experimentId}`)
    }
    if (!this._hypotheses.has(experiment.hypothesis_id)) {
      throw new AthenaValidationError(
        `unknown hypothesis id: ${experiment.hypothesis_id}`,
      )
    }
    const existingExperimentId = this.experimentForHypothesis(
      experiment.hypothesis_id,
    )
    if (existingExperimentId !== null) {
      throw new AthenaValidationError(
        `hypothesis ${experiment.hypothesis_id} already has an experiment: ${existingExperimentId}`,
      )
    }
    if (
      experiment.parent_id !== null &&
      !this._experiments.has(experiment.parent_id)
    ) {
      throw new AthenaValidationError(
        `unknown parent experiment id: ${experiment.parent_id}`,
      )
    }
    const hypothesis = this._hypotheses.get(experiment.hypothesis_id)!
    if (experiment.parent_id !== hypothesis.parent_id) {
      throw new AthenaValidationError(
        `experiment parent ${pyRepr(experiment.parent_id)} does not match hypothesis parent ${pyRepr(hypothesis.parent_id)}`,
      )
    }

    this._experiments.set(experimentId, experiment)
    if (!this._childrenIndex.has(experimentId)) {
      this._childrenIndex.set(experimentId, [])
    }
    if (experiment.parent_id !== null) {
      const siblings = this._childrenIndex.get(experiment.parent_id)
      if (siblings === undefined) {
        this._childrenIndex.set(experiment.parent_id, [experimentId])
      } else {
        siblings.push(experimentId)
      }
    }
  }

  /** 按 id 取实验记录；未知 id 报 KeyError。 */
  getExperiment(experimentId: string): Experiment {
    const experiment = this._experiments.get(experimentId)
    if (experiment === undefined) {
      throw new AthenaValidationError(`unknown experiment id: ${experimentId}`)
    }
    return experiment
  }

  /** 返回无父实验的根实验 id 列表。 */
  rootExperimentIds(): string[] {
    return [...this._experiments.entries()]
      .filter(([, experiment]) => experiment.parent_id === null)
      .map(([experimentId]) => experimentId)
  }

  /** 返回某实验的直接子实验 id 列表。 */
  listChildren(experimentId: string): string[] {
    this.getExperiment(experimentId)
    return [...(this._childrenIndex.get(experimentId) ?? [])]
  }

  /** BFS 返回某实验的全部后代实验 id。 */
  listDescendants(experimentId: string): string[] {
    this.getExperiment(experimentId)
    const descendants: string[] = []
    const queue = [...(this._childrenIndex.get(experimentId) ?? [])]
    while (queue.length > 0) {
      const childId = queue.shift()!
      descendants.push(childId)
      queue.push(...(this._childrenIndex.get(childId) ?? []))
    }
    return descendants
  }

  /** 从根到该实验的祖先链（含自身）；存在循环时报错。 */
  experimentPath(experimentId: string): string[] {
    this.getExperiment(experimentId)
    return parentChain(experimentId, this._experiments).reverse()
  }

  /** 按祖先链顺序返回每级实验对应的假设。 */
  hypothesesPath(experimentId: string): Hypothesis[] {
    return this.experimentPath(experimentId).map((itemId) =>
      this.getHypothesis(this.getExperiment(itemId).hypothesis_id),
    )
  }

  /** Return the experiment registered for a hypothesis, if one exists. */
  experimentForHypothesis(hypothesisId: string): string | null {
    for (const [experimentId, experiment] of this._experiments) {
      if (experiment.hypothesis_id === hypothesisId) {
        return experimentId
      }
    }
    return null
  }

  /** Return experiment records, optionally filtered to one Plan kind. */
  experiments(kind?: string | null): Experiment[] {
    const all = [...this._experiments.values()]
    if (kind === null || kind === undefined) {
      return all
    }
    return all.filter((experiment) => experiment.plan.kind === kind)
  }

  /** Return selected ancestry minus superseded claims, followed by child. */
  activeHypotheses(experimentId: string, child: Hypothesis): Hypothesis[] {
    if (child.parent_id !== experimentId) {
      throw new AthenaValidationError(
        "child parent experiment does not match selected parent experiment",
      )
    }
    const ancestors = this.hypothesesPath(experimentId)
    this._validateSupersedes(child, experimentId, ancestors)
    const superseded = new Set(child.supersedes)
    return [
      ...ancestors.filter(
        (hypothesis) =>
          hypothesis.id !== null && !superseded.has(hypothesis.id),
      ),
      child,
    ]
  }

  private _validateSupersedes(
    child: Hypothesis,
    experimentId: string | null,
    ancestors?: Hypothesis[],
  ): void {
    if (child.supersedes.length !== new Set(child.supersedes).size) {
      throw new AthenaValidationError(
        "supersedes contains duplicate hypothesis ids",
      )
    }
    if (child.id !== null && child.supersedes.includes(child.id)) {
      throw new AthenaValidationError(
        "supersedes cannot include the child hypothesis",
      )
    }
    if (child.supersedes.length === 0) {
      return
    }
    if (experimentId === null) {
      throw new AthenaValidationError(
        "supersedes ids must be on the selected parent experiment path",
      )
    }
    const lineage = ancestors ?? this.hypothesesPath(experimentId)
    const ancestorIds = new Set(lineage.map((hypothesis) => hypothesis.id))
    if (child.supersedes.some((item) => !ancestorIds.has(item))) {
      throw new AthenaValidationError(
        "every supersedes id must be on the selected parent experiment path",
      )
    }
  }

  /** 按允许的转移表推进实验状态；非法转移或终态报错。 */
  transitionExperiment(
    experimentId: string,
    status: ExperimentStatus,
    opts: { error?: string | null } = {},
  ): void {
    const error: string | null = opts.error ?? null
    const experiment = this.getExperiment(experimentId)
    if (ALLOWED_TRANSITIONS[experiment.status].size === 0) {
      throw new AthenaValidationError(
        `experiment status is terminal: ${experiment.status}`,
      )
    }
    if (!ALLOWED_TRANSITIONS[experiment.status].has(status)) {
      throw new AthenaValidationError(
        `invalid experiment transition: ${experiment.status} -> ${status}`,
      )
    }
    if (status === "FAILED" && (error === null || !error.trim())) {
      throw new AthenaValidationError("FAILED transition requires a nonblank error")
    }
    if (status !== "FAILED" && error !== null) {
      throw new AthenaValidationError("only FAILED transition accepts an error")
    }
    this._experiments.set(
      experimentId,
      parseOrThrow(ExperimentSchema, {
        ...toJSON(experiment),
        status,
        error,
      }),
    )
  }

  /** 把 RUNNING 实验置为 SUCCEEDED 并写入评估与产物。 */
  completeExperiment(
    experimentId: string,
    opts: {
      eval: EvalResult
      verdict: ComparisonVerdict | null
      artifacts: Record<string, ArtifactRef>
      commit?: CommitHash | null
    },
  ): void {
    const { eval: evaluation, verdict, artifacts } = opts
    const commit = opts.commit ?? null
    const experiment = this.getExperiment(experimentId)
    if (experiment.status !== "RUNNING") {
      throw new AthenaValidationError("only a running experiment can complete")
    }
    if (evaluation.experiment_id !== experimentId) {
      throw new AthenaValidationError(
        "evaluation experiment id does not match record key",
      )
    }
    if (!Number.isFinite(evaluation.primary)) {
      throw new AthenaValidationError("evaluation primary metric must be finite")
    }
    const mergedArtifacts = { ...experiment.artifacts, ...artifacts }
    this._experiments.set(
      experimentId,
      parseOrThrow(ExperimentSchema, {
        ...toJSON(experiment),
        status: "SUCCEEDED",
        eval: evaluation,
        verdict: verdict === null ? null : verdict,
        artifacts: mergedArtifacts,
        commit: commit ?? experiment.commit,
        error: null,
      }),
    )
  }

  /** 为实验记录附加一个产物引用；kind 非空。 */
  attachArtifact(experimentId: string, kind: string, ref: ArtifactRef): void {
    if (!kind.trim()) {
      throw new AthenaValidationError("artifact kind must be nonblank")
    }
    const experiment = this.getExperiment(experimentId)
    this._experiments.set(
      experimentId,
      parseOrThrow(ExperimentSchema, {
        ...toJSON(experiment),
        artifacts: { ...experiment.artifacts, [kind]: ref },
      }),
    )
  }

  /** 把成功的 baseline/search 实验标记为 SOTA。 */
  setSota(experimentId: string): void {
    const experiment = this.getExperiment(experimentId)
    if (experiment.status !== "SUCCEEDED") {
      throw new AthenaValidationError("SOTA requires a successful experiment")
    }
    if (experiment.plan.kind !== "baseline" && experiment.plan.kind !== "search") {
      throw new AthenaValidationError(
        "experiment kind is not eligible for SOTA",
      )
    }
    this._sotaId = experimentId
  }

  /** 返回当前 SOTA 实验 id；尚无则为 None。 */
  bestExperimentId(): string | null {
    return this._sotaId
  }

  /** 导出为带版本的 JSON 兼容对象。 */
  toDict(): {
    version: number
    sota_id: string | null
    hypotheses: Record<string, Hypothesis>
    experiments: Record<string, Experiment>
  } {
    const hypotheses: Record<string, Hypothesis> = {}
    for (const [hypothesisId, hypothesis] of this._hypotheses) {
      hypotheses[hypothesisId] = toJSON(hypothesis)
    }
    const experiments: Record<string, Experiment> = {}
    for (const [experimentId, experiment] of this._experiments) {
      experiments[experimentId] = toJSON(experiment)
    }
    return {
      version: SAVE_VERSION,
      sota_id: this._sotaId,
      hypotheses,
      experiments,
    }
  }

  /** 从对象重建树并校验版本/字段/父子关系与 SOTA。 */
  static fromDict(payload: Record<string, unknown>): ResearchTree {
    const version = payload["version"]
    if (typeof version !== "number" || !_LOAD_VERSIONS.has(version)) {
      throw new AthenaValidationError(
        `unsupported research tree version: ${version}`,
      )
    }

    const expectedFields = [
      "version",
      "sota_id",
      "hypotheses",
      "experiments",
    ].sort()
    const keys = Object.keys(payload).sort()
    if (
      keys.length !== expectedFields.length ||
      keys.some((key, index) => key !== expectedFields[index])
    ) {
      throw new AthenaValidationError(
        `invalid research tree top-level fields: expected ${JSON.stringify(expectedFields)}`,
      )
    }

    const rawHypotheses = payload["hypotheses"]
    const rawExperiments = payload["experiments"]
    if (!isPlainObject(rawHypotheses)) {
      throw new AthenaValidationError("research tree hypotheses must be a mapping")
    }
    if (!isPlainObject(rawExperiments)) {
      throw new AthenaValidationError("research tree experiments must be a mapping")
    }

    const parsedHypotheses: Array<[string, Hypothesis]> = []
    const explicitOrders = new Set<number>()
    for (const [hypothesisId, rawHypothesis] of Object.entries(rawHypotheses)) {
      if (!hypothesisId.trim()) {
        throw new AthenaValidationError(
          "hypothesis mapping keys must be nonblank strings",
        )
      }
      const hypothesis = parseOrThrow(HypothesisSchema, rawHypothesis)
      if (hypothesis.id !== hypothesisId) {
        throw new AthenaValidationError(
          `hypothesis id does not match mapping key: ${hypothesisId}`,
        )
      }
      const order = hypothesis.order
      if (order === null && version === SAVE_VERSION) {
        throw new AthenaValidationError(
          `research tree v${SAVE_VERSION} hypothesis order is required`,
        )
      }
      if (order !== null && explicitOrders.has(order)) {
        throw new AthenaValidationError(`duplicate hypothesis order: ${order}`)
      }
      if (order !== null) {
        explicitOrders.add(order)
      }
      parsedHypotheses.push([hypothesisId, hypothesis])
    }

    const hypotheses = new Map<string, Hypothesis>()
    const usedOrders = new Set(explicitOrders)
    let nextOrder = 0
    for (const [hypothesisId, hypothesis] of parsedHypotheses) {
      if (hypothesis.order === null) {
        while (usedOrders.has(nextOrder)) {
          nextOrder += 1
        }
        const assigned = parseOrThrow(HypothesisSchema, {
          ...toJSON(hypothesis),
          order: nextOrder,
        })
        usedOrders.add(nextOrder)
        nextOrder += 1
        hypotheses.set(hypothesisId, assigned)
      } else {
        hypotheses.set(hypothesisId, hypothesis)
      }
    }

    const experiments = new Map<string, Experiment>()
    for (const [experimentId, rawExperiment] of Object.entries(rawExperiments)) {
      if (!experimentId.trim()) {
        throw new AthenaValidationError(
          "experiment mapping keys must be nonblank strings",
        )
      }
      const experiment = parseOrThrow(ExperimentSchema, rawExperiment)
      if (
        experiment.eval !== null &&
        experiment.eval.experiment_id !== experimentId
      ) {
        throw new AthenaValidationError(
          `evaluation experiment id does not match mapping key: ${experimentId}`,
        )
      }
      experiments.set(experimentId, experiment)
    }

    const childrenIndex = ResearchTree._validateAndBuildChildren(
      hypotheses,
      experiments,
    )
    for (const hypothesis of hypotheses.values()) {
      if (
        hypothesis.parent_id !== null &&
        !experiments.has(hypothesis.parent_id)
      ) {
        throw new AthenaValidationError(
          `unknown parent experiment id: ${hypothesis.parent_id}`,
        )
      }
    }
    for (const [experimentId, experiment] of experiments) {
      const hypothesis = hypotheses.get(experiment.hypothesis_id)!
      if (experiment.parent_id !== hypothesis.parent_id) {
        throw new AthenaValidationError(
          `experiment ${experimentId} parent ${pyRepr(experiment.parent_id)} does not match hypothesis parent ${pyRepr(hypothesis.parent_id)}`,
        )
      }
    }

    const sotaId = payload["sota_id"]
    if (sotaId !== null && typeof sotaId !== "string") {
      throw new AthenaValidationError("sota_id must be a string or null")
    }

    const tree = new ResearchTree()
    tree._hypotheses = hypotheses
    tree._experiments = experiments
    tree._childrenIndex = childrenIndex
    for (const hypothesis of hypotheses.values()) {
      tree._validateSupersedes(hypothesis, hypothesis.parent_id)
    }
    if (sotaId !== null) {
      tree.setSota(sotaId)
    }
    return tree
  }

  private static _validateAndBuildChildren(
    hypotheses: Map<string, Hypothesis>,
    experiments: Map<string, Experiment>,
  ): Map<string, string[]> {
    const children = new Map<string, string[]>()
    for (const experimentId of experiments.keys()) {
      children.set(experimentId, [])
    }
    const hypothesisExperiments = new Map<string, string>()
    for (const [experimentId, experiment] of experiments) {
      if (!hypotheses.has(experiment.hypothesis_id)) {
        throw new AthenaValidationError(
          `unknown hypothesis id: ${experiment.hypothesis_id}`,
        )
      }
      const existingExperimentId = hypothesisExperiments.get(
        experiment.hypothesis_id,
      )
      if (existingExperimentId !== undefined) {
        throw new AthenaValidationError(
          `hypothesis ${experiment.hypothesis_id} already has an experiment: ${existingExperimentId}`,
        )
      }
      hypothesisExperiments.set(experiment.hypothesis_id, experimentId)
      if (experiment.parent_id !== null) {
        if (!experiments.has(experiment.parent_id)) {
          throw new AthenaValidationError(
            `unknown parent experiment id: ${experiment.parent_id}`,
          )
        }
        children.get(experiment.parent_id)!.push(experimentId)
      }
    }

    for (const experimentId of experiments.keys()) {
      parentChain(experimentId, experiments)
    }
    return children
  }

  /** 原子写 JSON 到目标路径，返回目标路径。 */
  save(path: string): string {
    return atomicWriteJson(path, this.toDict())
  }

  /** 从 JSON 文件加载并重建树；根须为对象。 */
  static load(path: string): ResearchTree {
    const payload = JSON.parse(fs.readFileSync(path, "utf-8"))
    if (!isPlainObject(payload)) {
      throw new AthenaValidationError("research tree payload must be an object")
    }
    return ResearchTree.fromDict(payload)
  }
}
