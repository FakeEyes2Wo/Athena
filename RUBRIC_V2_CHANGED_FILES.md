# Rubric V2 修改文件报告

本报告以用户提供的原始 `Athena-main.zip` 为基线。生成环境、`.venv`、缓存和打包产物不计入源码 diff。

## Production：新增

### `src/athena/agents/rubric_agent.py`

【新增】注册两个无工具、结构化输出的 Rubric Agent。必须新增，以把 LLM 科研判断限制在严格输出契约内。

### `src/athena/core/agent/prompts/evaluation_rubric_agent.md`

【新增】定义 Layer 1 的来源优先级、单 Primary、证据与失败要求。必须新增，避免复用职责不同的 Supervisor/Evaluator prompt。

### `src/athena/core/agent/prompts/hypothesis_rubric_agent.md`

【新增】定义 Layer 2 的各维度、解释、批 ID 和 NLP 风险要求。必须新增，保持 Ranking 与 Gate 的边界。

### `src/athena/research/rubrics/__init__.py`

【新增】Rubric 子包公共导出。必须新增，为两层契约与逻辑提供清晰模块边界。

### `src/athena/research/rubrics/models.py`

【新增】严格定义 Evaluation context/draft/policy 与 Hypothesis ranking context/review/batch。必须新增，用 schema 阻止空字段、越界分数、额外字段和重复 ID。

### `src/athena/research/rubrics/evaluation.py`

【新增】实现 Human > Official > Protocol > AI、metric capability/direction 校验、重试和安全失败。必须新增，使决定权与校验权不依赖 prompt 自觉。

### `src/athena/research/rubrics/ranking.py`

【新增】实现透明聚合、精确批 ID/evidence 校验和 NLP/LLM 风险上下文。必须新增，让 Selector 前的 AI 输出可验证、可复现。

## Production：修改

### `src/athena/agents/supervisor_agent.py`

【修改】原来负责 Supervisor 结构化工具和任务理解；现在去掉 unknown→accuracy 默认，保存 Human/Official/Protocol 各来源候选，并校验 unresolved/方向一致性。必须修改，因为这是错误默认值的来源。

### `src/athena/core/agent/prompts/evaluator_agent.md`

【修改】原来指导 Evaluator 生成冻结评价器；现在要求以冻结 Policy 为权威，并在 `metric.json` 声明 Primary 与 direction。必须修改，确保实际评价器不自行改指标。

### `src/athena/core/agent/prompts/supervisor_agent.md`

【修改】原来指导任务理解；现在要求捕获全部指标来源、未知保持 null，并说明 deterministic precedence。必须修改，使结构化上下文完整且不猜指标。

### `src/athena/core/research_models.py`

【修改】原来定义 Hypothesis；现在新增可选 `rubric_score` 和 `rubric_ref`。必须修改，使批评审结果真正进入研究树与 Selector，又不把完整解释膨胀进主状态。

### `src/athena/research/agent_turn_runner.py`

【修改】原来运行 Supervisor/Ideator/General turns 和 Gate；现在组装两层上下文、运行两类 Rubric Agent、重试、校验 evidence/ID、持久化 review，并把 Layer 1 Policy 注入 Ideator。必须修改，这是 Gate 后、注册前的实际 LLM 接入点。

### `src/athena/research/phase_runner.py`

【修改】原来编排 PREPARE/SEARCH/VALIDATE；现在把已加载的 Evaluation Policy 传给 Evaluator plan。必须修改，保证 Policy 在 freeze 前可见。

### `src/athena/research/runtime.py`

【修改】原来是组合根；现在注册 Rubric Agents，在任务理解后恢复或生成 Policy，应用权威方向，并在无 Primary/LLM 失败时阻止错误 freeze。必须修改，组合根负责正确生命周期顺序。

### `src/athena/research/supervisor/prepare.py`

【修改】原来创建并冻结 Evaluator；现在将 Policy 注入 content/context，并在 freeze 时核对 `metric.json` 的 Primary/direction。必须修改，防止 prompt 与实际文件脱节。

