import { readFileSync } from "node:fs"
import { z } from "zod"
import { atomicWriteJson, parseOrThrow } from "@athena/core"
import type { PaperSpec } from "./run-spec.js"

export type AutoResearchStatus = "RUNNING" | "WAITING" | "STOPPED" | "FAILED" | "COMPLETED"

const AutoResearchStateSchema = z.strictObject({
  version: z.literal(1),
  run_id: z.string().min(1),
  phase: z.string().min(1),
  stage_id: z.string().min(1),
  status: z.enum(["RUNNING", "WAITING", "STOPPED", "FAILED", "COMPLETED"]),
  paper_spec: z.record(z.string(), z.unknown()),
  stage: z
    .strictObject({
      phase: z.string(),
      round: z.number().int().min(0),
      used_rounds: z.number().int().min(0),
      budget: z.record(z.string(), z.unknown()).default({}),
    })
    .default({ phase: "INTAKE", round: 0, used_rounds: 0, budget: {} }),
  budgets: z.record(z.string(), z.unknown()),
  started_at: z.string(),
  updated_at: z.string(),
  deadline: z.string(),
  stopped_by: z.string().optional(),
  gate_results: z.array(z.record(z.string(), z.unknown())).default([]),
})

export type AutoResearchState = z.infer<typeof AutoResearchStateSchema>

export interface CreateStateInput {
  run_id: string
  phase: string
  stage_id: string
  status: AutoResearchStatus
  paper_spec: PaperSpec
  budgets: Record<string, unknown>
  started_at: string
  updated_at: string
  deadline: string
  stopped_by?: string
}

export class AutoResearchStateStore {
  constructor(private readonly path: string) {}

  load(): AutoResearchState {
    return AutoResearchStateSchema.parse(JSON.parse(readFileSync(this.path, "utf-8")))
  }

  save(state: AutoResearchState): string {
    return atomicWriteJson(this.path, state)
  }

  create(input: CreateStateInput): AutoResearchState {
    return AutoResearchStateSchema.parse({
      version: 1,
      run_id: input.run_id,
      phase: input.phase,
      stage_id: input.stage_id,
      status: input.status,
      paper_spec: input.paper_spec,
      budgets: input.budgets,
      started_at: input.started_at,
      updated_at: input.updated_at,
      deadline: input.deadline,
      stopped_by: input.stopped_by,
    })
  }

  setPhase(state: AutoResearchState, phase: string, stageId = phase): AutoResearchState {
    return AutoResearchStateSchema.parse({
      ...state,
      phase,
      stage_id: stageId,
      updated_at: new Date().toISOString(),
    })
  }

  setStatus(state: AutoResearchState, status: AutoResearchStatus, stoppedBy?: string): AutoResearchState {
    return AutoResearchStateSchema.parse({
      ...state,
      status,
      updated_at: new Date().toISOString(),
      stopped_by: stoppedBy ?? state.stopped_by,
    })
  }

  setGateResults(state: AutoResearchState, gateResults: Array<Record<string, unknown>>): AutoResearchState {
    return AutoResearchStateSchema.parse({
      ...state,
      gate_results: gateResults,
      updated_at: new Date().toISOString(),
    })
  }
}

export function parseAutoResearchState(data: unknown): AutoResearchState {
  return parseOrThrow(AutoResearchStateSchema, data)
}
