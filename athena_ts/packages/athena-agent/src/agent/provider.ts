/**
 * Responses 流式 Provider — OpenAI 兼容 Chat Completions streaming（移植 ``core/agent/provider.py``）。
 */

import type { ModelMessage } from "../messages.js"
import type { ToolRegistry } from "../tool.js"
import { truncateText } from "../tool-types.js"
import type { AgentConfig, StructuredOutputType } from "./models.js"
/** Minimal injected chat-completions transport. */
export interface ChatClient {
  chat: {
    completions: {
      create(kwargs: Record<string, unknown>): Promise<AsyncIterable<unknown>>
    }
  }
}

const ALLOWED_PROVIDERS = ["deepseek", "openai", "anthropic"] as const
export type ProviderKind = (typeof ALLOWED_PROVIDERS)[number]

function providerKind(): ProviderKind {
  const kind = process.env.LLM_PROVIDER || "deepseek"
  if (!ALLOWED_PROVIDERS.includes(kind as ProviderKind)) {
    throw new Error(`unsupported LLM_PROVIDER=${JSON.stringify(kind)}; expected one of ${ALLOWED_PROVIDERS.join(", ")}`)
  }
  return kind as ProviderKind
}

/** Preserve the existing deferred transport; actual clients are injected by callers. */
function getClient(): ChatClient {
  if (!(process.env.DEEPSEEK_API_KEY || process.env.OPENAI_API_KEY)) {
    throw new Error("Missing LLM API key: set DEEPSEEK_API_KEY or OPENAI_API_KEY in .env")
  }
  return {
    chat: {
      completions: {
        async create(): Promise<AsyncIterable<unknown>> {
          throw new Error("real OpenAI client wiring deferred to M2")
        },
      },
    },
  }
}

/** 流式响应事件 — kind 区分文本增量、函数调用、完成和错误四种类型。 */
export class StreamEvent {
  constructor(
    public readonly kind: "text_delta" | "function_call" | "response_completed" | "error",
    public readonly data: Record<string, unknown> = {}
  ) {}
}

const DSML_KINDS = ["tool_use_error", "tool_calls", "tool_call", "function_calls"]
const DSML_BARS = ["|", "｜"]

const DSML_OPEN_TOKENS = DSML_BARS.flatMap((bar) =>
  DSML_KINDS.map((kind) => `<${bar}DSML${bar}${kind}>`)
)
const DSML_CLOSE_TOKENS = DSML_BARS.flatMap((bar) =>
  DSML_KINDS.map((kind) => `</${bar}DSML${bar}${kind}>`)
)

function findEarliest(text: string, tokens: string[]): [number, string] | null {
  let best: [number, string] | null = null
  for (const token of tokens) {
    const index = text.indexOf(token)
    if (index !== -1 && (best === null || index < best[0])) best = [index, token]
  }
  return best
}

/** 剥离 deepseek 的 DSML tool-call 传输语法，防止泄漏进可见文本。 */
export class DeepSeekTextFilter {
  private buffer = ""
  private inside = false

  /** 喂入一段增量，返回可安全发出的干净文本。 */
  push(chunk: string): string {
    this.buffer += chunk
    return this.consume(false)
  }

  /** 结束流：返回剩余干净文本，丢弃未闭合的 DSML 块。 */
  flush(): string {
    return this.consume(true)
  }

  private consume(final: boolean): string {
    const out: string[] = []
    const maxOpen = Math.max(...DSML_OPEN_TOKENS.map((t) => t.length))
    const maxClose = Math.max(...DSML_CLOSE_TOKENS.map((t) => t.length))
    while (this.buffer) {
      if (this.inside) {
        const close = findEarliest(this.buffer, DSML_CLOSE_TOKENS)
        if (close !== null) {
          const [index, token] = close
          this.buffer = this.buffer.slice(index + token.length)
          this.inside = false
          continue
        }
        const keep = final ? 0 : Math.min(this.buffer.length, maxClose - 1)
        this.buffer = this.buffer.slice(this.buffer.length - keep)
        if (final) this.inside = false
        return out.join("")
      }
      const opened = findEarliest(this.buffer, DSML_OPEN_TOKENS)
      if (opened !== null) {
        const [index, token] = opened
        if (index) out.push(this.buffer.slice(0, index))
        this.buffer = this.buffer.slice(index + token.length)
        this.inside = true
        continue
      }
      if (final) {
        out.push(this.buffer)
        this.buffer = ""
        return out.join("")
      }
      const emitLen = this.buffer.length - Math.min(this.buffer.length, maxOpen - 1)
      if (emitLen <= 0) return out.join("")
      out.push(this.buffer.slice(0, emitLen))
      this.buffer = this.buffer.slice(emitLen)
      return out.join("")
    }
    return out.join("")
  }
}

