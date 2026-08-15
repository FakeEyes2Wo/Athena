/**
 * 基于 ModelMessage 的对话上下文管理（移植 ``memory/context_manager.py``）。
 *
 * snapshot/itemsSince/rollback 构成版本化快照协议；token 不变量：
 * ``_tokenCount`` 始终等于 ``sum(estimateOne(m) for m in _items)``。
 */

import { truncateText } from "../tool-types.js"
import type { ModelMessage } from "../messages.js"

/** 深拷贝消息列表（等价 dataclasses.replace 的深拷贝 items）。 */
function deepCopy<T>(value: T): T {
  return structuredClone(value)
}

export class ContextManager {
  private _items: ModelMessage[] = []
  private _tokenCount = 0
  private _contextLimit: number
  private _version = 0

  constructor(contextLimit: number = 200_000) {
    this._contextLimit = contextLimit
  }

  /** 深拷贝 — 外部修改不影响内部状态和 token 计数。 */
  get items(): ModelMessage[] {
    return deepCopy(this._items)
  }

  /** 当前估算 token 数。 */
  get tokens(): number {
    return this._tokenCount
  }

  /** 当前上下文版本号。 */
  get version(): number {
    return this._version
  }

  get limit(): number {
    return this._contextLimit
  }

  /** 上限以下的可用 token 余量。 */
  tokenMargin(ratio: number = 0.85): number {
    return Math.max(0, Math.floor(this._contextLimit * ratio) - this._tokenCount)
  }

  /** 返回 (idx, version)。compaction 后 version 递增，检测过期快照。 */
  snapshot(): [number, number] {
    return [this._items.length, this._version]
  }

  /** idx 之后新增的消息；越界返回空列表。 */
  itemsSince(idx: number): ModelMessage[] {
    if (idx < 0 || idx >= this._items.length) return []
    return [...this._items.slice(idx)]
  }

  /** 回滚到 idx，删除之后的消息并恢复 token 计数。 */
  rollback(idx: number): void {
    if (idx < 0 || idx > this._items.length) return
    const removed = this._items.slice(idx).reduce(
      (sum, m) => sum + ContextManager.estimateOne(m),
      0
    )
    this._items = this._items.slice(0, idx)
    this._tokenCount -= removed
    this._version += 1
  }

  /** 追加消息，更新 token 计数和版本。 */
  append(msg: ModelMessage): void {
    const prepared = ContextManager.prepare(msg)
    this._items.push(prepared)
    this._tokenCount += ContextManager.estimateOne(prepared)
    this._version += 1
  }

  /** 替换 [start:end] 区间的消息（compaction 使用）。 */
  replaceRange(start: number, end: number, newItems: ModelMessage[]): void {
    const prepared = newItems.map((item) => ContextManager.prepare(item))
    const removed = this._items.slice(start, end).reduce(
      (sum, m) => sum + ContextManager.estimateOne(m),
      0
    )
    const added = prepared.reduce((sum, m) => sum + ContextManager.estimateOne(m), 0)
    this._items.splice(start, end - start, ...prepared)
    this._tokenCount += added - removed
    this._version += 1
  }

  /** 预处理消息：深拷贝 parts，过长工具返回用 truncateText 保留头尾各半。 */
  private static prepare(msg: ModelMessage): ModelMessage {
    if (msg.kind !== "request") {
      return { ...msg, parts: [...msg.parts] }
    }
    const parts = [...msg.parts]
    return {
      ...msg,
      parts: parts.map((part) =>
        part.part_kind === "tool-return" && typeof part.content === "string"
          ? { ...part, content: truncateText(part.content) }
          : part
      ),
    }
  }

  /** 粗略 token 估算：字符串长度 / 4，含 args 序列化开销。 */
  static estimateOne(msg: ModelMessage): number {
    let total = 0
    for (const part of msg.parts) {
      if ("content" in part && typeof part.content === "string") {
        total += part.content.length
      }
      if ("args" in part && part.args != null) {
        total += String(part.args).length
      }
    }
    return Math.max(1, Math.floor(total / 4))
  }
}
