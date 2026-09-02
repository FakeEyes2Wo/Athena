# PR #25 / #26 与本地修复整合说明

## 背景

本地曾针对真实 VALIDATE 故障逐步加入输出新鲜度、结构化 JSON、repair loop、
策略审查、数据集路径传递和无进展终止等修复。与此同时，PR #25 与 #26 对其中
多条链路给出了更完整的实现。如果直接叠加，两套 freshness 和 execution repair
逻辑会重复并产生冲突。

## 整合原则

- 以 PR #25 的通用文件/目录 freshness 实现为准，不保留按 `report` 名称特判的本地实现。
- 以 PR #26 的 typed execution failures、输出 diff 剔除、changed-line policy 和
  prediction coverage 实现为准，不保留本地广泛捕获 `RuntimeError/ValueError` 的版本。
- 保留 PR 未覆盖的本地能力：原始数据集路径注入、持久化任务理解回退、自然语言中
  唯一合法 JSON 提取、严格 structured-output 重试提示、失败原文 artifact，以及
  unchanged diff 的 no-progress 快速终止。

## 采用的 PR 内容

| 来源 | 采用内容 |
|---|---|
| PR #25 | 共享 fenced JSON 解析；文件/目录通用 freshness；相关测试替身修复 |
| PR #26 | execution failure typed exceptions 与回灌；declared outputs 从 review diff 剔除；changed-line policy；prediction row coverage；fork resume 字段；SEARCH 前置条件与路径 prompt |

## 保留的本地差异

| 链路 | 内容 |
|---|---|
| Structured output | 从混合自然语言中只接受唯一一个 schema-valid JSON；歧义时拒绝 |
| Structured retry | 回传完整 schema，要求单一裸 JSON；最终失败保存 raw response artifact |
| Dataset execution | `ATHENA_DATA_CSV` 通过 `ExecutionContext` / `CommandRequest` 注入本地命令 |
| Dataset recovery | `config.dataset_path` 为空时回退到 `state.task_understanding.dataset` |
| Repair liveness | 同一阶段连续出现同一 diff 时立即报 no progress，避免消耗全部 16 次预算 |

## 调用链

```text
state.task_understanding.dataset
  -> PhaseRunner._validation_data_csv
  -> ValidationOptions.data_csv
  -> _execute_predictions
  -> ExecutionContext / CommandRequest
  -> LocalBackend / EnvironmentManager
  -> ATHENA_DATA_CSV

Agent final text
  -> shared unfence_json (PR #25)
  -> exact schema parse
  -> unique embedded schema-valid JSON recovery
  -> retry with explicit schema
  -> final raw-response artifact on exhaustion

validation rejection
  -> PR #26 typed feedback / review policy
  -> compare stage + diff ref with previous rejection
  -> unchanged: terminate as no progress
  -> changed: continue normal repair loop
```

## 恢复与回滚

整合前的全部已跟踪修改保存在 Git stash：

```text
backup pre PR25-26 integration 2026-09-02
```

因此冲突解决或后续验证出现问题时，原始本地状态仍可取回。未跟踪文件没有被该
stash 移动。
