/**
 * Hypothesis ranking and deduplication for rolling SEARCH（移植
 * ``research/supervisor/ranker.py``）。
 *
 * 用一个小型 UCB 风格选择器替代纯 ``(-priority, order)`` FIFO 排序：
 * rubric 冷启动先验 + 策略累计强度（Elo/BT 代理）+ novelty 探索奖励 -
 * 归一化成本惩罚。所有信号都是 tree 的确定性纯函数。
 */

import type { Hypothesis, ResearchTree } from "@athena/core"

const WORD = /[a-z0-9]+/g

export function tokenize(text: string): Set<string> {
  return new Set(text.toLowerCase().match(WORD) ?? [])
}

export function jaccard(left: Set<string>, right: Set<string>): number {
  if (left.size === 0 && right.size === 0) return 0.0
  const union = new Set([...left, ...right])
  let intersection = 0
  for (const token of left) {
    if (right.has(token)) intersection += 1
  }
  return intersection / union.size
}

function textSimilarity(left: Hypothesis, right: Hypothesis): number {
  const a = new Set([...tokenize(left.statement), ...tokenize(left.intervention)])
  const b = new Set([...tokenize(right.statement), ...tokenize(right.intervention)])
  return jaccard(a, b)
}

/** 证据与具体性给出的 [0.4, 1.0] 冷启动先验。 */
export function rubricPrior(hypothesis: Hypothesis): number {
  const evidence = hypothesis.sources.length > 0 ? 1.0 : 0.0
  const specific = tokenize(hypothesis.intervention).size >= 3 ? 1.0 : 0.0
  return 0.4 + 0.3 * evidence + 0.3 * specific
}

function settledHypotheses(tree: ResearchTree): Hypothesis[] {
  return Object.values(tree.toDict().hypotheses).filter(
    (hypothesis) => hypothesis.status !== "PROPOSED"
  )
}

/** 1 - 与已结算假设的最大相似度（无已结算时 1.0）。 */
export function novelty(hypothesis: Hypothesis, tree: ResearchTree): number {
  const settled = settledHypotheses(tree)
  if (settled.length === 0) return 1.0
  const similarity = Math.max(...settled.map((other) => textSimilarity(hypothesis, other)))
  return 1.0 - similarity
}

/** Greedy keep candidates distinct from existing and each other (input order wins ties). */
export function deduplicate(
  candidates: Hypothesis[],
  existing: Hypothesis[],
  threshold: number
): Hypothesis[] {
  const kept: Hypothesis[] = []
  const seen = [...existing]
  for (const candidate of candidates) {
    if (seen.some((other) => textSimilarity(candidate, other) >= threshold)) continue
    kept.push(candidate)
    seen.push(candidate)
  }
  return kept
}

export interface RankConfig {
  priorWeight: number
  strengthWeight: number
  noveltyWeight: number
  costWeight: number
  dedupThreshold: number
}

export const DEFAULT_RANK_CONFIG: RankConfig = {
  priorWeight: 0.4,
  strengthWeight: 0.3,
  noveltyWeight: 0.2,
  costWeight: 0.1,
  dedupThreshold: 0.8,
}

export interface HypothesisPolicyLike {
  priority(hypothesis: Hypothesis, tree: ResearchTree): number
}

/** Rank pending hypotheses by ``prior + strength + novelty - cost``. */
export class Selector {
  constructor(
    private policy: HypothesisPolicyLike,
    private config: RankConfig = DEFAULT_RANK_CONFIG
  ) {
    this.validateConfig(config)
  }

  private validateConfig(config: RankConfig): void {
    const weights = [config.priorWeight, config.strengthWeight, config.noveltyWeight] as const
    if (weights.some((weight) => weight < 0 || weight > 1)) {
      throw new Error("selector weights must be between 0 and 1")
    }
    if (config.costWeight < 0) throw new Error("cost_weight must be nonnegative")
    if (config.dedupThreshold <= 0 || config.dedupThreshold > 1) {
      throw new Error("dedup_threshold must be in (0, 1]")
    }
  }

  get dedupThreshold(): number {
    return this.config.dedupThreshold
  }

  deduplicate(candidates: Hypothesis[], existing: Hypothesis[]): Hypothesis[] {
    return deduplicate(candidates, existing, this.config.dedupThreshold)
  }

  rank(tree: ResearchTree, candidates: Hypothesis[]): Hypothesis[] {
    const strengths = this.normalizedStrengths(candidates, tree)
    return [...candidates].sort((left, right) => {
      const leftScore = this.score(left, tree, strengths)
      const rightScore = this.score(right, tree, strengths)
      if (rightScore !== leftScore) return rightScore - leftScore
      return (left.order ?? 0) - (right.order ?? 0)
    })
  }

  private normalizedStrengths(
    candidates: Hypothesis[],
    tree: ResearchTree
  ): Map<string, number> {
    const priorities = new Map<string, number>()
    for (const hypothesis of candidates) {
      if (hypothesis.id !== null) {
        priorities.set(hypothesis.id, this.policy.priority(hypothesis, tree))
      }
    }
    const values = [...priorities.values()].filter((value) => Number.isFinite(value))
    const low = values.length > 0 ? Math.min(...values) : 0.0
    const high = values.length > 0 ? Math.max(...values) : 0.0
    const span = high - low
    const normalized = new Map<string, number>()
    for (const [id, priority] of priorities) {
      normalized.set(id, span <= 0 ? 0.5 : (priority - low) / span)
    }
    return normalized
  }

  private score(
    hypothesis: Hypothesis,
    tree: ResearchTree,
    strengths: Map<string, number>
  ): number {
    const config = this.config
    const prior = rubricPrior(hypothesis)
    const strength = hypothesis.id === null ? 0.5 : (strengths.get(hypothesis.id) ?? 0.5)
    const explore = novelty(hypothesis, tree)
    const cost = Math.min(Math.max(hypothesis.cost, 0.0), 1.0)
    return (
      config.priorWeight * prior +
      config.strengthWeight * strength +
      config.noveltyWeight * explore -
      config.costWeight * cost
    )
  }
}
