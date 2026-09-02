# Athena vs Codex Harness 差异分析

本目录比较当前 Athena 与参考仓库 `C:\Users\80163\Desktop\挑战杯_2026\codex` 中
与 **messages / context window / agent harness** 相关的机制差异，并给出 Athena
缺失部分与改进思路。

## 阅读入口

| 文件 | 内容 |
| --- | --- |
| [01-messages-mechanism.md](01-messages-mechanism.md) | 消息模型、消息持久化、协议 item 结构差异 |
| [02-context-window-mechanism.md](02-context-window-mechanism.md) | 上下文估算、压缩、窗口管理、rollout 恢复差异 |
| [03-harness-mechanism.md](03-harness-mechanism.md) | Turn/Session/Step、工具执行、事件、多 Agent、环境/权限等 harness 差异 |
| [04-missing-parts-and-improvements.md](04-missing-parts-and-improvements.md) | Athena 缺失能力清单和改进优先级 |

## 参考路径

- Athena 当前仓库：`C:\Users\80163\Desktop\挑战杯_2026\Athena`
- Codex 参考仓库：`C:\Users\80163\Desktop\挑战杯_2026\codex`

主要 Athena 源码：

- `src/athena/memory/{context_manager,compaction,rollout}.py`
- `src/athena/core/agent/{runtime,provider,agent_runtime}.py`
- `src/athena/core/{tool,tool_types}.py`
- `src/athena/app_server/{thread_runtime,events,submissions,protocol}.py`
- `athena_ts/packages/athena-agent/src/{messages.ts,memory/*,agent/*}`

主要 Codex 源码：

- `codex-rs/protocol/src/{items.rs,models.rs}`
- `codex-rs/core/src/context_manager/{history,normalize,updates}.rs`
- `codex-rs/core/src/session/{context_window,auto_compact_window,turn,turn_context,step_context}.rs`
- `codex-rs/core/src/{compact.rs,compact_remote*.rs,tasks/compact.rs}`
- `codex-rs/core/src/tools/{registry,router,parallel,lifecycle}.rs`
- `codex-rs/history/src/lib.rs`
- `codex-rs/rollout/src/{recorder,compression,metadata,state_db}.rs`
