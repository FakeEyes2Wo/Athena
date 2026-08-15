/**
 * Agent — 流式工具调用循环（移植 ``core/agent/runtime.py``）。
 */

import { readFileSync } from "node:fs"

import { isTransientError, type ArtifactStore, type AthenaThread, type AthenaTurn } from "@athena/core"

import { CancelledError, CancellationToken } from "../cancel.js"
import {
  modelRequest,
  modelResponse,
  systemPrompt,
  textPart,
  toolCallPart,
  toolReturnPart,
  userPrompt,
  type ModelResponsePart,
} from "../messages.js"
import { ContextManager } from "../memory/context-manager.js"
import { BaseTool, ToolRegistry } from "../tool.js"
import { ToolContext, ToolResult, type AskUser, type EmitEvent } from "../tool-types.js"
import { AgentConfig, AgentContext, AgentOutcome, StepOutcome, ToolCall } from "./models.js"
import type { StructuredOutputType } from "./models.js"
import { BaseProvider, createProvider } from "./provider.js"
import type { ChatClient } from "./settings.js"

export type { StructuredOutputType }

const MAX_STRUCTURED_RETRIES = 3
const MAX_STREAM_RETRIES = 5
const RETRY_BASE_DELAY = 1.0

export abstract class BaseAgent {
  abstract name: string
  abstract description: string
  abstract run(ctx: AgentContext): Promise<AgentOutcome>

  /** 按名称调用工具并返回 ToolResult。 */
  async tool(
    ctx: AgentContext,
    name: string,
    input: Record<string, unknown> = {}
  ): Promise<ToolResult> {
    return ctx.tools
      .resolve(name)
      .ainvoke(new ToolContext(name, `${ctx.turn.turn_id}:${name}`, ctx.emit, ctx.cancel), input)
  }
}

export class Agent extends BaseAgent {
  model: BaseProvider
  tools: ToolRegistry
  systemPrompt: string
  config: AgentConfig
  outputType: StructuredOutputType | null
  artifacts: ArtifactStore | null

  constructor(
    model: BaseProvider,
    tools: ToolRegistry,
    systemPrompt: string,
    config: AgentConfig | null = null,
    opts: {
      outputType?: StructuredOutputType | null
      artifacts?: ArtifactStore | null
    } = {}
  ) {
    super()
    this.model = model
    this.tools = tools
    this.systemPrompt = systemPrompt
    this.config = config ?? new AgentConfig()
    this.outputType = opts.outputType ?? null
    this.artifacts = opts.artifacts ?? null
  }

  get name(): string {
    return this.config.name
  }

  get description(): string {
    return `Agent: ${this.model.modelName}`
  }

