/**
 * LLM worker 抽象：用 M1 @athena/agent 的 Agent/ResponsesProvider 直接运行 worker
 * 结构化 turn，替代 M4 AgentRuntime 门面。文本调用使用 singleTurnChat。
 */

import { randomBytes } from "node:crypto"
import { z } from "zod"
import {
  Agent,
  AgentContext,
  ContextManager,
  ResponsesProvider,
  ToolRegistry,
  type StructuredOutputType,
} from "@athena/agent"
import {
  AthenaThreadSchema,
  AthenaTurnSchema,
  HypothesisBatchSchema,
  type ArtifactStore,
} from "@athena/core"

import { PlanDecisionSchema, type PlanDecision } from "./supervisor/plans.js"

/** 把 zod schema 适配为 M1 Agent 的 StructuredOutputType。 */
export function zodOutputType<T>(name: string, schema: z.ZodType<T>): StructuredOutputType {
  return {
    name,
    modelJsonSchema() {
      return { type: "object" }
    },
    parseJson(text: string) {
      return schema.parse(JSON.parse(text))
    },
    toJson(value: unknown) {
      return JSON.stringify(value)
    },
  }
}

export const PlanDecisionOutputType = zodOutputType("PlanDecision", PlanDecisionSchema)

export const HypothesisBatchOutputType = zodOutputType("HypothesisBatch", HypothesisBatchSchema)

export interface WorkerRunnerOptions {
  store: ArtifactStore
  model: string
  client?: unknown
}

export class WorkerRunner {
  private readonly store: ArtifactStore
  private readonly provider: ResponsesProvider

  constructor(opts: WorkerRunnerOptions) {
    this.store = opts.store
    this.provider = new ResponsesProvider(opts.model, { client: opts.client as never })
  }

  /** 运行一次结构化输出 turn，返回已解析的结构化结果。 */
  async runStructured<T>(
    prompt: string,
    outputType: StructuredOutputType,
    opts: { systemPrompt?: string; memory?: ContextManager | null; tools?: ToolRegistry | null } = {}
  ): Promise<T> {
    const tools = opts.tools ?? new ToolRegistry()
    const agent = new Agent(this.provider, tools, opts.systemPrompt ?? "", null, {
      outputType,
      artifacts: this.store,
    })
    const memory = opts.memory ?? new ContextManager()
    const id = randomBytes(8).toString("hex")
    const thread = AthenaThreadSchema.parse({
      thread_id: `thread:${id}`,
      session_id: `session:${id}`,
      status: "running",
      context_ref: `context:${id}`,
    })
    const turn = AthenaTurnSchema.parse({
      turn_id: `turn:${id}`,
      thread_id: thread.thread_id,
      request_ref: prompt,
      status: "running",
    })
    const ctx = new AgentContext(
      thread,
      turn,
      async () => {},
      tools,
      new AbortController().signal,
      memory,
      prompt
    )
    const outcome = await agent.run(ctx)
    const json = await this.store.getText(outcome.resultRef)
    return outputType.parseJson(json) as T
  }

  /** 运行一次 PlanDecision turn（无记忆复用时用单轮）。 */
  async planDecision(prompt: string): Promise<PlanDecision | null> {
    try {
      return await this.runStructured<PlanDecision>(prompt, PlanDecisionOutputType)
    } catch {
      return null
    }
  }
}