export type { StructuredOutputType }

interface StreamChunk {
  choices?: Array<{
    delta?: {
      content?: string | null
      tool_calls?: Array<{
        index?: number
        id?: string | null
        function?: { name?: string | null; arguments?: string | null }
      }>
    }
    finish_reason?: string | null
  }>
}

export interface BaseProvider {
  readonly modelName: string
  readonly client: ChatClient
  stream(
    config: AgentConfig,
    tools: ToolRegistry,
    messages: ModelMessage[],
    cancel: AbortSignal,
    opts?: { outputType?: StructuredOutputType | null }
  ): AsyncGenerator<StreamEvent>
}

const ANTHROPIC_NOT_IMPLEMENTED =
  "native Anthropic provider is not yet implemented; set LLM_PROVIDER=deepseek|openai"

export class ResponsesProvider implements BaseProvider {
  private _client: ChatClient | null
  readonly providerKind: ProviderKind

  constructor(
    public readonly modelName: string,
    opts: { client?: ChatClient | null; providerKind?: ProviderKind | null } = {}
  ) {
    this._client = opts.client ?? null
    this.providerKind = opts.providerKind ?? providerKind()
  }

  get client(): ChatClient {
    if (this._client === null) this._client = getClient()
    return this._client
  }

  private responseFormat(outputType: StructuredOutputType): Record<string, unknown> | null {
    if (this.providerKind === "openai") {
      return {
        type: "json_schema",
        json_schema: { name: outputType.name, schema: outputType.modelJsonSchema() },
      }
    }
    if (this.providerKind === "deepseek") {
      return { type: "json_object" }
    }
    return null
  }

  private static assembleFunctionCalls(
    bufs: Map<number, { id: string; name: string; arguments: string }>
  ): StreamEvent[] {
    const events: StreamEvent[] = []
    for (const i of [...bufs.keys()].sort((a, b) => a - b)) {
      const b = bufs.get(i)!
      if (!(b.id && b.name)) continue
      let args: unknown = {}
      if (b.arguments) {
        try {
          args = JSON.parse(b.arguments)
        } catch {
          args = {}
        }
      }
      events.push(
        new StreamEvent("function_call", { call_id: b.id, name: b.name, arguments: args })
      )
    }
    return events
  }

