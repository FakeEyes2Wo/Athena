# Athena Agent 记忆与人工等待设计

> 状态：一次性迁移专题设计
> 日期：2026-08-08
> 上位设计：[dynamic-agent-orchestration-design.md](dynamic-agent-orchestration-design.md)

## 1. 三层记忆

| 层级 | 作用域 | 写入门槛 |
|---|---|---|
| 私有记忆 | 单个 `agent_id` | AgentSession 正常 turn/wait 边界 |
| 项目记忆 | 当前项目 | 有来源的事实、决定或已验证修复 |
| 全局记忆 | 跨项目 | 用户明确批准 |

三者不能混成共享可变黑板，也不复制完整聊天或大型 Artifact。

## 2. 私有记忆

```text
agent_id -> context_ref -> rollout + compaction
```

- 只有 follow-up 相同 `agent_id` 才恢复；
- 同类型新实例不继承；
- 其他 Agent 不能直接读取；
- `context_ref` 只由 AgentSession 持久化；
- compaction 只压缩该实例历史，不自动晋升长期记忆。

旧 `AgentOutcome.next_context_ref` 不参与 Kernel 私有记忆，旧调用方退场时删除。

## 3. 项目记忆

首版只保存：

- 已确认的任务或数据事实；
- 影响后续阶段的决定；
- 工程故障、实际修复方式和验证结果；
- 已通过评估的分析结论。

未验证建议、失败尝试和普通模型总结只保留在私有记忆或运行 Artifact 中。

内部条目最小字段：

```text
scope
kind
summary
source_refs
created_by
supersedes
```

条目不可原地改写。修正旧条目时创建 `supersedes` 新条目，历史保留。

## 4. 全局记忆

ReflectionAgent 可以从已验证项目结果生成 `global_candidate`，但不能直接写全局索引。候选必须说明：

- 可跨项目理解的摘要；
- 来源 refs；
- 通用理由；
- 不适用条件。

Supervisor 将候选 ref 交给 `wait_for_human`。批准后由确定性 MemoryService 创建新的 global Artifact 并写索引；拒绝只保留审计。

## 5. 人工等待

```text
Agent wait_for_human(content, refs)
  -> Kernel stores request id + agent id + refs
  -> current Run ends and lease releases
  -> App Server displays request
  -> human_reply writes mailbox
  -> Kernel clears wait and creates one wake Run
```

断线、transport timeout 和进程重启都不等于拒绝。等待只因用户回复、项目 stop 或项目删除而结束。

等待记录是 Kernel 内部事实，不增加 Approval DTO。

## 6. 检索与注入

首版使用 scope/kind 过滤、SQLite FTS summary 查询和最近条目 fallback，不建设向量数据库。

上下文优先级：

```text
current trigger and explicit refs
private memory
Supervisor-selected project memory
Supervisor-selected approved global memory
```

禁止自动注入全部项目记忆、未批准候选或另一个 Agent 的完整私有 memory。

## 7. 当前切换要求

私有 rollout 和 wait 恢复已有 Kernel 基础；项目/全局索引及 App Server 人工回复尚未统一接入 ProjectRuntime。一次性切换必须完成该入口，并删除其他模块自己的 approval future 或 memory runtime。

## 8. 验收

- follow-up 原实例恢复私有记忆；
- 同类型新实例看不到另一实例记忆；
- 已验证故障修复可进入项目记忆，未验证建议不能；
- 全局候选批准前不能被其他项目检索；
- rejected 不写全局索引；
- WAITING_FOR_HUMAN 跨断线和重启保留；
- approved 只产生一个 global ref 和一个唤醒 Run；
- supersedes 后默认检索忽略旧条目但保留审计。
