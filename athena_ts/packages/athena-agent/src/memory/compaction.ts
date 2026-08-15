/**
 * 上下文压缩 — 将早期对话历史替换为 LLM 生成的摘要（移植 ``memory/compaction.py``）。
 */

import type { ModelMessage } from "../messages.js"
import { modelRequest, systemPrompt } from "../messages.js"
import { ContextManager } from "./context-manager.js"

const SUMMARY_PART_CHARS = 300

/** 一次压缩的结果，包含回滚所需的原始消息。 */
export interface Compaction {
  version: number
  summary: string
  originalItems: ModelMessage[]
}

/** 摘要用 LLM 的最小客户端形态（chat.completions 或 messages.create）。 */
export interface SummarizeLLM {
  client?: { chat?: { completions?: { create: (kwargs: Record<string, unknown>) => Promise<unknown> } } }
  chat?: { completions?: { create: (kwargs: Record<string, unknown>) => Promise<unknown> } }
  messages?: { create: (kwargs: Record<string, unknown>) => Promise<unknown> }
}

/** 从 LLM 响应中提取摘要文本，兼容三种形态；均无法提取时抛错。 */
function extractText(response: unknown): string {
  const r = response as {
    content?: unknown
    choices?: Array<{ message?: { content?: string } }>
  }
  const content = r.content
  if (typeof content === "string") return content
  if (Array.isArray(content) && content.length > 0) {
    const first = content[0] as { text?: unknown } | undefined
    if (first && typeof first.text === "string") return first.text
  }
  const choices = r.choices
  if (Array.isArray(choices) && choices.length > 0) {
    return choices[0]?.message?.content ?? ""
  }
  throw new Error("摘要模型未返回文本")
}

export class Compactor {
  private _keepRecent: number
  private _summaryModel: string

  constructor(keepRecent: number = 20_000, summaryModel: string = "haiku") {
    if (keepRecent < 0) throw new Error("keep_recent 必须为非负数")
    this._keepRecent = keepRecent
    this._summaryModel = summaryModel
  }

  /** 如果上下文 token 数超过压缩阈值则返回 true。 */
  shouldCompact(ctx: ContextManager, atTokens: number = 170_000): boolean {
    return ctx.tokens >= atTokens
  }

  /** 执行压缩，原地修改 *ctx*。检查版本号防止并发修改。 */
  async compact(ctx: ContextManager, llm: SummarizeLLM): Promise<Compaction> {
    const items = ctx.items
    const split = this._splitRecent(items)
    const old = items.slice(0, split)
    if (old.length === 0) {
      return { version: ctx.version, summary: "", originalItems: [] }
    }

    const sourceVersion = ctx.version
    const summary = await this._summarize(old, llm)
    if (ctx.version !== sourceVersion) {
      throw new Error("压缩过程中上下文被并发修改")
    }

    const summaryMsg = modelRequest([systemPrompt(`[HISTORY SUMMARY]\n${summary}`)])
    ctx.replaceRange(0, split, [summaryMsg])
    return { version: ctx.version, summary, originalItems: old }
  }

  /** 从后往前累积 Token，找到近期消息的起始位置。 */
  private _splitRecent(items: ModelMessage[]): number {
    let acc = 0
    for (let i = items.length - 1; i >= 0; i--) {
      acc += ContextManager.estimateOne(items[i]!)
      if (acc >= this._keepRecent) return i
    }
    return 0
  }

  private async _summarize(items: ModelMessage[], llm: SummarizeLLM): Promise<string> {
    const payload: Record<string, unknown> = {
      model: this._summaryModel,
      temperature: 0.1,
      max_tokens: 2_000,
      messages: [{ role: "user", content: Compactor.summaryPrompt(items) }],
    }
    const client = llm.client ?? llm
    if (client?.chat?.completions?.create) {
      const response = await client.chat.completions.create(payload)
      return extractText(response)
    }
    if (llm.messages?.create) {
      const response = await llm.messages.create(payload)
      return extractText(response)
    }
    throw new Error("摘要模型未返回文本")
  }

  /** 把早期消息渲染成摘要 prompt。 */
  static summaryPrompt(items: ModelMessage[]): string {
    const parts: string[] = [
      "Summarize concisely (decisions, findings, code changes, hypotheses, results):\n\n",
    ]
    for (const msg of items) {
      const role = msg.kind === "request" ? "USER" : "ASSISTANT"
      for (const part of msg.parts) {
        if ("content" in part && typeof part.content === "string") {
          parts.push("[", role, "] ", part.content.slice(0, SUMMARY_PART_CHARS), "\n")
        } else if ("part_kind" in part && part.part_kind === "tool-call") {
          const a = "args" in part ? part.args : undefined
          const aStr = a != null ? String(a) : ""
          parts.push(
            "[ASSISTANT TOOL] ",
            String(part.tool_name),
            " ",
            aStr.slice(0, SUMMARY_PART_CHARS),
            "\n"
          )
        }
      }
    }
    return parts.join("")
  }
}
