/**
 * Append-only JSONL 持久化（移植 ``memory/rollout.py``）。
 *
 * 路径规则: ``{project}/.athena/sessions/{year}/{month}/{day}/rollout-{short_id}.jsonl``。
 * 每次写入后立即 flush，恢复时流式读取，遇到更新的 compaction checkpoint 时重置内存上下文。
 */

import { randomBytes } from "node:crypto"
import {
  appendFileSync,
  existsSync,
  mkdirSync,
  openSync,
  readFileSync,
  readSync,
  statSync,
} from "node:fs"
import { join } from "node:path"

import { ModelMessagesTypeAdapter, modelRequest, systemPrompt, type ModelMessage } from "../messages.js"
import { ContextManager } from "./context-manager.js"

function utcNowIso(): string {
  return new Date().toISOString()
}

/** 返回紧凑且安全的文件名 ID。 */
function safeShortId(threadId: string): string {
  const safe = threadId
    .slice(0, 12)
    .split("")
    .map((c) => (/[a-zA-Z0-9]/.test(c) || c === "-" || c === "_" ? c : "_"))
    .join("")
  return safe || "thread"
}

export class RolloutRecorder {
  private readonly base: string
  private path_: string | null = null
  private opened = false
  private seq = 0

  constructor(projectRoot: string) {
    this.base = join(projectRoot, ".athena", "sessions")
  }

  /** 当前 JSONL 文件路径，``open()`` 之前为 ``null``。 */
  get path(): string | null {
    return this.path_
  }

  /** 创建当天的 rollout 文件并返回路径。 */
  async open(threadId: string): Promise<string> {
    return this.openSync(threadId)
  }

  /** 同步创建或复用 rollout 文件并返回路径。 */
  openSync(threadId: string, appendTo?: string): string {
    if (this.opened) {
      return this.path_!
    }

    if (appendTo != null) {
      this.path_ = appendTo
      if (existsSync(appendTo) && statSync(appendTo).size > 0) {
        const size = statSync(appendTo).size
        const fd = openSync(appendTo, "r+")
        const buf = Buffer.alloc(1)
        readSync(fd, buf, 0, 1, size - 1)
        // 末尾非换行 → 补一个换行，避免半条记录粘连
        if (buf[0] !== 0x0a) {
          appendFileSync(appendTo, "\n", "utf8")
        }
      }
      this.opened = true
      this.seq = 0
      return appendTo
    }

    const now = new Date()
    const dayDir = join(
      this.base,
      String(now.getUTCFullYear()),
      String(now.getUTCMonth() + 1).padStart(2, "0"),
      String(now.getUTCDate()).padStart(2, "0")
    )
    mkdirSync(dayDir, { recursive: true })
    const shortId = safeShortId(threadId)
    this.path_ = join(dayDir, `rollout-${shortId}-${randomBytes(4).toString("hex")}.jsonl`)
    appendFileSync(this.path_, "", "utf8") // 创建空文件（等价 Python open("a")）
    this.opened = true
    this.seq = 0
    return this.path_
  }

  /** 追加一条消息为 JSONL 行。 */
  record(msg: ModelMessage): void {
    if (!this.opened) return
    const payload = ModelMessagesTypeAdapter.dump([msg])
    this.writeLine({ seq: this.seq, ts: utcNowIso(), msg: payload })
  }

  /** 追加一条 compaction 检查点标记。 */
  recordCompaction(version: number, summary: string): void {
    if (!this.opened) return
    this.writeLine({ seq: this.seq, type: "compaction", version, summary })
  }

  /** 关闭（TS 无持久 fd，仅重置打开状态）。 */
  async close(): Promise<void> {
    this.opened = false
  }

  private writeLine(obj: unknown): void {
    appendFileSync(this.path_!, JSON.stringify(obj) + "\n", "utf8")
    this.seq += 1
  }
}

/** 从 rollout JSONL 文件重建 ContextManager（同步实现）。 */
export function resumeContextSync(rolloutPath: string): ContextManager {
  let ctx = new ContextManager()
  const content = readFileSync(rolloutPath, "utf8")
  for (const line of content.split("\n")) {
    if (!line.trim()) continue
    let record: unknown
    try {
      record = JSON.parse(line)
    } catch {
      continue
    }
    if (typeof record !== "object" || record === null || Array.isArray(record)) continue
    const rec = record as Record<string, unknown>

    if (rec["type"] === "compaction") {
      const summary = rec["summary"]
      if (typeof summary !== "string") continue
      ctx = new ContextManager()
      ctx.append(modelRequest([systemPrompt(`[HISTORY SUMMARY]\n${summary}`)]))
      continue
    }

    const payload = rec["msg"]
    if (payload == null) continue
    try {
      const messages = ModelMessagesTypeAdapter.parse(payload)
      for (const message of messages) ctx.append(message)
    } catch {
      continue
    }
  }
  return ctx
}

/** 从 rollout JSONL 文件重建 ContextManager（异步入口，等价 resume_context）。 */
export async function resumeContext(rolloutPath: string): Promise<ContextManager> {
  return resumeContextSync(rolloutPath)
}