### `src/athena/research/supervisor/ranker.py`

【修改】原来使用 `rubric_prior`；现在优先使用已持久化 AI `rubric_score`，缺失时调用完全保留的原 fallback。必须修改，让 V2 实际影响排序且 Selector 不调用 LLM。

### `src/athena/research/supervisor/state.py`

【修改】原来保存恢复字段；现在新增 `evaluation_policy_ref`。必须修改，支持断点续传且只保存小引用。

### `src/athena/research/supervisor/supervisor.py`

【修改】原来负责 graph registration、Selector 和权威 direction；现在可应用/持久化 Policy，并在假设注册前调用批量 Rubric callback。必须修改，确保顺序是 Gate→Ranking→registration→Selector。

## Tests：新增

### `test/unit/research/rubrics/__init__.py`

【新增】将 Rubric 测试目录作为明确测试包。必须新增以保持测试组织一致。

### `test/unit/research/rubrics/test_evaluation_rubric.py`

【新增】覆盖来源优先级、unknown、retry/failure、invalid/empty/unsupported、minimize、单 Primary、resume、Evaluator mismatch 和 Runtime direction。必须新增，验证 Layer 1 的科学与安全边界。

### `test/unit/research/rubrics/test_hypothesis_rubric.py`

【新增】覆盖维度范围、公式上下界、惩罚、说明、ID integrity、evidence refs、NLP prompt、Selector preference/fallback。必须新增，验证 Layer 2 的 deterministic 边界。

## Tests：修改

### `test/integration/research/test_task_seeding.py`

【修改】原来验证任务启动/恢复；现在 hermetic fake 同时提供任务理解与 Evaluation Policy。必须修改，因为真实 Runtime 在 PREPARE 前新增了安全必需的 Policy 阶段。

### `test/unit/kaggle/test_supervisor_gate.py`

【修改】原来验证任务理解工具转发；现在提供并断言 Human metric provenance。必须修改，以符合不允许含指标但 source unresolved 的新契约。

### `test/unit/research/supervisor/test_ideator_wiring.py`

【修改】原来验证 Gate/Ideator 接线；现在验证 PASS 后、注册前进行排名，以及失败时保留无 AI 分数并走 fallback。必须修改，证明真实 integration 顺序。

## Docs / Delivery：新增

### `RUBRIC_V2_IMPLEMENTATION_REPORT.md`

【新增】完整 Audit、架构、数据流、边界、fallback、限制和后续接入说明。

### `RUBRIC_V2_CHANGED_FILES.md`

【新增】逐文件解释本次真实 diff 与统计。

### `RUBRIC_V2_BEGINNER_GITHUB_GUIDE.md`

【新增】面向 Git/GitHub 初学者的 branch、commit、push、PR 与同步指南。

### `RUBRIC_V2_LOCAL_TEST_GUIDE.md`

【新增】基于本仓库 `uv`/pytest 配置的本地验证和排错指南。

### `RUBRIC_V2_NLP_TEST_GUIDE.md`

【新增】SMS Spam 真实端到端验证步骤、记录表与故障排查。

### `RUBRIC_V2_ABLATION_GUIDE.md`

【新增】V1/V2 Ranking 与 Evaluation Policy 对照实验方案。

### `RUBRIC_V2_TEST_REPORT.md`

【新增】仅记录本次确实执行的命令、结果与未运行项。

### `RUBRIC_V2_PR_TEMPLATE.md`

【新增】可复制到团队 PR 的完整模板，NLP/消融明确标为 pending。

### `codex_docs/2026-08-20-rubric-v2-completion-report.md`

【新增】按仓库开发规则记录计划完成情况与验证证据。

## Docs：修改

### `codex_docs/CURRENT.md`

【修改】原来指向当前开发计划；完成后清除 active plan 并指向完成报告。必须修改以符合 Athena 自身计划生命周期规则。

## 汇总

```text
Production files changed: 18
Test files changed: 6
Docs/delivery files changed: 10
Total files changed: 34
```
