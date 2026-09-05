/**
 * RunSession — 门面构造的每 turn 受限视图（移植 ``core/agent/session.py``）。
 *
 * mailbox 只读不删；checkpoint 提交消费，失败 turn 的消息保留待重试。
 */

import type { ContextManager } from "../memory/context-manager.js"
import type { AgentId, AgentMessage } from "./types.js"

export class RunSession {
  constructor(
    public readonly agentId: AgentId,
    public readonly runtime: unknown,
    public readonly contextRef: string,
    public readonly memory: ContextManager,
    private readonly mailbox: AgentMessage[]
  ) {}

  /** 只读不删：turn 失败时未读消息须保留待重试。 */
  receiveMessages(): AgentMessage[] {
    return [...this.mailbox]
  }

  /** 提交 mailbox 消费：仅正常返回或进入持久化等待后调用。 */
  checkpoint(): void {
    this.mailbox.length = 0
  }
}
