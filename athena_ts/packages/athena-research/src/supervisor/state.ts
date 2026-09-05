/**
 * 自治 Supervisor 的 ``state.json`` 原子持久化（移植 ``research/supervisor/state.py``）。
 */

import { readFileSync } from "node:fs"
import { z } from "zod"
import { atomicWriteJson, parseOrThrow } from "@athena/core"

import { PlanStateSchema, planStateToJSON } from "./plans.js"

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

/** Schema owns the durable fields; state is plain data, not a service instance. */
export type ResearchState = z.infer<typeof ResearchStateSchema>
export type ResearchStatus = ResearchState["status"]

export function parseResearchState(data: unknown): ResearchState {
  return parseOrThrow(ResearchStateSchema, data)
}

/** Project only durable fields and normalize Plan serialization. */
export function researchStateToJSON(state: ResearchState): Record<string, unknown> {
  return {
    status: state.status,
    phase: state.phase,
    search_limit: state.search_limit,
    concurrency: state.concurrency,
    ideator_count: state.ideator_count,
    hypotheses_per_ideator: state.hypotheses_per_ideator,
    manual_mode: state.manual_mode,
    plans: Object.fromEntries(
      Object.entries(state.plans).map(([id, plan]) => [id, planStateToJSON(plan)])
    ),
    validation: state.validation,
    eda_dir: state.eda_dir,
    task_understanding: state.task_understanding,
  }
}

export function saveResearchState(path: string, state: ResearchState): string {
  return atomicWriteJson(path, researchStateToJSON(state))
}

/** Load and validate once, retaining the explicit root-shape error. */
export function loadResearchState(path: string): ResearchState {
  const payload = JSON.parse(readFileSync(path, "utf-8"))
  if (typeof payload !== "object" || payload === null || Array.isArray(payload)) {
    throw new Error("research state payload must be an object")
  }
  return parseResearchState(payload)
}
