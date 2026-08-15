# Idea Generation：SEARCH 门禁与 ideation 模式开关

本文档说明迁移到 main 架构后的 Idea Generation 实现。最小化后只保留生产实际使用的
light pipeline：`pre_gate + 两视角审阅 + light_hard_gate`，**pipeline 内不做本地排序**——
存活假设按提交顺序返回，由共享的 `ResearchTree`/Supervisor 假设池统一管理，与辩论式
Ideator 使用同一个假设池。

## 1. 一句话概述

Ideator 在 SEARCH 空槽时从 PREPARE 产出的 EDA 工作区探索并提案 **1-5 条可证伪假设**。
这些假设先经过 Idea Generation 质量门禁，再进入共享的 `ResearchTree`：

```
Ideator (ideator_gated_agent.md)
  -> IdeatorHypothesisBatch（富结构化输出）
  -> run_light_pipeline
       pre_gate（结构 + 可证伪性）
       -> methodology/statistics 两视角反方审阅
       -> light_hard_gate（终审）
  -> 存活假设按提交顺序返回
  -> Supervisor.register_hypotheses 写入 ResearchTree（共享假设池）
```

## 2. 模式开关：`--ideation ideageneration|baseline|debate`

CLI 新增 `--ideation` 参数，默认 `ideageneration`：

- `ideageneration`（默认）：Ideator 注册时绑定 `IdeatorHypothesisBatch` 输出契约和
  `ideator_gated_agent.md` prompt；产出经门禁后才入库。
- `baseline`：Ideator 注册时绑定 main 原有的 `HypothesisBatch` 契约和
  `ideator_agent.md` prompt；产出即入库，作为消融对照组。
- `debate`：使用辩论式 Ideator（`agents/ideator/ideator.py` 的
  proposal → review → revision → judge），直接消费共享 `ResearchTree`，默认配置
  3 个 debater、quorum=2。该模式保留自迁移前的实现，目前以最小 `DataProfile`
  （EDA-only 组合根里没有现成的 DataProfile/papers/models 来源）运行。

开关的传递链（刻意不在出口处加 `if`）：

```
cli._runtime_options -> ResearchRuntime.__init__  (self._ideation)
  -> AgentTurnRunner.run_ideator_turn            (debate 走 _run_debate_ideator_turn)
  -> register_ideator_agent(gated=...)           (绑定输出契约与 prompt)
  -> AgentTurnRunner._finish_ideator_batch       (决定是否跑门禁)
```

`prompt_agent.py` 新增 `prompt_agent_type` 参数，让 prompt 文件名与注册名解耦——
同一个 `ideator` 注册名下可以切换两份不同契约的 prompt。

## 3. 生产入口：`gate.run_light_pipeline`

`src/athena/research/idea_generation/gate.py` 是对外唯一入口，签名：

```python
async def run_light_pipeline(
    drafts: list[IdeatorHypothesisDraft], *,
    model: str, artifacts: ArtifactStore,
    progress: ProgressFn = _silent,
    rejections: list[str] | None = None,
) -> list[Hypothesis]
```

- 逐候选执行，单候选失败不影响其他候选（fail-closed）。
- `progress` 回调把每个阶段的小摘要投影成运行事件，避免"在跑"与"卡死"无法区分。
- `rejections` 由调用方传入一个 list，收集被丢弃候选的逐项 rubric 证据，供重新提案使用。
- 返回顺序 = 输入顺序；pipeline 内没有任何排序组件。

## 4. 每个候选的流水线

### 4.1 构造 `HypothesisPackage`

代码从 `IdeatorHypothesisDraft` 构造审计对象 `HypothesisPackage`，其中
`idea_id`（`idea-<12 hex>`）、`generation_strategy`（固定 `eda_grounded`）、
`lineage_op`（固定 `generate`）全部由代码分配——LLM 从不撰写标识符或簿记字段。

### 4.2 结构检查（纯函数，无 LLM）

`pre_gate_checks.structural_check` 检查两条不变量：

1. `premise_evidence_ok`：每个 `SUPPORTED_PREMISE` 至少绑定一个证据 ref。
2. `novel_hypothesis_testable`：`predicted_observations` 与
   `disconfirming_observations` 都非空。

这两条其实已被 Pydantic validator 保证，此处把它们落成可审计的报告对象。

### 4.3 可证伪性审计（LLM 单轮）

`pre_gate_checks.falsifiability_check` 用一次 `single_turn_structured_chat` 调
`FalsifiabilityJudgment`，判断是否存在真正可执行的证伪测试、列出不可观测变量。
调用失败时降级为 `degraded_falsifiability_report`（不可证伪），不静默放行。

### 4.4 `pre_gate`

`gatekeeper.pre_gate` 是廉价前置筛子，只有两项 rubric：

| rubric | 不通过 |
|---|---|
| `evidence_traceable` | 前提无证据或缺少预测/反证 |
| `falsifiable` | 可证伪性审计不通过 |

任一不通过 → `GateVerdict.REVISE` → 候选本轮丢弃，并记录 `blocking_factor`
与该项证据到 `rejections`。

### 4.5 两视角反方审阅

