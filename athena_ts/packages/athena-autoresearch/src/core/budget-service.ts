import type { Budgets } from "../schemas/run-spec.js"
import type { HypothesisPoolLike } from "./pipeline-types.js"

export class BudgetService {
  private tokensUsed = 0

  constructor(
    private readonly budgets: Budgets,
    private readonly startedAt: number = Date.now(),
  ) {}

  get maxTokens(): number {
    return this.budgets.max_tokens
  }

  get tokens_used(): number {
    return this.tokensUsed
  }

  addTokens(n: number): void {
    this.tokensUsed += n
  }

  canSpendTokens(n: number): boolean {
    return this.budgets.max_tokens === 0 || this.tokensUsed + n <= this.budgets.max_tokens
  }

  ideasRemaining(pool: HypothesisPoolLike): number {
    return Math.max(0, this.budgets.max_ideas - pool.countByOrigin("ideation"))
  }

  timeLimitReached(): boolean {
    return Date.now() >= this.startedAt + parseDurationMs(this.budgets.project_time_limit)
  }

  deadline(): number {
    return this.startedAt + parseDurationMs(this.budgets.project_time_limit)
  }
}

export function parseDurationMs(iso: string): number {
  const match = /^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?$/.exec(iso)
  if (!match) {
    throw new Error(`unsupported duration: ${iso}`)
  }
  const hours = match[1] ? Number(match[1]) : 0
  const minutes = match[2] ? Number(match[2]) : 0
  const seconds = match[3] ? Number(match[3]) : 0
  return hours * 3_600_000 + minutes * 60_000 + seconds * 1000
}
