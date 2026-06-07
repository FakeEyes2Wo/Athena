# Multica 利用设想

版本：0.1  
日期：2026-06-07  
相关 submodule：`external/multica`  
范围：说明 Multica 在 Li-Shou AI Scientist 项目中可能如何使用。本文是集成设想，不是实施承诺。

## 1. 定位

Multica 是一个开源 Managed Agents 平台，核心价值不是单个模型推理，而是把多个 coding agent 管成“可分配任务、可跟踪进度、可复用技能”的团队运行系统。

对本项目而言，Multica 可以有两种使用方式：

1. **参考架构**：学习它的 issue 生命周期、runtime/daemon、agent 分配、skills 注入、autopilot、实时进度流等设计。
2. **可选外壳**：在后期演示或内部研发中，用 Multica 管理 Qwen Code、Codex、OpenClaw、Hermes 等执行后端的任务流。

当前建议优先作为参考架构使用，不急于把 Multica 深度嵌入 AI Scientist 主链路。

## 2. Multica 已具备的能力

根据 `external/multica/README.md`、`README.zh-CN.md`、`CLI_AND_DAEMON.md` 和 `docs/product-overview.md`，Multica 的关键能力包括：

| 能力 | 概括 | 对 Li-Shou 的价值 |
| --- | --- | --- |
| Agent as Teammates | Agent 以成员身份出现在看板、评论、任务分配中 | 可借鉴为 AI Scientist 的多智能体协作 UI |
| Issue 生命周期 | 任务从创建、认领、执行到完成/失败有状态流 | 可映射为 hypothesis、experiment、review 任务状态 |
| Squads | 多个 agent 组成小队，由 leader agent 路由任务 | 可用于 Scientist/Skeptic/Methodologist/Citation Auditor 小队 |
| Autopilots | cron、webhook 或手动触发周期性任务 | 可用于定期文献更新、引用复核、实验重跑 |
| Reusable Skills | 把任务经验沉淀为可复用技能并注入 agent 上下文 | 可用于“文献综述”“数据画像”“引用审计”等固定流程 |
| Unified Runtimes | 用统一面板管理本地 daemon 和云端 runtime | 可借鉴为 Execution Harness 的 backend registry |
| Agent Daemon | 本地 daemon 探测 CLI，并在隔离工作目录中执行任务 | 可借鉴为后端执行 sandbox 和任务队列 |
| 实时进度流 | WebSocket 推送任务进展 | 可借鉴为前端展示 harness events |
| 多工作区隔离 | workspace 级别隔离 agents、issues、settings | 可映射到不同研究项目或不同参赛案例 |
| 自部署 | 支持 self-hosting | 可在比赛演示中保留本地可控性 |

Multica daemon 当前文档列出的自动探测 CLI 包括 Claude Code、Codex、GitHub Copilot CLI、OpenClaw、OpenCode、Hermes、Gemini、Pi、Cursor Agent、Kimi、Kiro CLI 等。它没有在当前 README 中明确列出 Qwen Code，因此若要让 Multica 直接调度 Qwen Code，需要新增 provider/CLI 适配或用外层 wrapper 伪装成可探测命令。

## 3. 与现有主框架的关系

Li-Shou 当前主框架中已有：

```text
AI Scientist Orchestrator
  -> ExecutionHarness
      -> QwenCodeHarness
      -> CodexHarness
      -> MinimalHarness
```

Multica 不应替代 AI Scientist Orchestrator。更合适的关系是：

```text
AI Scientist Orchestrator
  -> Scientific workflow: 文献、数据、假设、实验、报告
  -> ExecutionHarness: 代码执行、实验执行、产物归档

Multica
  -> 可选的 agent task board / runtime manager / daemon reference
```

也就是说，Multica 管“谁去做、状态如何、在哪个 runtime 上做”，AI Scientist 主框架管“科学任务是什么、证据链是否成立、输出是否符合赛题”。

## 4. 可借鉴的设计点

### 4.1 Issue 到科研任务的映射

| Multica 概念 | Li-Shou 映射 |
| --- | --- |
| Workspace | 一个研究项目或一个参赛案例 |
| Issue | 一项待执行科研子任务 |
| Agent | 一个可执行角色，如 Data Profiler、Experiment Runner |
| Squad | 多角色小组，如 Hypothesis Review Squad |
| Runtime | 本地或远程执行环境 |
| Skill | 固定科研流程说明 |
| Autopilot | 定期触发的科研自动化 |

示例 issue：

```yaml
title: "对 Gaia 样例数据生成数据画像"
assignee: "Data Profiler Agent"
context:
  project_id: astronomy_demo
  dataset_id: gaia_sample_001
  expected_artifacts:
    - profile_summary.csv
    - missingness_report.md
    - feature_distribution.png
```

### 4.2 Runtime/Daemon 参考

Multica daemon 的思路很适合借鉴到 `ExecutionHarness`：