  async *stream(
    config: AgentConfig,
    tools: ToolRegistry,
    messages: ModelMessage[],
    cancel: AbortSignal,
    opts: { outputType?: StructuredOutputType | null } = {}
  ): AsyncGenerator<StreamEvent> {
    const outputType = opts.outputType ?? null
    let apiMsgs = toApi(messages)
    if (this.providerKind === "deepseek" && outputType !== null) {
      apiMsgs = [...apiMsgs, schemaInstruction(outputType)]
    }
    const toolDefs = tools.specs.map((spec) => spec.toOpenaiTool())

    const kw: Record<string, unknown> = {
      model: this.modelName,
      messages: apiMsgs,
      max_tokens: config.maxTokens,
      temperature: config.temperature,
      stream: true,
      extra_body: { thinking: { type: "disabled" } },
    }
    if (outputType !== null) {
      const responseFormat = this.responseFormat(outputType)
      if (responseFormat !== null) kw["response_format"] = responseFormat
    }
    if (toolDefs.length) {
      kw["tools"] = toolDefs
      kw["tool_choice"] = config.toolChoice
    }

    let stream: AsyncIterable<StreamChunk>
    try {
      stream = (await this.client.chat.completions.create({ ...kw })) as AsyncIterable<StreamChunk>
    } catch (exc) {
      if (!isResponseFormatUnavailable(exc)) throw exc
      const retryKw = { ...kw }
      delete retryKw["response_format"]
      stream = (await this.client.chat.completions.create(retryKw)) as AsyncIterable<StreamChunk>
    }

    const bufs = new Map<number, { id: string; name: string; arguments: string }>()
    let finish = ""
    let text = ""
    const dsmlFilter = this.providerKind === "deepseek" ? new DeepSeekTextFilter() : null

    try {
      for await (const chunk of stream) {
        if (cancel.aborted) {
          yield new StreamEvent("error", { message: "cancelled" })
          return
        }
        for (const c of chunk.choices ?? []) {
          const d = c.delta ?? {}
          if (c.finish_reason) finish = c.finish_reason
          if (d.content) {
            const delta = dsmlFilter !== null ? dsmlFilter.push(d.content) : d.content
            if (delta) {
              text += delta
              yield new StreamEvent("text_delta", { delta, accumulated: text })
            }
          }
          if (d.tool_calls) {
            for (const t of d.tool_calls) {
              const i = t.index ?? 0
              if (!bufs.has(i)) bufs.set(i, { id: t.id ?? "", name: "", arguments: "" })
              const b = bufs.get(i)!
              if (t.id) b.id = t.id
              if (t.function) {
                if (t.function.name) b.name = t.function.name
                if (t.function.arguments) b.arguments += t.function.arguments
              }
            }
          }
        }

        if (finish === "tool_calls") {
          for (const event of ResponsesProvider.assembleFunctionCalls(bufs)) yield event
          finish = ""
          bufs.clear()
        }
      }
    } catch (exc) {
      yield new StreamEvent("error", {
        message: `${exc instanceof Error ? exc.constructor.name : typeof exc}: ${exc instanceof Error ? exc.message : String(exc)}`,
      })
      return
    }

    for (const event of ResponsesProvider.assembleFunctionCalls(bufs)) yield event

    if (dsmlFilter !== null) {
      const tail = dsmlFilter.flush()
      if (tail) {
        text += tail
        yield new StreamEvent("text_delta", { delta: tail, accumulated: text })
      }
    }

    yield new StreamEvent("response_completed", {
      finish_reason: finish || "stop",
      accumulated_text: text,
    })
  }
}

export function createProvider(
  model: string,
  opts: { client?: ChatClient | null } = {}
): ResponsesProvider {
  const kind = providerKind()
  if (kind === "anthropic") throw new Error(ANTHROPIC_NOT_IMPLEMENTED)
  return new ResponsesProvider(model, { ...opts, providerKind: kind })
}

function schemaInstruction(outputType: StructuredOutputType): Record<string, string> {
  return {
    role: "system",
    content:
      "Return a JSON object matching this schema:\n" +
      JSON.stringify(outputType.modelJsonSchema()),
  }
}

function isResponseFormatUnavailable(exc: unknown): boolean {
  const e = exc as { status?: unknown; status_code?: unknown; message?: string }
  const status =
    typeof e.status === "number"
      ? e.status
      : typeof e.status_code === "number"
        ? e.status_code
        : null
  const message = (e.message ?? String(exc)).toLowerCase()
  return (
    status === 400 &&
    message.includes("response_format") &&
    (message.includes("unavailable") || message.includes("unsupported"))
  )
}

/** 将 ModelMessage 列表转为 OpenAI API dict 格式。 */
function toApi(msgs: ModelMessage[]): Array<Record<string, unknown>> {
  const out: Array<Record<string, unknown>> = []
  for (const m of msgs) {
    if (m.kind === "request") {
      for (const p of m.parts) {
        const k = p.part_kind
        if (k === "system-prompt" || k === "user-prompt" || k === "text") {
          out.push({
            role: k === "system-prompt" ? "system" : "user",
            content: "content" in p ? String(p.content) : "",
          })
        } else if (k === "tool-return") {
          const c = truncateText(String("content" in p ? p.content : ""))
          out.push({
            role: "tool",
            tool_call_id: String(p.tool_call_id ?? ""),
            content: c,
          })
        }
      }
    } else {
      const parts = m.parts
      const tc = parts.filter((p) => p.part_kind === "tool-call")
      if (tc.length) {
        out.push({
          role: "assistant",
          content:
            parts
              .filter((p) => p.part_kind === "text")
              .map((p) => String("content" in p ? p.content : ""))
              .join("") || null,
          tool_calls: tc.map((p) => ({
            id: String(p.tool_call_id ?? ""),
            type: "function",
            function: {
              name: String(p.tool_name),
              arguments: String(p.args ?? "{}"),
            },
          })),
        })
      } else {
        out.push({
          role: "assistant",
          content: parts
            .filter((p) => "content" in p)
            .map((p) => String("content" in p ? p.content : ""))
            .join(""),
        })
      }
    }
  }
  return out
}
