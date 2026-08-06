# Athena 全规格收口设计

> 日期：2026-07-29
> 状态：已确认，进入实施
> 范围：当前 `docs/superpowers/specs/` 下全部 Athena 规格

## 1. 目标

本轮完成 Athena 的完整规格收口，而不是只完成目录重命名：

1. 使 Codex 对齐后的 Python package 成为唯一生产实现。
2. 补齐 AI4ML 数据准备、检索、假设生成、评估、排名、执行、实验编排和报告行为。
3. 保持 App Server、IDE RPC、ResearchTree v1 持久化和 Rust 可选回退的外部兼容性。
4. 在所有消费者迁移并通过静态门禁后删除旧 package。
5. 实施 A2.2“窄墨书脊”IDE 视觉规格，不改变 bridge、WebSocket 或业务状态职责。
6. 将纯格式化改动与行为改动分开提交，并排除本地工具运行状态。
7. 以最小状态面降低心智负担：类只保存不可推导的核心属性，简单行为优先使用函数。

## 2. 简洁性约束

本轮重构把“容易理解”作为硬验收条件：

- 一个类只承担一个状态所有者职责。
- 默认每个领域类不超过 7 个持久属性；超过时必须在代码和评审报告中说明不可合并或不可推导的理由。
- 派生值通过 property 或纯函数计算，不在多个字段中重复保存。
- 无状态行为使用模块函数，不为调用一个函数再创建 service class。
- 不创建只有一个字段、没有独立约束的包装类型。
- 不为未来可能出现的实现预建抽象层；只有两个真实实现或明确外部边界时才引入协议。
- `__init__` 不执行 I/O，不隐式启动任务，不复制可从依赖读取的配置。
- 新增或修改的公共函数与构造器默认不超过 4 个直接参数；成组配置优先复用已有配置对象，不为缩短签名新增空洞类型。
- 优先复用现有 `Hypothesis`、`ExperimentPlan`、`GitWorkBranch` 和 ArtifactRef，不创建同义 DTO。
- 单个 public 方法应能从签名看出输入、输出和失败方式；避免同时返回状态、数据和错误三套并行字段。

评审时除正确性外，必须检查属性数量、重复状态、包装层和跨文件跳转数量。

## 3. 规范优先级

本设计统一解释现有规格中的冲突：

1. 本设计是本轮收口的集成合同。
2. 各领域的详细行为仍以原规格为准；出现路径或迁移时序冲突时，以本设计为准。
3. “`core/`、`app_server/`、`ide/` 不变”解释为外部行为和协议兼容，而不是禁止内部所有权迁移。
4. 旧路径 re-export 仅允许作为任务之间的临时脚手架；最终提交不得依赖待删除 namespace。
5. Rust 规格中的“保留 Python 基线”解释为保留迁移后的 Python 行为基线，不要求保留旧目录名。

## 4. 最终 package 边界

最终 Python 域结构为：

- `athena.code`：代码生成适配、执行、监控和确定性审查。
- `athena.core.agent`：唯一 Python Agent、App Server runner 适配和无状态代码 Agent 构造接口。
- `athena.data`：确定性数据检查与处理。
- `athena.retrieval`：论文和模型检索降级链。
- `athena.brainstorm`：可证伪假设生成。
- `athena.evaluation`：冻结评估协议、执行与成对比较。
- `athena.experiment`：实验类型、存储、持久化、排名和编排。
- `athena.integrations.kaggle`：可选 Kaggle 适配器。
- `athena.app_server`：线程运行时及其管理契约。
- `athena.ide`：IDE RPC 编排，不拥有实验树实现。

最终删除集合固定为：

- `src/athena/execution/`
- `src/athena/workflows/`
- `src/athena/core/research/`
- `src/athena/research/`
- `src/athena/knowledge/`
- `src/athena/agents/`

## 5. 类型所有权

以下 package 必须含 `types.py`，并成为对应类型的唯一权威定义：

