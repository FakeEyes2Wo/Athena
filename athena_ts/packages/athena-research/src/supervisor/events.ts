/**
 * 事件投影记录与脱敏（移植 ``research/supervisor/events.py``）。
 */

import { z } from "zod"
import type { ArtifactRef } from "@athena/core"

const SECRET_PATTERNS: Array<{ groups: boolean; regex: RegExp }> = [
  {
    groups: true,
    regex: /(\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)\b\s*[=:]\s*)([^\s,;]+)/gi,
  },
  {
    groups: false,
    regex: /\bsk-[A-Za-z0-9_-]{8,}\b/g,
  },
]

/** 遮蔽展示与共享证据文本中的敏感材料。 */
export function redact(text: string): string {
  let redacted = text
  for (const { groups, regex } of SECRET_PATTERNS) {
    redacted = redacted.replace(regex, groups ? "$1[REDACTED]" : "[REDACTED]")
  }
  return redacted
}

const ANSI_STRING = /(?:\x1b(?:\]|P|X|\^|_)|[\x90\x98\x9d\x9e\x9f])[\s\S]*?(?:\x07|\x1b\\|\x9c|$)/g
const ANSI_CSI = /(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]/g
const ANSI_ESCAPE = /\x1b[ -/]*[0-~]/g
const UNPRINTABLE = /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\uFFFD]/g

/** 移除终端控制序列，保留可读 Unicode 与布局。 */
export function sanitizeTerminalText(text: string): string {
  let normalized = text.replace(/\r\n/g, "\n").replace(/\r/g, "\n")
  normalized = normalized.replace(ANSI_STRING, "")
  normalized = normalized.replace(ANSI_CSI, "")
  normalized = normalized.replace(ANSI_ESCAPE, "")
  return normalized.replace(UNPRINTABLE, "")
}

/** 中间截断 text，保留两端。 */
export function truncateMiddle(text: string, maxChars: number): string {
  if (!text || maxChars <= 0 || text.length <= maxChars) return text
  const left = Math.floor(maxChars / 2)
  const right = maxChars - left
  const removed = text.length - maxChars
  return `${text.slice(0, left)}…${removed} chars truncated…${text.slice(text.length - right)}`
}

/** 一条有序 append-only 展示记录。 */
export const OutputEventSchema = z.object({
  type: z.literal("output").default("output"),
  seq: z.number().int().min(1),
  source: z.enum(["supervisor", "agent", "tool"]),
  channel: z.enum(["text", "stdout", "stderr", "error"]),
  text: z.string(),
  plan: z.string().nullable().default(null),
  tool: z.string().nullable().default(null),
  artifact_ref: z.string().nullable().default(null),
  truncated: z.boolean().default(false),
})
export type OutputEvent = z.infer<typeof OutputEventSchema>

/** 一条完整可替换的运行时快照。 */
export const StateEventSchema = z.object({
  type: z.literal("state").default("state"),
  status: z.string(),
  phase: z.string(),
  plans: z.array(z.record(z.string(), z.unknown())).default([]),
  search: z.record(z.string(), z.unknown()).default({}),
  sota: z.record(z.string(), z.unknown()).nullable().default(null),
  waiting: z.record(z.string(), z.unknown()).nullable().default(null),
  manual: z.boolean().default(false),
  pending: z.array(z.record(z.string(), z.unknown())).default([]),
  validation: z.record(z.string(), z.unknown()).nullable().default(null),
  eda_dir: z.string().nullable().default(null),
})
export type StateEvent = z.infer<typeof StateEventSchema>

interface TextStore {
  putText(text: string): Promise<ArtifactRef>
}

/** 创建安全的运行时记录，完整工具输出仍作为 artifact 保留。 */
export class EventProjector {
  private store: TextStore
  private sequence = 0

  constructor(store: TextStore) {
    this.store = store
  }

  private nextSequence(): number {
    this.sequence += 1
    return this.sequence
  }

  resume(sequence: number): void {
    if (sequence > this.sequence) this.sequence = sequence
  }

  output(opts: {
    source: "supervisor" | "agent" | "tool"
    channel: "text" | "stdout" | "stderr" | "error"
    text: string
    plan?: string | null
    tool?: string | null
    artifactRef?: ArtifactRef | null
    truncated?: boolean
  }): OutputEvent {
    return OutputEventSchema.parse({
      seq: this.nextSequence(),
      source: opts.source,
      channel: opts.channel,
      text: redact(opts.text),
      plan: opts.plan ?? null,
      tool: opts.tool ?? null,
      artifact_ref: opts.artifactRef ?? null,
      truncated: opts.truncated ?? false,
    })
  }
}
