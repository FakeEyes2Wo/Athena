/** Hypothesis scoring policy, ranking and selection-local deduplication. */

import type { Hypothesis, ResearchTree } from "@athena/core"

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
  priority(hypothesis: Hypothesis): number
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

  constructor(private readonly k: number = 32.0) {
    if (!Number.isFinite(k) || k <= 0) {
      throw new Error("Elo k must be a positive finite number")
    }
  }

  seed(parent: Hypothesis | null): number {
    return parent === null ? EloPolicy.ROOT_PRIORITY : parent.priority
  }

  priority(hypothesis: Hypothesis): number {
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

export function tokenize(text: string): Set<string> {
  return new Set(text.toLowerCase().match(/[a-z0-9]+/g) ?? [])
}

export function jaccard(left: Set<string>, right: Set<string>): number {
  if (left.size === 0 && right.size === 0) return 0.0
  let intersection = 0
  for (const token of left) {
    if (right.has(token)) intersection += 1
  }
  return intersection / (left.size + right.size - intersection)
}

function tokens(hypothesis: Hypothesis): Set<string> {
  return tokenize(hypothesis.statement + " " + hypothesis.intervention)
}

/** Evidence and specificity provide the unchanged [0.4, 1.0] cold-start prior. */
function rubricPrior(hypothesis: Hypothesis): number {
  const evidence = hypothesis.sources.length > 0 ? 1.0 : 0.0
  const specific = tokenize(hypothesis.intervention).size >= 3 ? 1.0 : 0.0
  return 0.4 + 0.3 * evidence + 0.3 * specific
}

/** Greedy selection: input order wins ties; neither input is mutated. */
export function deduplicate(
  candidates: Hypothesis[],
  existing: Hypothesis[],
  threshold: number
): Hypothesis[] {
  const kept: Hypothesis[] = []
  const seen = existing.map(tokens)
  for (const candidate of candidates) {
    const words = tokens(candidate)
    if (seen.some((other) => jaccard(words, other) >= threshold)) continue
    kept.push(candidate)
    seen.push(words)
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

/** Rank by prior + normalized policy strength + novelty - clipped cost. */
export class Selector {
  constructor(
    private readonly policy: Pick<HypothesisPolicy, "priority">,
    private readonly config: RankConfig = DEFAULT_RANK_CONFIG
  ) {
    const weights = [config.priorWeight, config.strengthWeight, config.noveltyWeight]
    if (weights.some((weight) => !Number.isFinite(weight) || weight < 0 || weight > 1)) {
      throw new Error("selector weights must be between 0 and 1")
    }
    if (!Number.isFinite(config.costWeight) || config.costWeight < 0) {
      throw new Error("cost_weight must be finite and nonnegative")
    }
    if (!Number.isFinite(config.dedupThreshold) || config.dedupThreshold <= 0 || config.dedupThreshold > 1) {
      throw new Error("dedup_threshold must be in (0, 1]")
    }
  }

  deduplicate(candidates: Hypothesis[], existing: Hypothesis[]): Hypothesis[] {
    return deduplicate(candidates, existing, this.config.dedupThreshold)
  }

  rank(tree: ResearchTree, candidates: Hypothesis[]): Hypothesis[] {
    const priorities = new Map<string, number>()
    for (const hypothesis of candidates) {
      if (hypothesis.id !== null) {
        priorities.set(hypothesis.id, this.policy.priority(hypothesis))
      }
    }
    const values = [...priorities.values()].filter(Number.isFinite)
    const low = values.length > 0 ? Math.min(...values) : 0.0
    const high = values.length > 0 ? Math.max(...values) : 0.0
    const span = high - low
    const settled = Object.values(tree.toDict().hypotheses)
      .filter((hypothesis) => hypothesis.status !== "PROPOSED")
      .map(tokens)
    const config = this.config
    const scored = candidates.map((hypothesis) => {
      const strength = hypothesis.id === null || span <= 0
        ? 0.5
        : (priorities.get(hypothesis.id)! - low) / span
      const words = tokens(hypothesis)
      let similarity = 0
      for (const other of settled) similarity = Math.max(similarity, jaccard(words, other))
      const cost = Math.min(Math.max(hypothesis.cost, 0.0), 1.0)
      const score =
        config.priorWeight * rubricPrior(hypothesis) +
        config.strengthWeight * strength +
        config.noveltyWeight * (1 - similarity) -
        config.costWeight * cost
      return { hypothesis, score }
    })
    scored.sort((left, right) => {
      if (right.score !== left.score) return right.score - left.score
      return (left.hypothesis.order ?? 0) - (right.hypothesis.order ?? 0)
    })
    return scored.map(({ hypothesis }) => hypothesis)
  }
}