| Package | 权威类型 |
|---|---|
| `code` | `ExecutionOutput`, `GenerationResult`, `EngineResult` |
| `data` | `ColumnSummary`, `DataProfile`, `ProcessingLog`, `ProcessingRecord` |
| `retrieval` | `PaperRef`, `HFModelRef` |
| `brainstorm` | `HypothesisInput`, `BrainStormResult`, `FalsifiabilityError` |
| `evaluation` | `MetricDef`, `EvalSpec`, `EvalResult`, `ComparisonVerdict` |
| `experiment` | `ExperimentStatus`, `Experiment`, `ExperimentOutcome` |
| `integrations/kaggle` | Kaggle DTO |

容器 package `integrations` 和实现目录 `code/backends` 不需要额外 `types.py`。
`core.schemas` 可以从权威 package 导入并兼容导出仍属于公共协议的类型，但不得保留重复类定义。

## 6. Agent 合同

Agent 接口以 `2026-07-29-athena-agent-interface-consolidation-design.md` 为准：

- `athena.core.agent.Agent` 是唯一 Python Agent 实现。
- App Server 保持现有 `agent_runner`、`ThreadRuntime`、事件和中断逻辑。
- `athena.core.agent.create_code_agent(model, tools, system_prompt, config=None)` 使用同时持有模型标识与 client 的现有 `ResponsesProvider`；system prompt 保持显式，运行配置固定在最后。
- `AgentConfig` 只保存 `max_turns`、`max_tokens`、`temperature` 和 `name`；`Agent` 只保存 `model`、`tools`、`system_prompt` 和 `config`。
- 运行环境分别调用三次 `create_code_agent()`，以不同 prompt、tools 和 config 组装 code、data、plot Agent；接口不接受 `role`。
- prompt 资源归入 `core/agent/prompts/`，代码执行产出约束归入 `code/output_specs.py`。
- `athena.agents` 的消费者迁移完成且静态扫描为零后删除，不保留第二个 Agent、结果 DTO、角色枚举、注册表或兼容别名。

本轮不得修改既有 Agent loop、provider、上下文注入或 App Server 内部逻辑。该门面是未来 Rust core Agent 替换 Python 实现时的稳定边界。

## 7. Experiment 类型与存储合同

### 7.1 类型

`ExperimentStatus` 是小写序列化值的枚举：

- `pending`
- `running`
- `succeeded`
- `failed`
- `cancelled`

`Experiment` 仅保存以下 7 个属性：

- `id: str`
- `parent_id: str | None`
- `commit: CommitHash`
- `hypothesis: Hypothesis`
- `plan: ExperimentPlan`
- `status: ExperimentStatus`
- `outcome: ExperimentOutcome | None`

`ExperimentOutcome` 仅保存以下 3 个属性：

- `eval_result: EvalResult`
- `verdict: ComparisonVerdict | None`
- `is_sota: bool`

`ExperimentOutcome` 属于其 `Experiment`，因此不重复保存 `experiment_id`。指标名来自冻结的 `EvalSpec`，指标结果来自 `eval_result`，工作树句柄属于执行上下文；三者均不作为 `Experiment` 的重复属性。v1 JSON 所需的 `metric_type`、`result` 和 `gitwork` 由持久化适配层映射，不污染 canonical domain model。

所有模型必须可生成 Pydantic schema，并能稳定 JSON round trip。

### 7.2 异步存储接口

`ExperimentStore` 是编排层唯一依赖，包含：

- `upsert_experiment(experiment)`
- `get_experiment(experiment_id)`
- `list_children(parent_id, status_filter=None)`
- `list_descendants(root_id, status_filter=None)`，使用稳定 BFS 顺序
- `best_experiment()`，返回实验 ID或 `None`
- `set_sota(experiment_id, is_sota)`
- `add_hypothesis(hypothesis)`
- `pending_hypotheses()`
- `update_hypothesis_status(hypothesis_id, status)`

