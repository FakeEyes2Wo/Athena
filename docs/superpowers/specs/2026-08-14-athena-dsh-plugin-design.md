# Athena 作为 DSH 插件重设计（spec）

日期：2026-08-14
状态：已确认方向（Athena 作为 DSH cordis 插件/preset 运行）

## 1. 结论与纠偏

Athena 的 TS 端**不是**独立重写一个 agent 运行时 + research 运行时，而是把 **Athena 独有的研究域**打包成 DSH（DeepSeek Harness）的 cordis 插件/preset，运行在 DSH 之上，**复用 DSH 已提供的**：

- agent 运行时（harness 本身跑 agent，含 turn/stream/tool 循环）
- 工具层（bash/pwsh/fs/fs-search/web/skill/jobs/goal/todo/ask-user/subagent/workflow）
- skills 注册表、goals 域（`dsh-goal` + goal round driver）、subagent spawn/fork、workflow、沙箱/审批栈、持久化、model route

纠偏点：M1 `@athena/agent`（provider/runtime/tool/memory 的再实现）与 M2 的 `WorkerRunner` + 手工装配 `ResearchRuntime`，方向重复了 DSH 已有的能力，应**改为 DSH 插件**。M0 用 cordis 的方向是对的，但要落到 DSH 的插件形态（`provide()`/`inject`/`apply(ctx)` + 声明式组合 + realm 隔离）。

## 2. 目标架构

```
DSH host composition (base.cordis.yml + web.cordis.yml)
  ├─ tools / skills / goals / subagents / workflow / sandbox / model route  （DSH 自带）
  └─ Athena preset (agent.cordis.yml 增加 Athena 行)
       ├─ @athena/dsh-research   # 研究域 Services + 工具 + goal
       │    ├─ Service: researchTree     (ResearchTree)
       │    ├─ Service: researchState    (ResearchState + 原子持久化)
       │    ├─ Service: scheduler        (Scheduler/固定流程)
       │    ├─ Service: supervisor       (FixedFlowSupervisor，worker 改为调 DSH subagent/工具)
       │    ├─ Service: evaluator        (TrustedEvaluator)
       │    ├─ Service: scriptRunner     (DataScriptRunner，uv 子进程)
       │    ├─ Tool: research/status     (读 phase/status/SOTA)
       │    ├─ Tool: research/run        (启动 PREPARE→SEARCH→VALIDATE)
       │    └─ Goal: research-flow       (PREPARE→SEARCH→VALIDATE 一步式流程)
       └─ @athena/dsh-research-ideator   # ideator/plan/validate worker 作为 DSH subagent 预设
```

## 3. 保留 vs 弃用

**保留（已有研究域逻辑，改挂到 DSH Service）**：
- `research-tree.ts`（M0 core）、`supervisor/{plans,state,scheduler,recovery,policy}.ts`、`supervisor/experiment.ts`（PlanRunner）、`evaluation.ts`、`script_runner.ts`、`validation.ts`、`supervisor/prepare.ts`（freezeEvaluator/runEvaluatorPlan/runPreparePlan）、`supervisor/validation.ts`、`contracts.ts`。

**弃用/降级（DSH 已提供）**：
- M1 `@athena/agent` 的 provider/runtime/tool/memory 再实现（`WorkerRunner`、`ResponsesProvider` 直连、`Agent` ReAct 循环）→ 由 DSH agent/tools 承担。
- `ResearchRuntime` 手工装配 → 由 DSH 组合根 + cordis `provide()` 承担。

## 4. 插件形态（对齐 DSH）

> **纠偏（2026-08-14 静态查证 DSH 源码 `.d.ts`）**：DSH 的 cordis 是 **`@deepseek-ai/cordis`（vendor 的 cordis 4.0.1）**，不是 npm `cordis@4.0.0-rc.8`（M0 锁定错误）。DSH 工具经 **`ctx.tools.register(ToolDefinition)`** 注册（`@deepseek-ai/dsh-tools`，schema 用 `schemastery`/`zod`），subagent 经 **`ctx.subagents.start(name, SubagentStartRequest)`** 启动（`@deepseek-ai/dsh-subagent`）。`@athena/dsh` 因此依赖 `@deepseek-ai/cordis` + `@deepseek-ai/dsh-tools` + `@deepseek-ai/dsh-subagent`，且只能在 DSH 环境内编译（athena_ts 无 `@deepseek-ai/*`）。

关键 API（静态读 `.d.ts` 所得）：
- `ctx.tools.register(def: ToolDefinition): () => void`；`ToolDefinition = { name, description, parameters, output: { schema, render }, execute(args, exec): Promise<unknown> }`。
- `ctx.subagents.start(name, { prompt: ContentBlock[], parent: Agent, signal: AbortSignal, outputSchema?, label? }): Promise<SubagentRun>`。
- `SubagentRun = { id, localAgent, result: Promise<SubagentResult>, dispose() }`；`SubagentResult = { output: ContentBlock[], structured?: unknown, stopReason }`。

DSH 插件是 `{ apply(ctx) { ... } }` cordis 插件，Service 经 `ctx.provide(name, value)` 注册、`ctx.get(name)`/`inject: ['name']` 消费；`isolate: {svc: true}` 做 entry-local realm；事件 `ctx.on()`；副作用用 `ctx.effect()` 返回 disposer。

研究域 Services 均为**可直接构造的普通类**（与现有测试一致），插件 `apply()` 只做注册接线，不掺业务逻辑。LLM worker 的 `parent: Agent` 来自驱动 `research_run` 工具的 `exec.agent`（subagent `start` 强制要求 parent）。

## 5. 迁移步骤（后续实现）

1. 建 `packages/athena-dsh/`（npm 包，导出 cordis 插件 `apply(ctx)`，依赖 `cordis` + `@athena/research` 的纯逻辑）。
2. 插件注册 `researchTree/researchState/scheduler/evaluator/scriptRunner` 五个无状态/少状态 Service。
3. `supervisor` Service：`FixedFlowSupervisor` 的 worker 注入改为 `ctx.get('subagents')`/工具调用（DSH 已有），不再用 `WorkerRunner`。
4. 注册 `research/status`、`research/run` 动态工具 + `research-flow` goal。
5. 提供 Athena preset 的 `agent.cordis.yml` 片段（声明 Athena 行），供 DSH 组合 `include`。
6. 对拍测试改为在 `new Context()` 里注册插件后断言 `ctx.get('researchTree')` 等 Service 可用、flow 可用 fake subagent 跑通。

## 6. 开放项

- `supervisor` 的 LLM worker（ideator/plan/validate/prepare/evaluator）具体走 DSH 的 `subagent spawn` 还是 `goal` 域，需先 `cordis_inspect_list` 确认当前 DSH 版本的 subagent/goal Service 签名后再定。
- Athena preset 是独立 agent.cordis.yml 还是并入 `standard` preset，待定。
