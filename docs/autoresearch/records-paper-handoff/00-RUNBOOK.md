# Records-to-Paper RUNBOOK（总控）

适用：已有实验记录（当前支持 Athena；未来 wandb/csv/mlflow）→ 论文。
执行者：DSH / Coding Agent。01–04 每个模块独立可替换。

## AutoResearch 控制平面包装

当本 RUNBOOK 由 `ml.records-to-paper` 启动时，INTAKE 抽取 evidence 后、CLAIMS 前生成
并独立审查一份任务 rubric。该 rubric 必须标记为 `post-hoc`，只用于判定现有证据
允许什么强度的 claim 和还缺什么材料；不得伪装为预注册标准，也不得重定义已观察
到的主要指标。

步骤、恢复位置和最近错误由精简 `AutoResearchService` 保存。RUNBOOK 本身继续负责
写作与审查，不要求新增 Stage/Provider/Gate 代码框架。

## 常量

| 常量 | 默认 | 说明 |
|---|---|---|
| `AUTO_PROCEED` | `true` | 跳过全部 checkpoint |
| `MAX_PAPER_ROUNDS` | `5` | 内容修订循环上限；耗尽且核心贡献全 unsupported → FAILURE_REPORT |
| `MAX_CONTRACT_ROUNDS` | `3` | 验收契约谈判轮数 |
| 编译修复轮次 | 不限 | 只受 `project_time_limit` |
| `COST_TARGET` | ~$10 API/篇 | 01/05 零 LLM 成本 |
| 论文路径 | `overleaf → local → none` | Paper Engine 三路径 |

## 产物目录

```text
<work_dir>/records-paper/<run_id>/
  evidence_chain.json
  RUBRIC.md                         # post-hoc，限制 claim 强度，不冒充预注册
  record_digest.skeleton.md / record_digest.md
  claims.md / related_work_scaffold.md / outline.md
  paper_acceptance_contract.md / trace_exclusions.md
  trace_audit.json
  paper/  packaging/  FINAL_REPORT.md [FAILURE_REPORT.md]
```

## 阶段与 gate

| 阶段 | 模块 | 脚本 | gate |
|---|---|---|---|
| INTAKE | 01 | `extract` | exit 0 + schema 合法 |
| CLAIMS | 02 | 无 | 无 NEEDS_CONFIRMATION；contract accepted/contested |
| WRITING | 03 | 无 | 无 TODO/FIXME/DATA_NEEDED；bib 卫生 |
| REVIEW | 04 | `verify-trace` | PASS/WARN；编译过；三审查无 FAIL |
| PACKAGING | 04 | `verify-trace` 终检 | PASS/WARN；三审查 JSON 齐全 |

## 铁律

1. 数字只来自 `evidence_chain.json` 或 report artifact 原文；标签 `E:` / `B:`。
2. writer 不审自己的稿；三审查 fresh thread、跨 model family、零上下文。
3. `REFUTED/INCONCLUSIVE/FAILED` 写正文，不可排除躲掉。
4. bib 走 DBLP/CrossRef，否则 `[VERIFY]`。
