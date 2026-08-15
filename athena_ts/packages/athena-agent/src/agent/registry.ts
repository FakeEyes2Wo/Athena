/**
 * AgentTypeRegistry — ``agent_type`` 到实例能力的静态注册表（移植 ``core/agent/registry.py``）。
 */

import { AgentCommandError, AgentSpec, ErrorCode, type AgentId } from "./types.js"

export type AgentFactory = (agentId: AgentId, config: string | null) => AgentSpec

export class AgentTypeRegistry {
  private factories = new Map<string, AgentFactory>()

  /** 注册一个 agent_type 的工厂；重复注册同一类型报错。 */
  register(agentType: string, factory: AgentFactory): void {
    if (this.factories.has(agentType)) {
      throw new Error(`agent_type already registered: ${agentType}`)
    }
    this.factories.set(agentType, factory)
  }

  /** agent_type 是否已注册。 */
  contains(agentType: string): boolean {
    return this.factories.has(agentType)
  }

  /** 按 agent_type 创建该实例的全新 binding；未注册抛 NOT_FOUND。 */
  requireSpec(agentType: string, agentId: AgentId): AgentSpec {
    const factory = this.factories.get(agentType)
    if (factory === undefined) {
      throw new AgentCommandError(ErrorCode.NOT_FOUND, `unknown agent_type: ${agentType}`)
    }
    return factory(agentId, null)
  }

  /** 已注册类型（排序后）。 */
  get types(): string[] {
    return [...this.factories.keys()].sort()
  }
}
