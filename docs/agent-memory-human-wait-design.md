# Athena Agent 记忆与人工等待设计

> 状态：详细设计，待用户审阅
> 日期：2026-08-08
> 上位设计：[dynamic-agent-orchestration-design.md](dynamic-agent-orchestration-design.md)
> 运行时设计：[agent-kernel-runtime-design.md](agent-kernel-runtime-design.md)
> 范围：Agent 私有记忆、项目记忆、跨项目全局记忆、候选检索与 WAITING_FOR_HUMAN

## 1. 目标

Athena 需要保留三种不同性质的记忆，但不能把它们混成共享可变黑板：

1. Agent 私有记忆：同一实例跨 turn 延续工作。
2. 项目记忆：在当前项目内复用稳定事实和工程经验。
3. 全局记忆：经用户批准后跨项目复用。

人工等待是全局记忆写入和其他权威用户决定的统一门禁，必须可跨进程恢复。

## 2. 当前实现判断

现有 [memory_design.md](memory_design.md) 与 `src/athena/memory/` 已实现私有对话记忆的核心能力：

- `ContextManager` 保存 PydanticAI ModelMessage；
- `Compactor` 压缩早期历史；
- `RolloutRecorder` 追加 JSONL；
- `resume_context` 从 rollout 恢复。

这些能力应迁入 AgentSession 的唯一所有权，而不是重写。

当前 `src/athena/retrieval/` 负责论文和模型搜索，不是 Agent memory store。项目/全局记忆不放入该模块，避免“外部资料检索”和“内部经验记忆”同名混用。

## 3. 三层记忆

### 3.1 私有记忆

私有记忆绑定 `agent_id`：

```text
agent_id -> context_ref -> rollout + latest compaction
```

它包含该实例的模型消息、工具交互和历史摘要。规则：

- 只有显式 follow-up 同一 agent_id 才恢复；
- 新建同类型实例不继承；
- 其他 Agent 不能直接读取；
- Kernel 只负责加载和保存，不做语义晋升；
- turn 完成或进入等待后可卸载进程内 ContextManager。

DataAnalysis 修订使用这一机制：ReflectionAgent 不读取 DataAgent 私有记忆；它读取 v1 Artifact。Supervisor 把 review follow-up 给原 DataAgent 后，原实例恢复自己的上下文生成 v2。

### 3.2 项目记忆

项目记忆保存当前项目内可复用、且有证据来源的内容：

- 已确认的数据或任务事实；
- 影响后续阶段的设计决定；
- 工程故障、实际修复方式和验证结果；
- 已通过评估的分析结论。

项目记忆不保存完整聊天历史，也不复制大型报告。正文较大时只保存短摘要和 ArtifactRefs。

### 3.3 全局记忆

全局记忆面向跨项目复用，例如稳定的工具故障修复经验、通用绘图规则或可靠的数据处理注意事项。

任何 Agent 都不能直接写全局记忆。ReflectionAgent 可以产生候选，Supervisor 必须请求用户确认；只有明确批准后，确定性服务才写入全局 store。

## 4. 内部记忆条目

记忆条目是存储层内部 JSON Artifact，不进入 Agent 公共合同：

```json
{
  "schema_version": 1,
  "scope": "project",
  "kind": "failure_repair",
  "summary": "CSV 编码检测失败时使用显式 UTF-8 fallback",
  "source_refs": ["sha256:..."],
  "created_by": "agent_...",
  "supersedes": null
}
```

最小字段：

```text
scope        project | global_candidate | global
kind         fact | decision | failure_repair | validated_finding
summary      可注入 prompt 的短文本
source_refs  支撑该记忆的 ArtifactRefs
created_by   来源 agent_id 或 user
supersedes   可选，指向被修正条目
```

不增加公开 `MemoryCandidate` DTO。AgentMessage 只传该条目的 ArtifactRef。

条目不原地改写。发现旧记忆错误时，创建带 `supersedes` 的新条目；检索默认忽略已被取代的条目，但审计历史保留。

## 5. 项目记忆晋升

首版只允许在明确事件后晋升，避免每个 Agent turn 都自动总结：

```text
成功提交正式业务 Artifact
工程故障经过实际修复并验证
EvaluationPolicy passed
用户明确确认项目事实
```

晋升流程：

1. Agent 或 Reflection 生成记忆条目 Artifact；
2. 确定性服务校验 source_refs 非空且属于当前项目；
3. 把条目追加到项目 memory index；
4. 不修改来源 Agent 的私有 memory。

