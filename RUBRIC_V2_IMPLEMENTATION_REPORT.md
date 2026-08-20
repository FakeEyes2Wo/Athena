# Athena Rubric V2 实施报告

## 最终状态

核心功能已完成并实际接入 Athena：Research Evaluation Rubric 会在任务理解完成后、Evaluator 冻结前生成并持久化；Hypothesis Ranking Rubric 会在现有 Idea Generation Gate 通过后、Selector 排序前按批生成。真实付费 LLM/NLP 端到端运行和消融实验未在本次环境中执行，详见测试报告与对应指南。

## 项目现状 Audit

- 原任务理解在无法确定指标时会默认 `accuracy` / `maximize`，可能把未知科学目标伪装成已确定目标。
- Evaluator 在 PREPARE 中冻结，但此前没有收到独立、冻结的 Evaluation Policy。
- `Supervisor._direction` 是 Plan、Trusted Evaluator 比较、SOTA 替换和 VALIDATE 的权威方向来源。
- Idea Generation Gate 已存在并在 `AgentTurnRunner._finish_ideator_batch` 完成；本次保持 Gate 职责不变。
- Selector 原有 `rubric_prior` 是“是否有来源 + intervention 是否具体”的同步启发式，适合作为无模型/失败时的稳定 fallback，不足以充当正常路径的科研评审。
- `resume.json` 已有小型 artifact ref 持久化机制，适合新增 Evaluation Policy 引用。

## 实际实现内容

### Layer 1：Research Evaluation Rubric

输入由 deterministic code 组装，包含研究任务、任务理解、Human/Official/Protocol 三类指标候选及方向、轻量数据上下文、评价可行性、允许的指标能力与证据引用。代码按下列固定优先级锁定 Primary：

1. Human explicit requirement
2. Official benchmark / competition metric
3. Evaluation protocol metric
4. AI recommendation

正常路径调用无工具、结构化输出的 Evaluation Rubric Agent。输出经严格 Pydantic schema 与 capability registry 校验，形成冻结的 `EvaluationPolicy`：

- `primary_metric`
- `direction`
- `metric_source`
- `locked`
- `secondary_metrics`
- `guardrails`
- `weights`
- `confidence`
- `explanation`
- `evidence_refs`
- 固定的 `selection_mode = "primary"`

能力表只用于验证实现能力和规范化别名，不根据“分类”“不平衡”等简单规则替科研任务选择 Accuracy、AUC 或 F1。空指标、不支持指标、方向冲突和未知证据引用会被拒绝并最多重试一次。

Policy 在 PREPARE 之前保存为 artifact，并将引用写入恢复状态。恢复时验证并复用 Policy，同时把其中的方向重新应用到 Runtime 和 Supervisor。Evaluator prompt/context 会直接收到冻结 Policy；`metric.json` 必须声明匹配的 `primary_metric` 和 `direction` 才能冻结。

### Layer 2：Hypothesis Ranking Rubric

只有 Gate PASS 的候选会进入批量排名。排名上下文包含：

- Layer 1 冻结 Policy
- 研究任务
- EDA handoff/report 的有界文本
- Evaluator handoff
- baseline 与当前 SOTA
- 最近研究历史
- 候选 hypothesis、来源、artifact evidence 和 cost
- 搜索预算与并发环境
- 适用性优先的 NLP/LLM 风险清单

无工具、结构化输出的 Hypothesis Rubric Agent 为每个候选返回：`verifiability`、`historical_difference`、`eda_evidence`、`feasibility`、`cost_penalty`、`leakage_risk`、`confidence`、逐维理由、总说明及 evidence refs。

Deterministic code 强制输出 ID 与输入 ID 一一对应、唯一且不多不少，也禁止引用未出现在上下文中的 artifact。每条完整评审单独持久化，Hypothesis 只保存 `rubric_score` 和 `rubric_ref`。

透明聚合公式为：

```text
score = clamp(
    0.20
    + 0.25 * verifiability
    + 0.20 * historical_difference
    + 0.20 * eda_evidence
    + 0.15 * feasibility
    - 0.10 * cost_penalty
    - 0.10 * leakage_risk,
    0.0,
    1.0,
)
```

`confidence` 与 explanation 用于报告和审计，不暗中改变该公式。

## 数据流

```text
用户任务
  -> Supervisor 任务理解（未知指标保持 null）
  -> Evaluation Rubric Agent
  -> deterministic precedence/capability/evidence validation
  -> frozen EvaluationPolicy artifact + resume ref
  -> Evaluator 使用 Policy 生成并冻结 metric.json/evaluate.py
  -> baseline / SEARCH
  -> Ideator 提案
  -> 现有 Idea Generation Gate
  -> Hypothesis Rubric Agent（一次批量评审）
  -> deterministic ID/evidence validation + aggregation
  -> review artifact；Hypothesis 保存 score/ref
  -> 同步 deterministic Selector
  -> Plan / Trusted Evaluator / SOTA
```