`review_board` 定义两个独立视角（`REVIEW_PERSPECTIVES`）：

- `methodology`：对照/基线是否清晰、混淆变量、相关≠因果、干预是否可操作。
- `statistics`：样本量/功效、多重比较、效应是否可区分于噪声、预测是否可量化。

每个视角一次 `single_turn_structured_chat` 产出 `SkepticJudgment`，再包装为
`SkepticReport`。审阅失败映射为 `failed=True`（fail-closed），从不向外抛。
审阅 prompt 刻意不携带生成侧的任何自评概率。

### 4.6 验证方案

`validation.match_verifier` 用固定 domain `machine_learning` 匹配内置 verifier 表，
只保留 `ablation_replication`（"改一个特征/模型，比较改前改后指标"），再由
`plan_validation` 生成 `ValidationPlan`。找不到 verifier 时不虚构，标 EXPLORATORY。

### 4.7 `light_hard_gate` 终审

`gatekeeper.light_hard_gate` 判定优先级（短路，`blocking_factor` 记第一项）：

1. `evidence_traceable` / `falsifiable` 不过 → `REVISE`
2. 任一视角 `fatal_flaw_found` → `REJECT`
3. 任一视角 `failed` 或 `unaddressed_risks` 超过单项阈值 → `REVISE`
4. 跨视角风险总数超过 `max_total_risks(视角数)` → `REVISE`
5. 无可用 verifier → `EXPLORATORY`
6. 全过 → `PASS`

`PASS` 与 `EXPLORATORY` 都放行；`REVISE`/`REJECT` 本轮丢弃。

**风险阈值（实测校准）**：

- `MAX_TOLERATED_RISKS = 6`。初版取 2，实测审阅稳定产出 4-5 条风险且
  `fatal_flaw_found` 全为 False，取 2 在数学上无人可过；故改以
  `fatal_flaw_found` 为主判据，条数只作"明显失控"的兵线。
- `max_total_risks(N) = 6 * N - 1`。必须严格小于 `6 * N`，否则"总量超标"一项
  与"单项超标"数学上不可能同时存在，会退化为死代码。

## 5. 门禁全拒时的重新提案

`AgentTurnRunner._run_ideator_lane` 中：

- 门禁全拒后，把 `rejections` 里的逐项理由拼成 followup 请求，**在同一个 Ideator
  thread** 上重新提案（保留它原本的探索上下文），要求换实质、不复述被拒假设。
- 上限 `MAX_GATE_RETRIES = 2`。耗尽后发 error 输出，本 lane 返回空——绝不无限重试，
  也绝不静默（真实跑测中曾因全拒后返回空列表导致 SEARCH 停在 RUNNING 不推进）。

## 6. 结构化输出契约

`idea_schemas` 中面向 Ideator 的核心模型：

- `IdeatorHypothesisDraft`：`statement` / `intervention` / `expected_effect` /
  `supported_premises` / `inference_chain` / `predicted_observations` /
  `disconfirming_observations` / `sources`。
- `IdeatorHypothesisBatch`：1-5 条 `IdeatorHypothesisDraft`。

LLM 面向的 `*Judgment` 模型与代码生成的 `*Report` 模型分离；`GateVerdict` 只在
`gatekeeper` 里产出。

`structured_chat.single_turn_structured_chat` 是本分支特有的补充：main 自带的
`single_turn_chat` 只返回纯文本、无 `output_type`，而门禁每次调用都需要经过
Pydantic 校验的结构化对象，因此它直接用 `core.agent.Agent` 的
`output_type`/`artifacts` 能力构建一次性结构化调用。

## 7. 文件结构（最小化后）

```
src/athena/research/idea_generation/
  gate.py              生产入口 run_light_pipeline，编排门禁
  gatekeeper.py        pre_gate / light_hard_gate 判定逻辑
  pre_gate_checks.py   结构检查 + 可证伪性审计
  review_board.py      methodology/statistics 两视角审阅
  validation.py        Verifier 匹配 + ValidationPlan
  prompts.py           门禁各阶段 prompt
  idea_schemas.py      门禁用结构化数据模型
  structured_chat.py   单轮结构化输出调用
  __init__.py          包说明
```

已删除（最小化中移除）：`ranking.py`（pipeline 本地 Elo 排序，排序职责回归共享
`ResearchTree`/Supervisor 假设池）、`workflow.py`、`revision.py`、
`candidate_generation.py`、`evidence_retrieval.py`、`state.py`、
`hypothesis_selector.py`，以及对应的 3 个测试文件。

## 8. 测试与验证

- `test/unit/idea_generation/`：覆盖模式开关、light pipeline、门禁进度、
  全拒重试上限、风险阈值校准等。
- `test/unit/test_cli.py`：`--ideation` 参数解析（默认 ideageneration，baseline/debate
  透传）。
- `tests/test_ideator.py`：辩论式 Ideator 44 个测试（proposal/review/revision/judge
  与 quorum/超时等）。
- 真实验证见 PR #11 描述：MazeCrawler（Kaggle Simulations）上 gated 583.09 vs
  baseline 301.14（各 n=1，结论有限）。