根到节点路径通过 `get_experiment()` 和 `parent_id` 的纯 helper 计算，不扩大 Store interface。稳定实验 ID 同时作为图节点 ID，不再另生成无关联 node ID。父节点不存在、环、无效状态和未知 ID 必须显式失败。

`InMemoryExperimentStore` 只保存 `_experiments`、`_hypotheses`、`_sota_id` 和 `_lock`。children、descendants、pending 和 best 等结果按需计算，避免 `_children`、缓存 best 和对象字段之间出现多份状态。并发策略使用单事件循环内的 `asyncio.Lock`。

## 8. IDE v1 持久化

`experiment/persistence.py` 负责本地 ResearchTree 快照，`IDEHandler` 只调用公开接口。

保持现有 v1 JSON 形状：

```json
{
  "version": 1,
  "sota_id": "experiment-id",
  "nodes": {},
  "hypotheses": {}
}
```

每个节点保留 `id`、`parent_id`、`children_ids` 和含 `gitwork` 的 `exp`。要求：

- 默认路径 `.athena/research_tree.json`。
- 继续读取当前 v1 文件；本轮不引入 v2 写格式。
- 写入使用同目录临时文件和原子替换。
- `tree_get`、`tree_save`、`tree_load`、`tree_add_node` 的响应 envelope 保持兼容。
- `tree_save` 和 `tree_add_node` 继续发出 `tree/updated`。
- 不允许 IDE 访问 `_sota_id` 等私有字段。
- Tauri 注册并测试 `tree_get`、`tree_save`、`tree_load` 命令。

兼容字段仅存在于 `experiment/persistence.py` 的序列化/反序列化函数参数和局部值中，不为它们创建第二套长期领域对象。持久化模块不拥有运行中状态。

## 9. App Server 所有权

ThreadManager 的结构合同迁入 `athena.app_server`。`RuntimeThreadManager` 的 `start`、`submit`、`fork`、`interrupt`、`events`、`get`、`aclose` 行为、事件顺序和关闭语义保持不变。

`execution/handlers.py` 中未被生产运行时使用的旧队列循环不迁移；其有价值的契约测试改为 App Server 的结构/生命周期测试。

## 10. AI4ML 行为

### 10.1 数据准备

- 数据工具保持确定性，不调用 LLM。
- 超过 100K 行时以三个固定种子生成分析样本。
- 处理记录包含列、操作、参数和 UTC 时间。
- 生成 train/validation/test 三个互斥分割及可验证 manifest。
- 原始数据副本、清理结果和分割均通过 ArtifactRef 追踪。

### 10.2 检索与假设

- 检索顺序为 arXiv、Semantic Scholar、web、明确标记的 common knowledge。
- 阻塞客户端通过线程卸载或异步客户端调用，不阻塞事件循环。
- LLM JSON、畸形输出和无 LLM fallback 均有测试。
- 每个假设必须有来源、单实验干预和可验证预期效果。

### 10.3 评估与排名

- `EvalSpec` 冻结，评估重复执行具有幂等性。
- 比较器读取 per-sample 结果，按指标方向执行成对检验。
- 明确区分 improvement、noise/tie 和 regression。
- 排名实现 Bradley-Terry 更新与探索项；近重复假设受到更大惩罚，排序在固定种子下稳定。

### 10.4 代码执行

- `CodeEngine` 失败耗尽轮次时返回失败，不得伪造成功。
- monitor 实施超时取消和 stall 信号。
- deterministic review 检查变更范围、禁用文件和未声明依赖，并进入 revise 循环。
- `OutputSpec` 在 Agent 结果判定中生效。
- Codex/Qoder 适配器通过可选依赖或 CLI 边界加载；核心 package 在未安装外部 SDK 时仍可导入。
- 自动测试使用 fake backend，不调用真实外部服务。

### 10.5 实验编排