## LLM 与 deterministic code 的边界

LLM 负责：理解科研上下文、在没有更高优先级明确指标时提出科学评价建议、解释 secondary/guardrails、按证据评审候选假设并输出理由。

Deterministic code 负责：来源优先级、schema、指标能力和方向校验、证据引用白名单、精确批 ID、重试上限、分数公式、范围截断、持久化、恢复、Selector 排序以及唯一 Primary 的 SOTA 比较。

Selector 内没有新增 LLM 调用，因此仍是同步且可复现的决策函数。

## Fallback 与失败行为

- 已有 Human/Official/Protocol Primary：LLM enrichment 两次失败后保留经过 capability 验证的显式 Primary，confidence 设为 `0.0`，解释明确说明没有 AI 建议；不伪造 secondary、guardrail 或 evidence。
- 没有任何 Primary：LLM 两次失败或持续返回无效指标时，安全终止在 Evaluator 冻结之前；不会静默切成 Accuracy。
- Hypothesis Rubric：超时、schema 错误、缺/多/重复 ID、未知 evidence ref 或其他异常时，不写 `rubric_score` / `rubric_ref`；Selector 自动使用原样保留的 `rubric_prior`。
- 未被 Selector 选中的候选保持 `PROPOSED`，不会因排名较低自动变成 `REJECTED` 或 `REFUTED`。

## 实际 Integration

- `ResearchRuntime` 注册两类 Rubric Agent，在任务理解后调用/恢复 Evaluation Policy，并在 Supervisor 启动 PREPARE 前应用方向。
- `PhaseRunner` 将当前 Policy 交给 `run_evaluator_plan`。
- `prepare.py` 把 Policy 同时放入 Agent content 与 artifact context，并在 freeze 时核对 `metric.json`。
- `Supervisor.register_hypotheses` 在 graph registration 之前调用批量 Rubric 回调；先由 Supervisor 分配稳定候选 ID，避免模型生成 ID。
- `Selector` 优先使用合法 `rubric_score`，否则调用未改变的 `rubric_prior`。
- `ResearchState` 新增 `evaluation_policy_ref`，支持恢复。

## 为什么这样修改

这些接入点分别是 Athena 已有流程中最窄且语义正确的边界：任务理解与 Evaluator freeze 之间适合固定评价政策，Gate 与 graph/Selector 之间适合做预算优先级评审。这样无需重写 PREPARE、Gate、研究树或 SOTA 算法，也不会让 LLM 进入对时序敏感的 Selector。

## 与原 Athena 的差异

- 未知 Primary 不再被默认成 Accuracy。
- 任务理解同时保存多来源候选，优先级由代码而非模型最终决定。
- Evaluator 不再自行重选 Primary；冻结文件必须与 Policy 一致。
- 正常 hypothesis priority prior 来自有解释、可追溯的 LLM Rubric；原启发式仅在失败时使用。
- 两层输出都成为可恢复/可审计 artifact，核心状态仍只保存小引用。

## Known limitations

- 本次没有使用真实 API Key 跑完整 PREPARE→SEARCH，也没有实际执行 SMS Spam 或 BANKING77；普通测试全部使用 fake/stub。
- 消融方案已提供但未实际执行，因此没有声称 V2 优于 V1。
- GUI/TUI 尚未显示完整 Rubric 维度、解释和 evidence；artifact 与核心模型已可供未来 UI 读取。
- GUI intent preview 内仍有旧的启发式展示逻辑，但它不进入 Runtime 的权威 Policy/SOTA 路径。
- capability registry 当前覆盖常见指标；新指标必须先实现 Evaluator 能力并显式登记，系统才会接受。
- Secondary、guardrails 和 weights 当前仅记录/报告，不参与 SOTA。这是刻意遵守单 Primary 方案 A，而不是功能遗漏。

## Future integrations

- 在 GUI/TUI 增加只读 Policy 与 Hypothesis review 详情页。
- 扩展 capability registry 与 Evaluator 模板，并为新增指标配契约测试。
- 在真实 NLP 运行后根据日志校准 Rubric prompt，而不修改 deterministic 公式来迎合单个数据集。
- 按消融指南记录选择顺序、执行成功率、best-primary 改善、泄漏风险提示和调用成本。
- Dataset Role Review、完整 EDA Review、多目标 SOTA/Pareto 仍保持独立后续项目。

## 验收摘要

- Layer 1、Layer 2、两类正常 LLM 路径、严格验证、持久化、恢复、direction 传递、Evaluator freeze 核对、Selector score 接线和旧 fallback：Completed。
- Rubric 单元/接入测试、研究 integration、广泛回归：Completed，具体例外见 `RUBRIC_V2_TEST_REPORT.md`。
- 真实 NLP validation：NOT RUN。
- Ablation：NOT RUN。
- 完整 Rubric GUI：Follow-up。