  async run(ctx: AgentContext): Promise<AgentOutcome> {
    let mem = ctx.memory
    if (mem === null) {
      mem = new ContextManager()
      ctx.memory = mem
    }

    const user = loadInput(ctx)
    if (this.systemPrompt && !hasSystem(mem)) {
      mem.append(modelRequest([systemPrompt(this.systemPrompt)]))
    }
    if (user) {
      mem.append(modelRequest([userPrompt(user)]))
    }

    let retries = 0
    for (let i = 0; i < this.config.maxTurns; i++) {
      if (ctx.cancel.isSet()) break
      const outcome = await samplingLoop(this, ctx)
      if (outcome.kind === "done") {
        if (this.outputType !== null) {
          let instance: unknown
          try {
            instance = this.outputType.parseJson(outcome.text)
          } catch (exc) {
            if (retries >= MAX_STRUCTURED_RETRIES) {
              throw new Error(
                `structured output invalid after retries: ${errMessage(exc)}`
              )
            }
            retries += 1
            mem.append(
              modelRequest([
                userPrompt(
                  `Previous JSON output was invalid: ${errMessage(exc)}\nReturn JSON matching the schema.`
                ),
              ])
            )
            continue
          }
          const jsonText = this.outputType.toJson(instance)
          const ref =
            this.artifacts !== null
              ? await this.artifacts.putText(jsonText)
              : `result://${ctx.turn.turn_id}`
          return new AgentOutcome(ref, `context://${ctx.turn.turn_id}/next`)
        }
        return new AgentOutcome(`result://${ctx.turn.turn_id}`, `context://${ctx.turn.turn_id}/next`)
      }
      if (outcome.kind === "error") {
        throw new Error(outcome.text || "provider stream failed")
      }
    }

    if (this.outputType !== null) {
      throw new Error("max turns exhausted without structured output")
    }
    return new AgentOutcome(`result://${ctx.turn.turn_id}`, `context://${ctx.turn.turn_id}/next`)
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function errName(exc: unknown): string {
  return exc instanceof Error ? exc.constructor.name : typeof exc
}

function errMessage(exc: unknown): string {
  return exc instanceof Error ? exc.message : String(exc)
}

/** 未知工具名 → 可恢复的工具错误，列出已注册工具供同一 turn 重试。 */
async function unknownToolResult(name: string, available: string[]): Promise<ToolResult> {
  return new ToolResult(
    { available_tools: available },
    false,
    `unknown tool: ${name}; available_tools=${available}`
  )
}

/** 取消并等待所有在途工具任务（JS Promise 不可取消，退化为等待完成）。 */
async function cancelToolTasks(toolTasks: Array<Promise<unknown> | null>): Promise<void> {
  const pending = toolTasks.filter((t): t is Promise<unknown> => t !== null)
  if (pending.length) await Promise.allSettled(pending)
}

async function samplingLoop(agent: Agent, ctx: AgentContext): Promise<StepOutcome> {
  let retriesLeft = MAX_STREAM_RETRIES
  while (true) {
    const [outcome, transient] = await sampleOnce(agent, ctx)
    if (!transient || retriesLeft <= 1) return outcome
    retriesLeft -= 1
    const delay =
      RETRY_BASE_DELAY *
      2 ** (MAX_STREAM_RETRIES - retriesLeft) *
      (0.5 + Math.random())
    await sleep(delay * 1000)
  }
}

async function sampleOnce(agent: Agent, ctx: AgentContext): Promise<[StepOutcome, boolean]> {
  const mem = ctx.memory
  if (mem === null) throw new Error("assert: memory is null")

  const toolCalls: ToolCall[] = []
  const toolTasks: Array<Promise<unknown> | null> = []
  let serialBarrier: Promise<unknown> | null = null
  let text = ""
  let hadCalls = false

  try {
    const stream = agent.model.stream(agent.config, agent.tools, mem.items, ctx.cancel, {
      outputType: agent.outputType,
    })
    for await (const event of stream) {
      if (event.kind === "text_delta") {
        const accumulated = event.data["accumulated"]
        text =
          typeof accumulated === "string"
            ? accumulated
            : text + (typeof event.data["delta"] === "string" ? event.data["delta"] : "")
        await ctx.emit("agent/text_delta", `event:${ctx.turn.turn_id}`, event.data)
      } else if (event.kind === "function_call") {
        hadCalls = true
        const tc = new ToolCall(
          String(event.data["call_id"] ?? ""),
          String(event.data["name"] ?? ""),
          (event.data["arguments"] as Record<string, unknown>) ?? {}
        )
        await ctx.emit("agent/function_call", `event:${ctx.turn.turn_id}:${tc.callId}`, {
          name: tc.name,
          arguments: tc.args,
        })
        const idx = toolCalls.length
        toolCalls.push(tc)
        toolTasks.push(null)

        let tool: BaseTool
        try {
          tool = agent.tools.resolve(tc.name)
        } catch {
          toolTasks[idx] = unknownToolResult(
            tc.name,
            agent.tools.specs.map((s) => s.name)
          )
          continue
        }
        const tctx = new ToolContext(
          tc.name,
          `${ctx.turn.turn_id}:${tc.name}`,
          ctx.emit,
          ctx.cancel,
          ctx.askUser
        )
        const task = dispatchToolCall(tool, tctx, tc, idx, toolTasks, serialBarrier)
        toolTasks[idx] = task
        if (!tool.spec.concurrencySafe) serialBarrier = task
      } else if (event.kind === "response_completed") {
        break
      } else if (event.kind === "error") {
        await cancelToolTasks(toolTasks)
        const transient = !hadCalls && isTransientError(event.data["message"] ?? "")
        return [
          new StepOutcome("error", String(event.data["message"] ?? "")),
          transient,
        ]
      }
    }
  } catch (exc) {
    if (exc instanceof CancelledError) {
      await cancelToolTasks(toolTasks)
      throw exc
    }
    await cancelToolTasks(toolTasks)
    const transient = !hadCalls && isTransientError(exc)
    return [new StepOutcome("error", `${errName(exc)}: ${errMessage(exc)}`), transient]
  }

  const outcome = await finalizeStep(mem, toolCalls, toolTasks, text, hadCalls)
  return [outcome, false]
}

async function finalizeStep(
  mem: ContextManager,
  toolCalls: ToolCall[],
  toolTasks: Array<Promise<unknown> | null>,
  text: string,
  hadCalls: boolean
): Promise<StepOutcome> {
  const results: unknown[] = new Array(toolTasks.length).fill(undefined)
  const present = toolTasks.filter((t): t is Promise<unknown> => t !== null)
  if (present.length) {
    const gathered = await Promise.allSettled(present)
    let gi = 0
    for (let i = 0; i < toolTasks.length; i++) {
      const t = toolTasks[i]
      if (t !== null) {
        const r = gathered[gi]!
        results[i] = r.status === "fulfilled" ? r.value : r.reason
        gi++
      }
    }
  }

  if (hadCalls && toolCalls.length) {
    const parts: ModelResponsePart[] = []
    if (text) parts.push(textPart(text))
    for (const tc of toolCalls) {
      parts.push(toolCallPart(tc.name, JSON.stringify(tc.args), tc.callId))
    }
    mem.append(modelResponse(parts))
  }

  for (let i = 0; i < toolCalls.length; i++) {
    const tc = toolCalls[i]!
    let content = toStr(results[i])
    if (content.length > 50_000) {
      content = content.slice(0, 24_950) + "\n...[TRUNCATED]...\n" + content.slice(-24_950)
    }
    mem.append(modelRequest([toolReturnPart(tc.name, content, tc.callId)]))
  }

  if (!hadCalls && text) {
    mem.append(modelResponse([textPart(text)]))
    return new StepOutcome("done", text)
  }
  if (hadCalls) {
    return new StepOutcome("continue")
  }
  return new StepOutcome("done", text)
}

function dispatchToolCall(
  tool: BaseTool,
  tctx: ToolContext,
  tc: ToolCall,
  idx: number,
  toolTasks: Array<Promise<unknown> | null>,
  serialBarrier: Promise<unknown> | null
): Promise<unknown> {
  if (tool.spec.concurrencySafe) {
    const runSafe = async (): Promise<unknown> => {
      if (serialBarrier !== null) await serialBarrier
      return tool.ainvoke(tctx, tc.args)
    }
    return runSafe()
  }
  const previous = toolTasks.slice(0, idx).filter((t): t is Promise<unknown> => t !== null)
  const runSerial = async (): Promise<unknown> => {
    if (previous.length) await Promise.allSettled(previous)
    return tool.ainvoke(tctx, tc.args)
  }
  return runSerial()
}

export type AgentRunnerFn = ((thread: AthenaThread, turn: AthenaTurn, emit: EmitEvent) => Promise<AgentOutcome>) & {
  runWithContext: (
    thread: AthenaThread,
    turn: AthenaTurn,
    emit: EmitEvent,
    memory: ContextManager | null,
    cancel: CancellationToken
  ) => Promise<AgentOutcome>
}

/** 将 BaseAgent 适配为两套 Runner 签名，向后兼容 ThreadRuntime。 */
export function agentRunner(
  agent: BaseAgent,
  tools: ToolRegistry,
  opts: { askUser?: (thread: AthenaThread, turn: AthenaTurn) => AskUser | null } = {}
): AgentRunnerFn {
  const bind = (thread: AthenaThread, turn: AthenaTurn): AskUser | null =>
    opts.askUser ? opts.askUser(thread, turn) : null

  const run = async (
    thread: AthenaThread,
    turn: AthenaTurn,
    emit: EmitEvent
  ): Promise<AgentOutcome> => {
    const ctx = new AgentContext(
      thread,
      turn,
      emit,
      tools,
      new CancellationToken(),
      null,
      null,
      [],
      bind(thread, turn)
    )
    return agent.run(ctx)
  }

  const runWithContext = async (
    thread: AthenaThread,
    turn: AthenaTurn,
    emit: EmitEvent,
    memory: ContextManager | null,
    cancel: CancellationToken
  ): Promise<AgentOutcome> => {
    const ctx = new AgentContext(thread, turn, emit, tools, cancel, memory, null, [], bind(thread, turn))
    return agent.run(ctx)
  }

  return Object.assign(run, { runWithContext })
}

export function createAgent(
  model: string,
  tools: ToolRegistry,
  systemPrompt: string,
  opts: {
    client?: ChatClient | null
    maxTurns?: number
    maxTokens?: number
    temperature?: number
    name?: string
  } = {}
): Agent {
  const provider = createProvider(model, { client: opts.client ?? null })
  const config = new AgentConfig(
    opts.maxTurns ?? 200,
    opts.maxTokens ?? 4096,
    opts.temperature ?? 0.1,
    opts.name ?? "agent"
  )
  return createCodeAgent(provider, tools, systemPrompt, config)
}

export function createCodeAgent(
  model: BaseProvider,
  tools: ToolRegistry,
  systemPrompt: string,
  config: AgentConfig | null = null
): Agent {
  return new Agent(model, tools, systemPrompt, config)
}

function toStr(r: unknown): string {
  if (r instanceof ToolResult) {
    if (!r.success) return r.error ? `[ERROR] ${r.error}` : "[ERROR]"
    if (r.data === null || r.data === undefined) return "[OK]"
    return typeof r.data === "object" ? JSON.stringify(r.data) : String(r.data)
  }
  if (r instanceof Error) return `[ERROR] ${errName(r)}: ${errMessage(r)}`
  return String(r)
}

function loadInput(ctx: AgentContext): string {
  if (ctx.inputText !== null) return ctx.inputText
  const ref = ctx.turn.request_ref
  if (ref && ref.startsWith("artifact://")) {
    try {
      return readFileSync(ref.slice("artifact://".length), "utf8")
    } catch {
      // 文件不存在、权限不足或编码错误时回退到原始 ref
    }
  }
  return ref || ""
}

function hasSystem(cm: ContextManager): boolean {
  for (const msg of cm.items) {
    if (msg.kind === "request" && msg.parts.some((p) => p.part_kind === "system-prompt")) {
      return true
    }
  }
  return false
}
