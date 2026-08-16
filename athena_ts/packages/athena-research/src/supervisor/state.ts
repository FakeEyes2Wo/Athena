/**
 * 自治 Supervisor 的 ``state.json`` 原子持久化（移植 ``research/supervisor/state.py``）。
 */

import { readFileSync } from "node:fs"
import { z } from "zod"
import { atomicWriteJson, parseOrThrow } from "@athena/core"

import { PlanStateSchema, planStateToJSON, type PlanState } from "./plans.js"

export type ResearchStatus = "RUNNING" | "WAITING" | "COMPLETED" | "STOPPED" | "FAILED"
export type ResearchPhase = "PREPARE" | "SEARCH" | "VALIDATE" | "COMPLETED"

const ResearchStateSchema = z
  .strictObject({
    status: z.enum(["RUNNING", "WAITING", "COMPLETED", "STOPPED", "FAILED"]),
    phase: z.enum(["PREPARE", "SEARCH", "VALIDATE", "COMPLETED"]),
    search_limit: z.number().int().min(0),
    concurrency: z.number().int().min(1),
    ideator_count: z.number().int().min(1).max(8).default(3),
    hypotheses_per_ideator: z.number().int().min(1).max(5).default(2),
    manual_mode: z.boolean().default(false),
    plans: z.record(z.string(), PlanStateSchema).default({}),
    validation: z.record(z.string(), z.unknown()).nullable().default(null),
    eda_dir: z.string().nullable().default(null),
    task_understanding: z.record(z.string(), z.unknown()).nullable().default(null),
  })
  .superRefine((state, ctx) => {
    for (const [planId, plan] of Object.entries(state.plans)) {
      if (plan.kind === "PREPARE" && planId !== "prepare") {
        ctx.addIssue({ code: "custom", message: "PREPARE plan key must be 'prepare'" })
      }
      if (plan.kind === "VALIDATE" && planId !== "validate") {
        ctx.addIssue({ code: "custom", message: "VALIDATE plan key must be 'validate'" })
      }
      if (
        plan.kind === "SEARCH" &&
        (!planId.trim() || planId === "prepare" || planId === "validate")
      ) {
        ctx.addIssue({ code: "custom", message: "SEARCH plan key must be a Hypothesis ID" })
      }
    }
  })

type ResearchStateInput = z.input<typeof ResearchStateSchema>

/** 未完成自治研究的小型持久化检查点。 */
export class ResearchState {
  status: ResearchStatus
  phase: ResearchPhase
  search_limit: number
  concurrency: number
  ideator_count: number
  hypotheses_per_ideator: number
  manual_mode: boolean
  plans: Record<string, PlanState>
  validation: Record<string, unknown> | null
  eda_dir: string | null
  task_understanding: Record<string, unknown> | null

  constructor(input: ResearchStateInput) {
    const parsed = parseOrThrow(ResearchStateSchema, input)
    this.status = parsed.status
    this.phase = parsed.phase
    this.search_limit = parsed.search_limit
    this.concurrency = parsed.concurrency
    this.ideator_count = parsed.ideator_count
    this.hypotheses_per_ideator = parsed.hypotheses_per_ideator
    this.manual_mode = parsed.manual_mode
    this.plans = parsed.plans
    this.validation = parsed.validation
    this.eda_dir = parsed.eda_dir
    this.task_understanding = parsed.task_understanding
  }

  static parse(data: unknown): ResearchState {
    return new ResearchState(parseOrThrow(ResearchStateSchema, data))
  }

  toJSON(): Record<string, unknown> {
    return {
      status: this.status,
      phase: this.phase,
      search_limit: this.search_limit,
      concurrency: this.concurrency,
      ideator_count: this.ideator_count,
      hypotheses_per_ideator: this.hypotheses_per_ideator,
      manual_mode: this.manual_mode,
      plans: Object.fromEntries(
        Object.entries(this.plans).map(([id, plan]) => [id, planStateToJSON(plan)])
      ),
      validation: this.validation,
      eda_dir: this.eda_dir,
      task_understanding: this.task_understanding,
    }
  }

  /** 原子替换 ``path`` 为当前已校验状态。 */
  save(path: string): string {
    return atomicWriteJson(path, this.toJSON())
  }

  /** 从 JSON 加载并校验状态；根须为对象。 */
  static load(path: string): ResearchState {
    const payload = JSON.parse(readFileSync(path, "utf-8"))
    if (typeof payload !== "object" || payload === null || Array.isArray(payload)) {
      throw new Error("research state payload must be an object")
    }
    return ResearchState.parse(payload)
  }
}
