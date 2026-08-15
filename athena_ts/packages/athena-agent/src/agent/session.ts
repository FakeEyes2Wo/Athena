/**
 * RunSession — 门面构造的每 turn 受限视图（移植 ``core/agent/session.py``）。
 *
 * mailbox 读即清，checkpoint 为空操作（失败 turn 的未读消息须保留待重试）。
 */

import type { ContextManager } from "../memory/context-manager.js"
import type { AgentId, AgentMessage } from "./types.js"

/** ContextManager 只读视图；``raw`` 暴露底层 ContextManager。 */
export class MemoryView {
  constructor(private memory: ContextManager) {}

  get raw(): ContextManager {
    return this.memory
  }

  append(): never {
    throw new Error("memory writes go through ThreadRuntime")
  }

  replaceRange(): never {
    throw new Error("memory writes go through ThreadRuntime")
  }
}

export class RunSession {
  private memoryView: MemoryView
  private mailbox: AgentMessage[]

  constructor(
    public readonly agentId: AgentId,
    public readonly runtime: unknown,
    public readonly contextRef: string,
    memory: ContextManager,
    mailbox: AgentMessage[]
  ) {
    this.memoryView = new MemoryView(memory)
    this.mailbox = mailbox
  }

  get memory(): MemoryView {
    return this.memoryView
  }

  /** 只读不删：turn 失败时未读消息须保留待重试。 */
  receiveMessages(): AgentMessage[] {
    return [...this.mailbox]
  }

  /** 提交 mailbox 消费：仅正常返回或进入持久化等待后调用。 */
  checkpoint(): void {
    this.mailbox.length = 0
  }
}