- PREPARE、SEARCH、VALIDATE、REPORT 全部依赖 canonical package。
- 每次实验创建/更新 store checkpoint，并维护单调 `state_version`。
- candidate、baseline、tie、失败、暂停、恢复、停止均产生明确状态和事件。
- async 路径不调用阻塞 `input()`。
- SEARCH 不访问最终 test split；最终测试只执行一次。
- 报告包含数据摘要、完整假设链、每次实验指标/裁决/p-value、消融和最终测试。

## 11. 错误处理

- 参数和结构错误尽早失败并包含稳定错误类型。
- 可降级的检索/外部集成错误进入下一适配器，并保留来源说明。
- 单实验执行失败记录为 failed checkpoint；是否继续由 Supervisor 与预算决定。
- 持久化损坏、未知版本和原子替换失败不得覆盖最后一份有效文件。
- App Server、IDE 与 Tauri 边界返回协议化错误，不泄漏未处理异常或后台任务。

## 12. A2.2 IDE 视觉实现

完成后端行为迁移后，实施 `2026-07-29-athena-ide-research-studio-design.md`：

- `ResearchMasthead` 取代已删除 TopBar 的品牌/四态展示职责。
- `AppShell` 仍为无业务状态的插槽布局。
- 会话书脊、研究画布、连续台账和按需详情遵循目标尺寸与响应式规则。
- 展示映射集中到纯函数模块，不复制 `usePipeline` 业务状态。
- 使用本地字体和 Lucide 图标。
- bridge、WebSocket 和后端协议不变。
- Playwright 截图检查 1440x900 与 1024x720 的重叠、溢出、截断和控制台错误。

## 13. 迁移与删除顺序

严格按以下顺序实施：

1. 文档和 canonical contract。
2. 类型所有权与 Agent 拆分。
3. Experiment 类型、store 和 v1 持久化。
4. Evaluation、Data、Retrieval、Brainstorm、Ranking、Code 行为。
5. Experiment 编排。
6. App Server 所有权迁移。
7. IDE Python/Tauri 持久化迁移。
8. 测试、示例和真实 fake-backed E2E 迁移。
9. 静态证明旧依赖为零后删除旧目录。
10. A2.2 IDE 视觉重构。
11. Python 格式化独立提交。
12. 全栈验证和最终评审。

任何删除不得早于其消费者迁移和覆盖测试。

## 14. 测试与发布门禁

每项行为变更必须先有正确失败的测试，再实施最小修复。最终门禁为：

- `uv sync --locked`
- `uv run pytest`
- 旧 namespace 静态 import 扫描无匹配
- `npm test -- --run`
- `npm run build`
- Tauri `cargo test`
- Rust workspace fmt、clippy 和 test
- `git diff --check`
- 目标尺寸浏览器截图和控制台检查
- 全分支规格与质量评审
- 简洁性门禁：领域类属性数、重复状态、无状态 service class 和无必要包装类型审查

## 15. 提交与排除规则

- 每个行为任务使用聚焦提交并经过任务级评审。
- Python 纯格式化改动单独提交，不与行为迁移混合。
- 不提交 `.claude/`、`.superpowers/brainstorm/`、`.superpowers/sdd/` 或其他运行时状态。
- 不覆盖任务外的现有用户改动；发生并发修改时先重新读取并合并。

## 16. 完成定义

完成必须同时满足：

1. 全部 canonical package 和行为合同已实现并测试。
2. App Server 与 IDE 不再依赖旧 package，外部协议保持兼容。
3. 全部生产、测试和示例旧 import 为零，旧目录已删除。
4. v1 ResearchTree 文件可继续读取，前端保存/加载链路可用。
5. A2.2 视觉规格通过自动和截图验收。
6. Python、前端、Tauri 和 Rust 全部验证通过。
7. 文档、代码和最终评审不存在未裁定 blocker。
8. 新增领域类满足简洁性约束；任何超过 7 个持久属性的例外均有明确理由和测试。
