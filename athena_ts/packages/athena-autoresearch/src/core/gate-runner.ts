import type { StageResult } from "./pipeline-types.js"

export interface Gate {
  readonly id: string
  readonly retryable: boolean
  check(ctx: unknown, stageResult: StageResult): Promise<GateResult>
}

export interface GateResult {
  pass: boolean
  blockingFactor?: string
  itemScores: Array<{ item: string; score: 0 | 1; evidence: string }>
  feedback: string[]
}

export class GateRunner {
  private readonly gates = new Map<string, Gate>()

  register(gate: Gate): void {
    if (this.gates.has(gate.id)) {
      throw new Error(`duplicate gate: ${gate.id}`)
    }
    this.gates.set(gate.id, gate)
  }

  has(id: string): boolean {
    return this.gates.has(id)
  }

  async run(
    gateRefs: readonly string[],
    ctx: unknown,
    stageResult: StageResult,
  ): Promise<GateResult[]> {
    const results: GateResult[] = []
    for (const ref of gateRefs) {
      const gate = this.gates.get(ref)
      if (!gate) {
        throw new Error(`unknown gate: ${ref}`)
      }
      results.push(await gate.check(ctx, stageResult))
    }
    return results
  }
}