普通模型建议、失败但未验证的修复、未通过评估的结论不自动晋升。

## 6. 全局记忆候选

ReflectionAgent 在项目阶段结束或故障修复完成后，可以生成 `scope=global_candidate` 的条目。候选必须包含：

- 可跨项目理解的短摘要；
- 来源项目与 ArtifactRefs；
- 为什么具有通用性；
- 可能不适用的条件。

候选生成不等于写入。Supervisor 只把候选 ArtifactRef 交给 `wait_for_human`。

## 7. 人工确认

### 7.1 请求

```text
Supervisor wait_for_human(
  content="是否将该修复经验加入全局记忆？",
  context_refs=[candidate_ref],
)
```

Kernel 在同一事务中：

- 保存稳定 request id；
- 保存目标 Supervisor agent_id；
- 保存问题和 context_refs；
- 把 Agent 设为 WAITING_FOR_HUMAN；
- 结束当前 Run；
- 释放执行槽。

这些字段属于 Kernel 内部等待记录，不增加公共 `ApprovalRecord`。

### 7.2 回复

App Server 只负责展示请求和把用户回复交回 Kernel：

```text
approved -> 写入 mailbox -> 执行批准动作 -> 唤醒 Supervisor
rejected -> 写入 mailbox -> 保留候选审计 -> 唤醒 Supervisor
```

客户端断线、transport timeout 和服务重启都不自动转成 rejected。等待只在用户明确回复、取消项目或删除项目后结束。

### 7.3 写入全局 store

批准后由普通服务完成：

1. 读取 candidate Artifact；
2. 把 scope 改为 global 并保留来源；
3. 写入新的不可变 Artifact；
4. 追加到全局 memory index；
5. 把 global ref 回写项目审计。

ReflectionAgent 和 SupervisorAgent 都不直接写全局数据库。

## 8. 索引与检索

### 8.1 存储位置

```text
项目索引：{project}/.athena/project-memory.sqlite3
全局索引：用户 Athena 数据目录/global-memory.sqlite3
正文：ArtifactStore
```

索引只保存 entry ref、scope、kind、摘要、来源和 supersedes 关系。

### 8.2 首版检索

首版不建设向量数据库。使用：

- scope 与 kind 过滤；
- SQLite FTS 对 summary 做关键词查询；
- 最近相关条目作为 fallback；
- 去除已被 supersedes 的条目。

Kernel 返回候选 ArtifactRefs 和短摘要；Supervisor 显式选择真正注入目标 Agent 的引用。语义向量检索在真实召回不足后再设计。

## 9. 注入规则

Agent turn 的上下文优先级：

```text
当前用户/触发消息
显式 context_refs
该 agent_id 的私有记忆
Supervisor 选择的项目记忆
Supervisor 选择的已批准全局记忆
```

禁止：

- 自动注入全部项目记忆；
- 自动注入未批准的全局候选；
- 把另一个 Agent 的完整私有 memory 注入当前 Agent；
- 把工具日志直接晋升为长期记忆；
- 让检索结果覆盖当前用户要求或确定性项目事实。

## 10. 与 compaction 的关系

Compaction 只压缩单个 Agent 私有历史，不产生项目或全局记忆。压缩摘要可以保留：

- 已做决定；
- 已完成工作；
- 待办与重要 ArtifactRefs。

它不能因为摘要中出现“通用经验”就自动晋升。长期记忆仍走独立的、带来源的晋升流程。

## 11. 失败反馈

首版只保护：

- 私有 context_ref 能恢复；
- 全局写入前存在明确批准；
- 项目与全局 scope 不混写；
- source_refs 保留；
- WAITING_FOR_HUMAN 可恢复。

检索质量、摘要质量和候选去重问题通过真实运行观察后补充，不预先设计复杂评分器。

## 12. 最小验证

- follow-up 原 agent_id 恢复私有记忆；
- 同类型新实例看不到另一个实例的私有记忆；
- 已验证故障修复能进入项目记忆；
- 未验证建议不会自动晋升；
- 全局候选在批准前不可被其他项目检索；
- rejected 候选不写全局 store；
- App Server 断线后 WAITING_FOR_HUMAN 仍存在；
- approved 后写入新 global ref 并唤醒原 Supervisor；
- supersedes 后默认检索不再返回旧条目。
