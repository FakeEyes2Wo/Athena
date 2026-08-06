# Athena 当前工程与目录

Status: current
Owner: Athena maintainers
Last verified: 2026-07-31
Source of truth: `src/athena/`, `src/gui_gateway/`, `athena-gui/`, `tests/`, `test/unit/`

## 当前运行主链

```text
GUI / Client
  -> gui_gateway WebSocket adapter
  -> athena.research.ResearchRuntime
  -> PREPARE -> SEARCH -> VALIDATE -> REPORT
  -> core.research_tree.ResearchTree v2
  -> GitWorkBranch + EvalResult + ArtifactRef
```

`athena.core.thread_models` 负责 Thread/Turn 记录模型；`athena.app_server` 继续独立负责请求响应、订阅、并发控制等运行时与控制逻辑，不拥有 Thread/Turn 记录模型。研究运行时不放入或修改 app-server。`gui_gateway` 仅处理 ping、WebSocket 响应封装和逐连接订阅，不持有研究树、预算或工作流状态。

IdeaGeneration 由 `athena.ideator.Ideator` 唯一负责。Ideator 为可配置的独立辩手创建 Thread，并发执行提案、评审和修订阶段，再由一个 judge 生成最终假设与完整审计 artifact；生成失败会显式报错，不存在模板 fallback。

## 规范 Owner

| 概念 | 规范 Owner | 兼容路径 |
|---|---|---|
| 研究生命周期 | `athena.research.runtime.ResearchRuntime` | `gui_gateway` 仅转发 |
| 实验图与持久化 | `athena.core.research_tree.ResearchTree` | `athena.experiment` 重导出核心类型 |
| PREPARE baseline | `athena.workflows.prepare.baseline` | `athena.experiment.baseline` 重导出 |
| IdeaGeneration | `athena.ideator.Ideator` | 无兼容路径或模板 fallback |
| SEARCH loop | `athena.workflows.search.search_loop` | `athena.experiment.search_loop` 重导出 |
| Ranking | `athena.experiment.ranking` | 无遗留 core 路径 |
| VALIDATE | `athena.workflows.validate` | `athena.experiment.validate` 重导出 |
| REPORT | `athena.workflows.report` | `athena.experiment.report` 重导出 |
| Thread/Turn records | `athena.core.thread_models` | `athena.app_server` 负责运行时与控制逻辑；与研究运行时相互独立 |

## ResearchTree v2

运行时只接受严格 v2 快照：

```json
{
  "version": 2,
  "sota_id": "exp_baseline",
  "hypotheses": {"hyp_baseline": {"id": "hyp_baseline"}},
  "experiments": {
    "exp_baseline": {
      "parent_id": null,
      "hypothesis_id": "hyp_baseline",
      "status": "SUCCEEDED"
    }
  }
}
```

实验 ID 是 `experiments` 映射键；Experiment 不嵌入 ID 或 Hypothesis。子关系仅由 `parent_id` 派生。成功实验必须含有限 primary、逐样本证据和真实生命周期状态；SOTA 只可指向成功的 baseline 或 search 实验。

## 工作流约束

- PREPARE 必须真实执行 baseline，成功后才能建立首个 SOTA。
- SEARCH 先登记候选，再转 RUNNING；失败和取消均保留终态及可得日志。
- 排序、预算与 SOTA 只在真实比较裁决后更新。
- VALIDATE 为 SOTA 路径中的每个假设创建独立消融 worktree，并只运行一次成功的冻结 final-test。
- REPORT 只读取树内完整成功证据，生成确定性 Markdown，并把 report artifact 附到 SOTA。
- 缺少工作流后端时明确抛错，不返回占位成功。
- 唯一保留的先进排序延期标记位于 `athena.experiment.ranking`。

## GUI Gateway 与桌面端

Python `GuiRequestHandler` 只转发到共享 ResearchRuntime。每个 WebSocket 连接独立订阅并在断开时退订。Tauri 提供 `tree_get`、`tree_save`、`tree_load` 转发；React 使用 v2 `hypotheses` 与 `experiments`，由 `parent_id` 构图并显示真实 lifecycle 与 eval。