1. 启动时探测可用执行后端。
2. 把每个后端注册成 runtime。
3. 轮询或订阅任务队列。
4. 在隔离工作目录执行任务。
5. 流式回传进度。
6. 定期发送 heartbeat。
7. 对完成/取消任务做工作目录清理。

这可以补强当前 `docs/backend_execution_harness.md` 中的 backend registry、event stream、artifact store 和 GC 设计。

### 4.3 Skills 注入

Multica 的 skill 更像“运行前注入给 agent 的任务说明和上下文”。Li-Shou 可以把以下内容沉淀为 skill：

1. `literature-review-skill`：如何检索、筛选、总结论文。
2. `citation-audit-skill`：如何检查引用真实性和断言支撑关系。
3. `dataset-profile-skill`：如何生成数据画像和质量报告。
4. `baseline-experiment-skill`：如何设置 baseline、metrics、seed。
5. `report-composer-skill`：如何按赛题 12 个字段输出研究计划。

注意：skill 只提供上下文和流程约束，不应绕过证据库、引用审计和人在回路。

### 4.4 Autopilot 自动化

可借鉴的周期任务：

| 自动任务 | 触发方式 | 产物 |
| --- | --- | --- |
| 每日文献更新 | cron | 新论文列表、差异摘要 |
| 每周引用复核 | cron | 无法访问文献、撤稿/更新检查 |
| 实验重跑 | 手动或 cron | 最新 metrics、失败日志 |
| 数据源可用性检查 | cron | 数据 URL 状态、hash 变化 |
| 报告一致性检查 | 手动 | 字段缺失、引用缺失、证据缺失 |

这些任务适合出现在比赛演示的“应用潜力”部分，证明系统不是一次性生成报告，而是可持续维护科研项目。

## 5. 三种使用路线

### 5.1 路线 A：只作为设计参考

这是当前最推荐路线。

做法：

1. 阅读 Multica 的 runtime、daemon、skill、autopilot 设计。
2. 把核心思想吸收到 Li-Shou 自己的 `ExecutionHarness`。
3. 不运行 Multica 服务，不引入它的前后端依赖。

优点：

1. 风险最低。
2. 不受许可证附加条件影响。
3. 不会把项目复杂度拉高。

### 5.2 路线 B：内部研发任务看板

做法：

1. 自部署 Multica 或使用其云端服务。
2. 把开发任务、实验任务、文档任务作为 issue 管理。
3. 使用 Codex/OpenClaw/Hermes 等已有支持的 CLI 跑工程任务。

优点：

1. 研发协作更顺滑。
2. 可观察 agent 执行过程。

风险：

1. 需要额外部署和维护。
2. 比赛主线会变复杂。
3. Qwen Code 当前需要额外适配。

### 5.3 路线 C：作为比赛演示外壳

做法：

1. 把 AI Scientist 的执行任务同步成 Multica issue。
2. 前端展示 Multica 看板或 runtime 状态。
3. 由 Multica daemon 调用底层 CLI 执行任务。

适合在后期考虑。只有当主系统已经稳定，且团队希望突出“多智能体团队协作基础设施”时再做。

风险：

1. 与赛题 Qwen/百炼主线可能抢叙事焦点。
2. Qwen Code 适配需要额外工程。
3. 许可证和展示边界需要确认。

## 6. 许可证与合规注意

`external/multica/LICENSE` 显示 Multica 使用 Modified Apache 2.0，并带有商业使用附加条件。当前项目若只是本地研究、架构参考或内部使用，风险较低；如果把 Multica 前端/后端作为产品组件、托管服务或对外商业分发，需要仔细审查许可证，必要时联系 Multica 获得授权。

建议：

1. 比赛方案中可以引用“借鉴 Managed Agents 平台思想”，但不要声称复制或内嵌 Multica。
2. 如果演示中运行 Multica 前端，不要移除其 logo 或版权信息。
3. 如果只使用 submodule 作为参考资料，不把其代码打包进最终系统，需要在提交材料中说明第三方依赖边界。

## 7. 推荐下一步

1. 先保持 Multica 为 submodule 参考，不做服务部署。
2. 从 `docs/product-overview.md` 和 `CLI_AND_DAEMON.md` 提取 runtime/daemon 设计，补强 `ExecutionHarness`。
3. 若后续要用 Multica 管理 Qwen Code，新增一个 `qwen` provider 探测和执行适配。
4. 若要演示 Autopilot，优先做“每日文献更新”或“报告一致性检查”这类轻量任务。

## 8. 参考来源

1. Multica 仓库：<https://github.com/multica-ai/multica>
2. 本地 README：`external/multica/README.md`
3. 本地中文 README：`external/multica/README.zh-CN.md`
4. CLI 和 daemon 文档：`external/multica/CLI_AND_DAEMON.md`
5. 自部署文档：`external/multica/SELF_HOSTING.md`
6. 产品概览：`external/multica/docs/product-overview.md`
7. 许可证：`external/multica/LICENSE`

