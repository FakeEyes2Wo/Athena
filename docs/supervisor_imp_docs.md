# Athena Supervisor Implementation Plan

> 历史实现计划：本文保留当时的路径和决策记录，不代表当前目录结构。当前实现入口见
> `src/athena/research/runtime/`、`src/athena/research/supervisor/` 与
> `docs/research_core_mechanisms_ch.md`。

> Status: historical implementation plan. The current research layout and ownership
> are recorded in [`codex_docs/2026-09-02-research-layout-simplification-goal.md`](../codex_docs/2026-09-02-research-layout-simplification-goal.md).
> The numbered task snapshots below are retained for audit history and are not a
> source of truth for present module paths.

**Goal:** 用确定性的 `Planner -> Validator -> Executor` Supervisor 接管 Athena 的完整研究流程，通过 `ResearchRuntime` 和 `Athena-cli` 从任意数据目录完成 `PREPARE -> SEARCH -> VALIDATE -> COMPLETED`。

**Architecture:** `ResearchRuntime` 只负责装配、公开命令和 execution 生命周期；Supervisor 每次只生成一个可恢复的一步式 Plan。SQLite 只保存控制状态、Plan/Operation、预算和 Artifact refs，报告、脚本、模型、图和日志全部保存在内容寻址 `ArtifactStore`。所有业务数据解释和读取由 LLM 生成的 `python-uv` Bundle 完成，确定性服务只负责隔离执行、合同校验、排名、事实提交和恢复。

**Tech Stack:** Python 3.11+、Pydantic、SQLite/WAL、`uv`、现有 `AgentRuntime`、`LocalArtifactStore`、`ResearchTree`、`LocalGitWorkspace`、`CodeEngine`、pytest/pytest-asyncio。

## Current Snapshot

> 最近巡检：2026-08-10 16:09:10 +08:00。冻结快照执行一次完整验证，结果为 `237 passed in 36.83s`。验证结束后检测到 `base_runner.py`（16:08:46）和 `runtime.py`（16:09:07）的并发写入；本快照不读取、不合并这些后续改动，也不据此重跑验证，留待下一轮审计。

真实入口已从全新临时项目跑通：

```powershell
uv run .\src\main.py
```

该次运行耗时 87.2 秒，依次到达 `PREPARE -> SEARCH -> VALIDATE -> COMPLETED`。角色脚本通过通用列集合规则识别 `train.csv`/`test.csv`，得到 `target_column=Survived`；Init 和 EDA 均消费同一 target。`main.py` 只在 `(phase, status, state_version)` 改变时打印状态，失败 execution 返回非零退出码。

本轮已确认并修复四个真实阻塞点：

- DeepSeek V4 默认 thinking 会耗尽 token 而不返回工具调用；provider 通过官方 `thinking.type=disabled` 关闭 thinking。等待或 retry 不是根因。
- DataAgent 已拆分 `role`/`eda`，LLM 只生成 `analysis.py`，框架使用 `sys.executable` 执行一次；不做脚本修复或重试。role proposal 使用 `DatasetRoleProposal.model_validate_json()` 后单独提交。
- 冻结 evaluator 统一接收 `--request/--output`，`eval.py` 将结果写入 `result.json`；不再依赖 stdout 猜测结果。
- Windows shell 子进程保留 home 变量并使用 `-NoProfile -NonInteractive`，真实 `pwsh` 调用返回码为 0、stderr 为空，不再触发 conda profile 的 `Could not determine home directory`。

SEARCH 首版只有一组确定性 fallback 候选，因此首轮选出 SOTA 后立即提交 `SEARCH_STOP` 并进入 VALIDATE，避免固定幂等键重复生成空计划。这个行为用于先跑通主链路，不代表真实多轮搜索已经完成。

仍未完成的生产能力：Ideator/Code 仍是确定性 fallback，不是真实训练/预测 Bundle；Reflection 入口仍使用确定性 fallback；DataAgent 仍固定 `analysis.py` 而未迁移到通用 `python-uv` Bundle；没有自动 API retry，首个 LLM 事件前的外部失败会让 execution 直接 FAILED；`strong_isolation=false`。这些限制不得通过新增兜底类、宽泛异常捕获或猜测性修复掩盖。

冻结验证命令：

```powershell
.venv\Scripts\python.exe -m pytest test/unit/agent test/unit/research test/unit/test_agent.py test/unit/test_cli.py test/unit/test_init_agent.py test/unit/test_main.py tests/test_research_runtime.py tests/test_supervisor_recovery.py -q
```

## Previous Snapshot (2026-08-10 13:50)

> 最近巡检：2026-08-10 13:50:02 +08:00。证据来自该时刻冻结的主工作树快照；排除 `.venv`、`node_modules`、`.worktrees`、`.superpowers` 和 `.claude/worktrees` 中的依赖、缓存及隔离副本。每次文档修改只对冻结快照运行一次验证，随后出现的并发改动留到下一轮。本轮已从全新临时项目运行真实入口并单独复现 DataAgent；两次均以失败终止，运行进程已停止。

本计划以动态工作树为准，而不是仅根据设计文档或 Git 提交状态推断进度。当前实现状态如下：

| 范围 | 当前证据 | 状态 |
|---|---|---|
| Task 1：合同与事实投影 | `contracts.py` 已定义 `ExecutionConfig`、Dataset/Eval/EDA/baseline/ranking/ablation/final-test 合同；`ProjectFacts` 和 `project_phase()` 已切换到明确的 `*_ref` | 已实现并通过局部测试，尚未提交 |
| Task 2：Journal、lease 与恢复内核 | `PlanJournal` 已包含持久化 execution、lease generation/fencing、幂等 operation/fact/budget 提交、可对账 dispatch、event/outbox；Executor 与 Coordinator 已使用 lease、heartbeat 和 durable dispatch 身份 | 已实现并通过局部测试，尚未提交 |
| Task 3：Runtime 与 CLI 生命周期 | 已移除确定性 intent/task/data/metric 推断和 `--detach`；Runtime 自动注册 worker 并装配静态 `ResearchServices`；Executor 可解析 service/worker/fact 引用并原子提交 facts。`main.py` 仅在 `(phase,status,state_version)` 变化时打印状态，仍每 2 秒读取 STATUS/rollout；回归测试覆盖相同状态无重复行/heartbeat | failed Plan 不会把 execution 置为终态，也未投影到 STATUS；真实 `wait:data` 失败后 execution 仍为 RUNNING，入口会安静地永久轮询。subscriber 与 `aclose()` 仍有广泛 `except Exception`；主链路跑通前不扩展这些兜底 |
| Task 4：通用 LLM 数据 Bundle | `script_runner.py` 已实现源码树、environment hash、`uv sync/run --frozen` 和动态 entrypoint；受管路径 fact 已用于首个 DataAgent、InitAgent、独立 EDA、SEARCH 和 final-test。真实 DataAgent 已从受管 Titanic 目录生成分析脚本、proposal、报告和 5 张图 | baseline Ideator/Code payload 仍没有 `data_path`；所谓“all data reading workers”测试遇到缺失字段会跳过。DataAgent 仍固定 `analysis.py`/`python analysis.py` 和 CSV/pandas，inspect 未冻结为通用 Bundle；真实 train/predict bundle 尚未进入 Planner |
| Task 5：PREPARE | 六步 Planner 已加入 role/EDA review、同源 follow-up、修订上限与 HumanRequest；注入真实 client 时 Reflection 已选择结构化 `DatasetRoleReview`/`EDAReview` 输出类型；PREPARE 已冻结 eval bundle并调用 baseline evaluator | CLI 没有注入 Reflection client，实际仍走确定性 fallback。角色发现与完整 EDA 仍混在最长 20 turns 的 DataAgent 循环；proposal 可缺失、未经 Pydantic 校验，Planner 提升整个 Bundle。一次真实运行完整产出后仍 RuntimeError，另一次写坏脚本、缺 report/proposal 后失败；角色冻结、真实 follow-up、baseline 训练/预测均未到达 |
| Task 6、8、9、11：SEARCH/VALIDATE/恢复/验收 | SEARCH/VALIDATE 的确定性 evaluator、唯一 FinalTestAttempt、同步/后台 Coordinator 和 reopen 测试可到 COMPLETED | 真实流程仍未越过 PREPARE。旧项目恢复把 `snapshot_version=1` 的未完 Plan 对上 current=4，Validator 报 stale 并把 Plan 标为 failed；execution 仍 RUNNING。确定性完成依赖 fake workers/fallback predictions，不能证明真实 train/predict、reproduction、ablation 或 Titanic 验收 |
| Task 7：决策与 repair | 第 50 轮新增但未接线的 3 个模型、`decisions.py`、Journal auto-decision 表/API 和 16 个局部测试已全部移除；扫描无悬空引用 | 按“先跑通、保持最小设计”原则暂缓。先贯通真实主链路；只有出现可复现故障后再实现最小 repair/decision 行为，避免提前恢复被删除的字段、函数和纠错层 |
| Task 10：旧流程清理 | 旧 `SupervisorAgent`、ReportAgent prompt、`ProjectRuntime` 和 `test/unit/test_project_runtime.py` 已删除；9 个仍有效的 Runtime/Coordinator/恢复/worker 行为测试已迁入 `tests/test_research_runtime.py`；App Server diff 为空，完整确定性测试通过 | 代码与测试迁移已完成；旧 `docs/myplan`/`docs/architecture` 文档仍推荐 `ProjectRuntime`、旧阶段命令和 REPORT 流程，静态清理门尚未通过 |

第 71/72 轮继续沿真实入口审计，而不是增加 retry。Planner 的 `_data_aware_payload()` 已让 Init、独立 EDA、SEARCH 和 final-test worker 读取受管 raw，真实 DataAgent 也确实读取了该目录。Reflection 的真实 LLM builder 已按 `kind` 选择 Pydantic review 输出合同，但 CLI composition root 只传 model、不传 client，实际入口仍使用确定性 fallback。baseline 两个 worker 仍只收到静态 content；现有“所有数据 worker 使用 managed root”测试只对已经带 `data_path` 的 payload 断言，因此没有覆盖这一缺口。

真实运行给出了更窄的根因。全新 `main.py` execution 在约 59 秒后令 `wait:data` 失败；workspace 已生成 `analysis.py`、合法 proposal、完整报告和 5 张图，说明 DeepSeek、目录读取与脚本执行均工作，但内层 turn 收尾仍抛 RuntimeError。随后直接派发同一 DataAgent 的复现耗时 89 秒：LLM 生成脚本和 5 张图，最后一次修订却把 Python 字符串写坏，未生成 `report.md`/proposal，外层明确触发 `analysis script did not produce report.md`。默认 `max_turns=20`，因此问题不是没有等待或 retry，而是把角色发现、脚本修复、完整 EDA 和 proposal 塞进同一长 ReAct 循环；增加 turns/retry 只会放大不稳定性。

合同仍未闭合：proposal 是可选 Bundle 文件，没有 `DatasetRoleProposal.model_validate_json()`；Planner 提升的是整个 DataAnalysis Bundle，不是独立已验证 proposal Artifact。角色发现与 EDA 尚未分离，`dataset.freeze_roles_and_splits` 未接入；REVISE 仍把普通修订文本交给要求顶层 JSON/data_path 的 DataAgent。Reflection 的确定性读取路径还用多个广泛 `except Exception` 把坏 ref 降为空文本，与“少兜底、先跑通”原则不一致。

当前最强生产证据是本轮全新临时项目的真实失败运行，而不再是旧单 CSV PermissionError。目录输入问题已经跨过，但 PREPARE 仍未完成；failed Plan 没有让 execution FAILED，STATUS 保持 PREPARE/RUNNING。另一个旧项目恢复未完 Plan 时因 `stale snapshot_version: plan=1, current=4` 失败，证明现有 reopen 测试没有命中真实的“Plan 已提交前序 service facts、worker 在途、随后恢复”边界。下一步应先拆分 role proposal 与 EDA 两个短 turn，并用现有 Pydantic 合同校验 proposal；不增加 timeout/retry/路径猜测，也不反复重跑同一 execution。

最近一次重新执行：

```powershell
.venv\Scripts\python.exe -m pytest test/unit/agent test/unit/research test/unit/test_cli.py test/unit/test_init_agent.py test/unit/test_main.py tests/test_research_runtime.py tests/test_supervisor_recovery.py -q
```

第七十二轮对 13:50:02 冻结快照只运行一次相关验证，结果为 `197 passed in 24.71s`。它覆盖状态输出去重、Reflection 的 fake structured-output 路径、managed-root 投影、`SEARCH_NO_SOTA`、同步/后台 Coordinator、reopen、baseline/SEARCH/final-test evaluator 及现有恢复用例。该结果只证明确定性测试面；真实入口和直接 DataAgent 复现均失败，且现有测试没有覆盖 incomplete Plan 自身推进 state_version 后的恢复、failed Plan 终态投影、真实 Reflection client、两阶段 proposal/EDA 或 baseline 数据输入。最近广域证据仍为第二十轮的 `739 passed, 25 subtests passed in 33.51s`。权威设计哈希 `9FA5D5A9C67CD8988599BA747329B87AAD8919D42671A87E18F86A989934CE37` 未变，App Server diff 为空。

迁移约束审计还发现 `src/athena/workflow.py` 在当前工作树中有改动；本轮巡检没有读取或修改该文件。完成 Task 10 前必须由实现者确认该差异满足“历史 workflow 零读取/零修改”的门槛，且 `git diff -- src/athena/app_server` 应继续为空。

## Global Constraints

- 以 `docs/supervisor_design.md` 为最终产品合同；本计划只决定实现顺序和文件职责，不降低设计门槛。
- 每轮审计首先寻找对完整 `PREPARE -> SEARCH -> VALIDATE -> COMPLETED` 生产链路的推进证据：真实 Planner 消费点、service operation、阶段 gate、权威事实提交和端到端运行优先于局部 helper、恢复分支与防错测试。
- 首轮优先贯通 `PREPARE -> SEARCH -> VALIDATE -> COMPLETED`；主链路跑通前，不为尚未发生的错误新增 repair/retry、失败指纹、额外状态或兜底分支。
- 保持最小必要设计：类只保留当前边界所需字段，函数必须有当前生产消费者；结构校验优先复用 Pydantic 等已采用的成熟能力，不手写重复校验或自研可由成熟库承担的机制。
- 纠错机制必须由可复现故障驱动。事务回滚、唯一约束和 fencing 属于数据正确性，不视为可省略的预防性兜底。
- 不修改 `src/athena/app_server/` 中任何文件。
- 不得读取、导入、执行或迁移 `src/athena/workflow.py` 的任何逻辑。该文件只保留自身停用 docstring。
- 删除 `ProjectRuntime`、旧 `SupervisorAgent`、REPORT phase、ReportAgent 及其生产引用；不提供兼容 facade。
- `ResearchRuntime` 是唯一公开 Python 门面；`Athena-cli` 是首版唯一支持的用户入口。
- TUI/GUI 只作为未来 adapter，不复制 Supervisor 状态机，也不在本轮接入 App Server。
- 原始文件字节和原始列永久不可变。派生数据只能追加不冲突的新列，并保留原 schema、行身份、逐值哈希和 split 边界。
- 平台不得按文件名、扩展名、列名或已知数据集结构推断业务语义。所有实际读取、解析、inspect、split、sample、EDA 和 transform 都由 LLM 生成脚本完成。
- Python 脚本必须使用 `uv`。脚本文件名不固定，只固定 Bundle metadata 声明的唯一 entrypoint 和 CLI+JSON 调用合同。
- EDA 的主要产物固定为 `eda_report.md`；图、脚本、日志和检查结果是独立 Artifact，由 Markdown 引用。
- SQLite 不保存 Markdown、脚本、图片、模型、预测或日志正文，只保存小型元数据、状态和 Artifact refs。
- SEARCH 默认 `ideator_count=3`、`hypotheses_per_ideator=2`、`selected_hypotheses_per_round=2`，硬上限分别为 8、5、4。
- classical ML 和 deep learning 默认都用 train 内 `k=5`；Human 可按 execution policy 禁用。K-fold 只提供稳定性证据，唯一 SOTA 只按共享 test split 的 primary `test_score` 排序。
- final-test 是恰好一个可恢复的逻辑 attempt，不允许失败后创建新 attempt、重新挑模型或 best-of-run。
- 原型先跑通完整流程，明确记录 `strong_isolation=false`；安全隔离不在首轮实现，但必须保留指定 TODO。
- 真实验收只能读取 `examples/titanic`。禁止读取、导入、复制、恢复或写入 `examples/titanic-run/**`、`.athena/titanic-run*`。
- 不得增加任何 Titanic 专用分支、prompt、fixture、列名规则、target 规则或 fallback。

必须放在实际临时边界旁的注释如下，文字不要改写：

```python
# TODO(supervisor-security): 完整流程跑通后，用强隔离 runner 替换本地 workspace shell；生产/敏感数据发布前必须完成。
# TODO(data-runtime): 出现已批准的非 Python 数据脚本需求后，新增对应 runtime adapter；首版仅支持 python-uv。
# TODO(search-cost-tiebreak): 成本指标合同获批后，可在 test score 平局时比较训练时间、推理时间和内存；首版平局始终保留当前 SOTA。
# TODO(search-bootstrap): test predictions 重采样成本完成基准评估且 EvalSpec 获得 bootstrap seed/count/CI/min_effect 合同后，增加 paired bootstrap；首版使用 5-fold 或单次 test。
# TODO(search-pareto): 需要多目标 EvalSpec、frontier 上限/剪枝、预算分配和 VALIDATE 最终选择合同获批后，再扩展为 Pareto front；首版保持唯一 SOTA。
# TODO(supervisor-ui): Athena-cli 与 ResearchRuntime 控制/事件合同稳定后，基于同一 RUN、STATUS、HumanRequest 和事件 API 增加 TUI/GUI 适配器；不得复制 Supervisor 状态机或编排逻辑，App Server 集成另行设计。
```

---

## Target File Ownership

| File | Single responsibility |
|---|---|
| `src/athena/research/runtime/facade.py` | composition root、公开 dispatch、execution 生命周期、事件订阅；不判断具体阶段业务 |
| `src/athena/cli.py` | 参数转换、状态显示、HumanRequest 命令行交互；不拥有流程状态 |
| `src/athena/research/contracts.py` | Dataset、EvalSpec、EDA、baseline、ranking、ablation、final-test 的 Artifact payload 合同 |
| `src/athena/research/data_service.py` | 原始摄取、不可变 manifest、split/derived 数据不变量、Bundle 冻结 |
| `src/athena/research/script_runner.py` | `python-uv` DRAFT/FROZEN 生命周期和统一 CLI+JSON entrypoint 执行 |
| `src/athena/research/runtime/services.py` | `RUN_SERVICE` 静态白名单与已实现领域服务的窄适配；不编排 Agent、不决定下一步 |
| `src/athena/research/evaluation/` | K-fold/single-test policy、可信 test/final-test evaluator、分数合同 |
| `src/athena/research/search.py` | 图入库、Selector、RankingRound、唯一 SOTA 事务输入 |
| `src/athena/research/validation.py` | SOTA reproduce、ablation closure、FinalTestAttempt 状态推进 |
| `src/athena/research/supervisor/models.py` | Plan/Operation/execution/HumanRequest/lease 的持久化领域模型 |
| `src/athena/research/supervisor/state.py` | 权威 facts、预算、phase/status 只读投影 |
| `src/athena/research/supervisor/journal.py` | SQLite schema、事务、lease、outbox、恢复查询；不含业务决策 |
| `src/athena/research/supervisor/planner.py` | 只根据 snapshot 生成下一份一步式 Plan |
| `src/athena/research/supervisor/validator.py` | 权限、refs、预算、phase gate、final-test gate 校验 |
| `src/athena/research/supervisor/executor.py` | 执行已批准 operation、技术重试、耐久化结果；不决定下一步 |
| `src/athena/research/supervisor/coordinator.py` | lease 下的 recover-or-plan 循环和 durable event 发布 |
| `src/athena/agents/{init_agent,data_agent,reflection_agent}.py` | 独立 LLM worker，只输出结构化候选 Artifact |
| `src/athena/agents/builtin_agents.py` | 删除 JSON sink fallback；只保留真正静态 worker adapter |
| `src/athena/core/agent/prompts/{init_agent,data_agent,code_agent}.md` | 与通用数据/实验合同一致的 worker prompt |
| `src/athena/core/agent/prompts/reflection_agent.md` | 角色审查、EDA rubric、候选 hypothesis rubric 的独立 LLM 审查 prompt |

不要创建通用 repository/service-manager/event-bus 框架。上述四个领域服务由 `ResearchRuntime` 直接装配，通过 `RUN_SERVICE` 静态名称注册给 Executor。

## Core Interfaces

以下名字是跨任务合同，后续任务不得自行改名：

```python
class ExecutionConfig(BaseModel):
    interaction_mode: Literal["interactive", "auto"] = "interactive"
    ideator_count: int = 3
    hypotheses_per_ideator: int = 2
    selected_hypotheses_per_round: int = 2
    kfold_policy: Literal["required", "auto", "disabled"] = "auto"
    k_folds: Literal[5] = 5
    ablation_mode: Literal["FULL_LINEAGE", "BASELINE_ONLY"] | None = None
    max_total_plans: int = 100
    max_search_experiments: int = 20
    max_repair_turns_per_agent: int = 8
    max_identical_failure_repeats: int = 3
    max_prepare_revisions: int = 2
    max_validation_environment_repairs: int = 2
    max_consecutive_no_improvement: int = 5
    max_execution_duration_seconds: int = 21600

class SupervisorServices(Protocol):
    async def run(self, service: str, request: dict[str, object]) -> ServiceResult: ...

class ServiceResult(BaseModel):
    result_refs: list[ArtifactRef]
    facts: dict[str, object] = Field(default_factory=dict)
    evidence_refs: list[ArtifactRef] = Field(default_factory=list)
```

execution 状态字面量固定为 `RUNNING | PAUSED | WAITING_FOR_HUMAN | COMPLETED | FAILED | CANCELLED`。研究 phase 与 execution 状态正交；存在开放且阻塞的 HumanRequest 时只读状态投影必须返回 `WAITING_FOR_HUMAN`，不能把等待仅保存在 Python task 或 Agent mailbox。

`RUN_SERVICE` 只接受以下静态名称：

```text
dataset.ingest
dataset.freeze_roles_and_splits
dataset.accept_derived
scripts.freeze
scripts.run
evaluation.baseline
evaluation.candidate_batch
search.register_hypotheses
search.freeze_ranking_round
validation.reproduce_sota
validation.run_ablation
validation.advance_final_test
```

## Task 1: Freeze Contracts and Establish the Real Baseline

**Files:**

- Create: `src/athena/research/contracts.py`
- Modify: `src/athena/research/supervisor/models.py`
- Modify: `src/athena/research/supervisor/state.py`
- Create: `test/unit/research/test_supervisor_contracts.py`

**Interfaces:**

- Produces: `ExecutionConfig`, `DatasetRoleProposal`, `DatasetRoleReview`, `DatasetManifest`, `DerivedDatasetManifest`, `DataScriptBundle`, `EvalSpec`, `EDAReview`, `BaselinePlan`, `CandidateEvaluation`, `RankingRound`, `AblationSummary`, `FinalTestAttempt`, `ValidationResult`。
- Produces: `ProjectFacts` 使用明确的 `*_ref` 字段，`project_phase(facts)` 只由事实投影。

- [x] **Step 1: 先运行当前基线并保存输出**

```powershell
uv run pytest test/unit/research/test_supervisor_core.py test/unit/test_cli.py tests/test_research_runtime.py -q
```

Expected: 记录实际 pass/fail；不要把本计划中的旧 `20 passed` 当成当前证据。

- [x] **Step 2: 写合同失败测试**

```python
def test_execution_config_defaults_and_limits() -> None:
    cfg = ExecutionConfig()
    assert cfg.k_folds == 5
    assert (cfg.ideator_count, cfg.hypotheses_per_ideator) == (3, 2)
    with pytest.raises(PydanticValidationError):
        ExecutionConfig(ideator_count=9)

def test_phase_requires_accepted_prepare_and_validation_facts() -> None:
    assert project_phase(ProjectFacts(task_ref="artifact://sha256:" + "0" * 64)) == ResearchPhase.PREPARE
    facts = prepared_facts(search_stop_ref=None)
    assert project_phase(facts) == ResearchPhase.SEARCH
    assert project_phase(validated_facts()) == ResearchPhase.COMPLETED
```

- [x] **Step 3: 实现最小、严格的 Pydantic 合同**

合同必须验证 enum、范围、ArtifactRef、唯一 ID、版本和非空字段。`ProjectFacts` 至少包含：

```text
task_ref, execution_config_ref, dataset_role_proposal_ref, dataset_role_review_ref,
dataset_manifest_ref, eval_spec_ref, eda_report_ref, eda_review_ref,
baseline_experiment_ref, graph_ref, ranking_round_ref, sota_experiment_ref,
search_stop_ref, ablation_decision_ref, ablation_summary_ref,
final_test_attempt_ref, validation_result_ref
```

- [x] **Step 4: 运行合同测试和原 Supervisor 单测**

```powershell
uv run pytest test/unit/research/test_supervisor_contracts.py test/unit/research/test_supervisor_core.py -q
```

Expected: PASS；旧 `analysis_ref/eval_ref/baseline_ref/sota_ref/validation_ref` 假事实测试已改为新合同。

- [ ] **Step 5: Commit**

```powershell
git add src/athena/research/contracts.py src/athena/research/supervisor/models.py src/athena/research/supervisor/state.py test/unit/research/test_supervisor_contracts.py test/unit/research/test_supervisor_core.py
git commit -m "refactor: freeze supervisor research contracts"
```

## Task 2: Make Journal, Lease, and Executor Recovery Durable

**Files:**

- Modify: `src/athena/research/supervisor/journal.py`
- Modify: `src/athena/research/supervisor/executor.py`
- Modify: `src/athena/research/supervisor/coordinator.py`
- Modify: `src/athena/core/agent/agent_runtime.py`
- Create: `test/unit/research/test_supervisor_journal.py`

**Interfaces:**

- Produces: `LeaseToken(execution_id, owner_id, generation, expires_at)`。
- Produces: `PlanJournal.claim_lease()`, `heartbeat()`, `reserve_dispatch()`, `complete_operation()`, `append_event()`, `read_status_snapshot()`。
- Consumes: Task 1 的 Plan/Operation/ProjectFacts 合同。

- [x] **Step 1: 写事务和 fencing 失败测试**

```python
def test_stale_lease_cannot_commit(tmp_path: Path) -> None:
    journal = PlanJournal(tmp_path / "supervisor.db")
    first = journal.claim_lease("exec-1", "owner-a", ttl_seconds=1)
    second = journal.take_over_expired_lease("exec-1", "owner-b", now=first.expires_at)
    with pytest.raises(StaleLeaseError):
        journal.commit_operation_success(..., lease=first)
    assert second.generation == first.generation + 1

def test_operation_fact_budget_and_version_commit_once(tmp_path: Path) -> None:
    version = journal.complete_operation(..., idempotency_key="search:round:1:commit")
    assert journal.complete_operation(..., idempotency_key="search:round:1:commit") == version
    assert journal.read_budget("exec-1").search_experiments_used == 1
```

- [x] **Step 2: 把 SQLite 写操作收敛为显式事务**

增加 `executions` lease/heartbeat/terminal 字段、稳定 dispatch 记录、append-only fact events、outbox events 和 final-test attempts 表。禁止 `INSERT OR REPLACE` 覆盖已成功 operation；状态转换必须检查合法前态。

- [x] **Step 3: 让 Agent dispatch 可对账**

Executor 在外部派发前先 `reserve_dispatch(dispatch_key, agent_type, payload_ref)`；若记录已有 `agent_id/run_id`，先用 `AgentRuntime.has_agent/run_summary/resume_agent` 认领，不再 spawn。仅当 Journal 明确为 `RESERVED` 且没有外部身份时才创建 Agent，并立即写回身份。

同时移除 `AgentRuntime` 对 `athena.app_server.exceptions` 的反向依赖；在 core 层转换本地异常，不修改 App Server。

- [x] **Step 4: Coordinator 加 lease、heartbeat 和 durable outbox**

Coordinator 每个 step 校验 generation，执行前 heartbeat，提交后发布 outbox。非 owner 只能 STATUS；失去 lease 立即停止写入。事件发布失败保留待发布记录，不回滚已提交事实。

- [x] **Step 5: 运行持久化测试**

```powershell
uv run pytest test/unit/research/test_supervisor_journal.py test/unit/research/test_supervisor_core.py test/unit/agent/test_agent_runtime.py -q
```

Expected: PASS；重开 Journal 后 operation outputs、dispatch IDs、预算和 state_version 不丢失。

- [ ] **Step 6: Commit**

```powershell
git add src/athena/research/supervisor src/athena/core/agent/agent_runtime.py test/unit/research
git commit -m "feat: make supervisor journal restart safe"
```

## Task 3: Stabilize ResearchRuntime and Athena-cli Lifecycle

**Files:**

- Modify: `src/athena/research/runtime/facade.py`
- Create: `src/athena/research/runtime/services.py`
- Modify: `src/athena/cli.py`
- Modify: `pyproject.toml`
- Create: `test/unit/research/test_services.py`
- Modify: `test/unit/test_cli.py`
- Modify: `tests/test_research_runtime.py`

**Interfaces:**

- Current interface: `ResearchRuntime(project_root=..., session=..., research=..., dependencies=...)`; lifecycle/control uses `start`, `start_task`, `message`, `subscribe`, `settings`, and `aclose`. This grouped interface supersedes the task-era flat constructor and `dispatch` proposal。

- [x] **Step 1: 写 CLI/Runtime 失败测试**

```python
async def test_run_preserves_auto_mode_and_registers_workers(runtime) -> None:
    await runtime.dispatch("TASK_CONFIGURE", {"interaction_mode": "auto", "task": "predict an outcome", "data_path": "data"})
    result = await runtime.dispatch("RUN", {})
    assert result["interaction_mode"] == "auto"
    assert {"init", "data", "reflection", "ideator", "code"} <= runtime.registered_worker_types

def test_cli_parser_accepts_mode_and_search_policy() -> None:
    args = _build_parser().parse_args(["run", "--project", "p", "--data", "d", "--task", "intent", "--mode", "auto", "--kfold", "disabled"])
    assert (args.mode, args.kfold) == ("auto", "disabled")
```

- [x] **Step 2: 减少 Runtime 职责**

Runtime 构造时注册真实 worker 和静态 service registry；`TASK_CONFIGURE` 只验证并保存 task/config、摄取源路径候选，不硬编码 classification/tabular/f1。`RUN` 创建或恢复 execution 并启动 Coordinator；STOP interrupt 活动 Agents；取消后的再次 RUN 创建新 execution。

巡检证据：构造器自动 worker 注册、配置保存、execution 生命周期和 STOP interrupt 已实现；composition root 直接装配 `ResearchServices` 并注入 Executor，Validator 校验完整静态名称白名单。dataset/scripts、evaluation 和 search handlers 已注册；validation 名称仍明确拒绝而不回退。Executor 可逐个解析 `RUN_SERVICE.request` 顶层字段中的引用，将前序 worker result 字段传给确定性服务，并用 Journal `complete_operation` 在同一事务内保存 operation 成功状态、service facts、幂等键和 `state_version`。具体 Planner 消费点仍由对应后续任务完成。

- [x] **Step 3: 修复 CLI 模式与 attach 行为**

`--mode interactive|auto` 必须进入 `ExecutionConfig`。默认使用 `interactive`。附着模式轮询 STATUS，并在出现 HumanRequest 时显示问题、options，通过 `await asyncio.to_thread(input, prompt)` 读取命令行输入后调用 `HUMAN_REPLY`。删除当前伪 `--detach`：进程内 Coordinator 无守护进程时退出会取消任务，首版 CLI 必须附着到 terminal；后台继续运行只能由另一个长期存活 host 提供。

在 `src/athena/cli.py` 的公共 adapter 边界放置 `TODO(supervisor-ui)` 原文。

- [x] **Step 4: 修复 STATUS 和事件**

STATUS 不隐式创建 execution。无 execution 时返回 `execution=None, phase="IDLE"`；默认选择活动 execution，否则最近 terminal。Coordinator 的 durable events 必须通过 Runtime subscriber 转发。

- [x] **Step 5: 运行 Runtime/CLI 测试**

```powershell
uv run pytest test/unit/research/test_services.py test/unit/test_cli.py tests/test_research_runtime.py -q
```

Expected: PASS；无硬编码 task/data type/metric；mode 和 execution selection 可恢复。

- [ ] **Step 6: Commit**

```powershell
git add pyproject.toml src/athena/research/runtime/facade.py src/athena/cli.py test/unit/test_cli.py tests/test_research_runtime.py
git commit -m "feat: stabilize supervisor runtime and cli"
```

## Task 4: Implement Generic LLM Data Bundles with uv

**Files:**

- Create: `src/athena/research/script_runner.py`
- Create: `src/athena/research/data_service.py`
- Modify: `src/athena/agents/data_agent.py`
- Modify: `src/athena/core/agent/prompts/data_agent.md`
- Create: `test/unit/research/test_data_scripts.py`
- Create: `test/unit/research/test_dataset_invariants.py`

**Interfaces:**

- Produces: `DataScriptRunner.freeze(workspace, metadata) -> DataScriptBundle`。
- Produces: `DataScriptRunner.run(bundle, request, output_schema) -> ScriptRunResult`。
- Produces: `DatasetService.ingest(source_root) -> DatasetManifest` and `accept_derived(parent, candidate) -> DerivedDatasetManifest`。

- [x] **Step 1: 写 Bundle 和数据不变量失败测试**

```python
async def test_frozen_bundle_uses_declared_entrypoint_and_uv(tmp_path: Path) -> None:
    bundle = await runner.freeze(tmp_path / "draft", metadata(entrypoint="src/inspect_anything.py"))
    assert bundle.entrypoint == "src/inspect_anything.py"
    assert bundle.runtime == "python-uv"
    assert bundle.lock_ref is not None
    assert runner.command(bundle)[0:3] == ["uv", "run", "--frozen"]

def test_derived_dataset_cannot_change_original_column(tmp_path: Path) -> None:
    with pytest.raises(OriginalColumnMutation):
        service.accept_derived(parent, derived_with_changed_value(parent))
```

- [x] **Step 2: 实现统一 entrypoint 合同**

调用形式固定为：

```text
uv run --frozen <declared-entrypoint> --request <request.json> --output <result.json>
```

DRAFT 允许 Agent 命令行使用 `uv init / uv add / uv run`；冻结时执行 `uv lock`，保存源码、`pyproject.toml`、`uv.lock`、Python 版本和 environment hash。后续只允许 `uv sync --frozen` 与 `uv run --frozen`。Runner 不按脚本文件名寻找入口。

巡检证据：声明式 entrypoint、完整源码树、`pyproject.toml`/`uv.lock`、Python 版本、environment hash 和显式 `uv sync/run --frozen` 均已有实现与局部测试；Planner 已用 `scripts.freeze` 冻结 eval workspace，`ResearchServices` 的 `scripts.freeze/run` adapter 也有实际 `uv` 测试。DataAgent 的 inspect/EDA 仍未迁入该 Bundle 合同。

- [x] **Step 3: 实现不可变摄取与 derived 校验**

`ingest` 只复制源字节、生成 SHA-256 manifest，不解析业务数据。`accept_derived` 校验原始列名称、类型、行身份、逐值 hash 和 split membership 完全相同；新列名与原列冲突立即失败。

巡检证据：原始字节复制、文件 SHA-256、原列名称/类型/逐值 hash、行身份、split membership 和新列名冲突均已有实现与局部测试。受管 raw 已进入 manifest/fact，并被首个 DataAgent、Init、独立 EDA、SEARCH 和 final-test payload 消费；真实 DataAgent 已从该目录完整读取三个 Titanic CSV。baseline payload 仍缺 data path，`dataset.freeze_roles_and_splits` handler 也尚未进入 Planner。

- [x] **Step 4: 保留明确的原型风险注释**

在 local shell runner 和 runtime registry 的实际边界分别加入 `TODO(supervisor-security)` 与 `TODO(data-runtime)` 原文，并给每个运行结果写入 `strong_isolation=false`。

- [x] **Step 5: 运行数据合同测试**

```powershell
uv run pytest test/unit/research/test_data_scripts.py test/unit/research/test_dataset_invariants.py test/unit/research/test_services.py -q
```

Expected: PASS；测试使用两个不同文件布局和动态脚本名，不包含 Titanic 知识。

- [ ] **Step 6: Commit**

```powershell
git add src/athena/research/script_runner.py src/athena/research/data_service.py src/athena/agents/data_agent.py src/athena/core/agent/prompts/data_agent.md test/unit/research
git commit -m "feat: add generic uv data script contracts"
```

## Task 5: Complete PREPARE: Role Review, EvalSpec, EDA, and Baseline

**Files:**

- Modify: `src/athena/agents/init_agent.py`
- Modify: `src/athena/agents/data_agent.py`
- Modify: `src/athena/agents/reflection_agent.py`
- Modify: `src/athena/agents/builtin_agents.py`
- Create: `src/athena/core/agent/prompts/reflection_agent.md`
- Modify: `src/athena/core/agent/prompts/init_agent.md`
- Modify: `src/athena/core/agent/prompts/code_agent.md`
- Modify: `src/athena/research/supervisor/planner.py`
- Create: `test/unit/research/test_prepare_flow.py`

**Interfaces:**

- DataAgent outputs: `DatasetRoleProposal`, then `eda_report.md` plus evidence refs。
- ReflectionAgent outputs: `DatasetRoleReview` or `EDAReview`; it never directly reads raw data。
- InitAgent outputs: `EvalSpec` after approved roles/splits, using only training schema/sample。
- Baseline Ideator outputs: one `BaselinePlan` containing 2-3 directions and exactly one recommendation。

- [x] **Step 1: 写 PREPARE 状态机失败测试**

```python
async def test_prepare_requires_role_review_and_eda_review(harness) -> None:
    await harness.run_until("DATA_ROLE_REVIEW")
    assert harness.facts().dataset_manifest_ref is None
    await harness.complete_role_review("ACCEPT")
    await harness.run_until("EDA_REVIEW")
    assert harness.facts().baseline_experiment_ref is None

async def test_revise_follows_up_same_data_agent(harness) -> None:
    original = await harness.first_data_agent()
    await harness.return_role_review("REVISE")
    await harness.step()
    assert harness.last_followup_agent_id == original.agent_id
```

巡检证据：事实门槛、六步顺序、PREPARE ingest 的 `RUN_SERVICE(dataset.ingest)` + DataAgent spawn、role/EDA `REVISE` 后同一 DataAgent follow-up、两类评审修订耗尽后的 auto 失败与 interactive HumanRequest，以及非默认修订上限均已有测试。

- [ ] **Step 2: 按小 Plan 实现 PREPARE 顺序**

```text
ingest raw -> DataAgent inspect/role proposal -> Reflection role review
-> DatasetService freeze reader/splits -> InitAgent EvalSpec
-> DataAgent full-read EDA -> Reflection EDA rubric
-> one Ideator BaselinePlan -> one CodeAgent Bundle
-> runner -> trusted test evaluator -> baseline fact
```

Reflection `REVISE` 必须 follow-up 原 DataAgent，同一 agent/thread/workspace/reader lineage。最多两次自动修订；auto 耗尽后失败，interactive 创建 `data_role_resolution` HumanRequest。

巡检状态：Planner 已生成六个顺序 Plan，并实现 role/EDA review ACCEPT/REVISE、同 Agent follow-up、修订耗尽后的 auto FAILED/interactive HumanRequest；`scripts.freeze` 和 `evaluation.baseline` 已接入。独立 EDA 已切到受管 raw，但首个 DataAgent 仍一次承担角色发现、脚本修复、完整 EDA 和 proposal，且 proposal 可缺失、未经 `DatasetRoleProposal` 校验，Planner 提升整个 Bundle。两次真实运行分别在完整产出后的 turn 收尾和缺 report/proposal 时失败，均未进入 role review。真实 REVISE payload 仍不满足 DataAgent 的 JSON/data_path 输入；角色冻结和真实 baseline 尚未到达。

- [ ] **Step 3: 实现 full-read-first EDA**

DataAgent 默认完整读取 train。只有 Runner 明确返回内存/timeout/resource-limit 分类后，Planner 才允许同一 DataAgent 生成自适应采样版本；解析、代码或质量失败不能采样回退。采样只影响 EDA，不影响训练或评估。

- [ ] **Step 4: 实现独立 LLM EDA review**

rubric 必须覆盖读取成功、schema/target、缺失/异常、分布、泄漏、split、采样稳定性、图表引用和结论证据。非空 Markdown 和至少一张图只是结构预检。通过后 SQLite 只登记 `eda_report_ref`、`eda_review_ref` 和 accepted fact。

巡检状态：ReflectionAgent 已能按 `kind` 读取 proposal/report，并在显式注入 client 时用 `DatasetRoleReview`/`EDAReview` 结构化输出；测试只使用 fake provider。CLI 只把 model 交给 `ResearchRuntime`，Reflection builder 因 `client=None` 走确定性非空/图表 fallback，真实独立 LLM rubric 尚未进入入口；fallback 的多个广泛异常捕获也应在主通路跑通时收敛。

- [ ] **Step 5: 实现真实 baseline**

删除 `IdeatorAgent`/`CodeAgent` 的 JSON sink fallback。Baseline Ideator 读取 approved EDA、Dataset descriptor、EvalSpec，生成 2-3 个方向并推荐一个；CodeAgent 只能实现该 recommendation。runner 用完整 train 训练，trusted evaluator 在 test 上评分后才提交 baseline/SOTA 初始事实。

- [ ] **Step 6: 运行 PREPARE 测试**

```powershell
uv run pytest test/unit/research/test_prepare_flow.py test/unit/research/test_data_scripts.py -q
```

Expected: PASS；没有 CSV 假设、固定 `eval.py/analysis.py` 或固定脚本名。

- [ ] **Step 7: Commit**

```powershell
git add src/athena/agents src/athena/core/agent/prompts src/athena/research/supervisor/planner.py test/unit/research/test_prepare_flow.py
git commit -m "feat: complete generic prepare workflow"
```

## Task 6: Implement Evaluation Policy and SEARCH Graph Ranking

**Files:**

- Create: `src/athena/research/evaluation/evaluator.py`
- Create: `src/athena/research/search.py`
- Modify: `src/athena/core/research_tree.py`
- Modify: `src/athena/core/research_models.py`
- Modify: `src/athena/research/supervisor/planner.py`
- Modify: `src/athena/research/supervisor/validator.py`
- Create: `test/unit/research/test_search.py`

**Interfaces:**

- Produces: `EvaluationPolicy.choose_mode(config, cost_snapshot) -> Literal["kfold-5", "single-test"]`。
- Produces: `SearchService.register_hypotheses(graph_ref, batches) -> graph_ref`。
- Produces: `SearchService.freeze_round(snapshot, evaluations) -> RankingRoundResult`。

- [x] **Step 1: 写 ranking/SOTA 失败测试**

```python
def test_all_valid_hypotheses_enter_graph_before_selection(search) -> None:
    result = search.register_and_select(six_hypotheses(), selected_count=2)
    assert len(result.graph.pending_hypotheses()) == 6
    assert len(result.ranking.selected_ids) == 2
    assert len(result.ranking.deferred_ids) == 4

def test_test_score_alone_selects_unique_sota(search) -> None:
    result = search.freeze_round(parent_score=.80, candidates=[candidate(.83, kfold_mean=.79), candidate(.82, kfold_mean=.90)])
    assert result.sota.test_score == .83

def test_tie_keeps_current_sota(search) -> None:
    assert search.compare(.8, .8 + 1e-13).winner == "current"
```

巡检证据：`test_search.py` 已覆盖全部有效假设先入图、selected/deferred、`test_score` 唯一 SOTA、数值 tie 保留当前 SOTA、minimize 方向和成本预检；registry 另覆盖两个 search handler。未保留这些测试在实现前失败的 red 阶段记录。

- [ ] **Step 2: 实现一轮 SEARCH 的冻结输入**

每轮并行创建配置数量的独立 Ideator；每个读取 task、EDA、baseline、graph snapshot 和允许证据。所有结构有效假设先写入 graph，再对“全部未执行且合格节点”排名。每个 selected hypothesis 使用独立 CodeAgent、experiment 和 `LocalGitWorkspace`，从同一父 SOTA commit/dataset/EvalSpec 分叉。

巡检状态：Planner 已生成 register、`evaluation.candidate_batch` 和 freeze operation，Executor 可解析 service result Artifact 并提交 graph/ranking/SOTA facts。第 69 轮同步与后台 Coordinator 测试均产生 SOTA 并到 COMPLETED，上轮“freeze 无 SOTA后重复 SEARCH”未再复现；若该状态再次出现，新 `SEARCH_NO_SOTA` 分支会明确 FAILED 而非循环。当前仍只派发一个 Ideator 和一个 CodeAgent，predictions/labels 来自 deterministic fallback；没有资格池、独立 experiment/worktree、同源 EvalSpec 分叉或真实 train/predict，因此本步骤保持未完成。

- [ ] **Step 3: 实现 Selector**

Reflection rubric 解决冷启动；Bradley-Terry/UCB 只读取真实 Comparator 胜负。RankingRound 是 append-only Artifact，保存候选、prior、BT mean、uncertainty、novelty、cost、final score、稳定排序、selected/deferred IDs、graph/policy version。未选中保持 pending/deferred，不得标为 refuted。

巡检状态：当前 helper 仅按输入顺序或 `test_score` 形成 selected/deferred，并把 `RankingRound` JSON 写入 ArtifactStore；尚无 Reflection rubric、去重/资格校验、BT/UCB、novelty/cost、完整候选快照、graph/policy version 或权威 append-only Journal 记录。

- [ ] **Step 4: 实现 K-fold/single-test policy**

默认 `k=5`，记录 fold scores/mean/std；随后完整 train 训练一次并对 test 评分。成本估算超过剩余时长 40% 或单 fold 超内存/timeout 时，整轮启动前统一切换 single-test。Human 只可在 round 边界把 execution policy 设为 required/auto/disabled。

巡检状态：`EvaluationPolicy.choose_mode()` 已实现 `required/auto/disabled`、40% 成本公式和单 fold timeout 的确定性模式选择；`TrustedEvaluator` 可运行冻结 eval bundle 并生成 `CandidateEvaluation`。baseline 与 candidate-batch handler 均已由 Planner 消费，并有真实 `uv` 局部路径测试。仍无 fold scores/mean/std、完整 train + test、内存预检、真实模型 predictions 或 round 边界 policy version 更新；完整 Coordinator 也尚未从 SEARCH 进入 VALIDATE。

- [ ] **Step 5: 实现唯一 SOTA 事务输入**

所有候选完成后按 primary test score 和 direction 排序；`math.isclose(rel_tol=1e-9, abs_tol=1e-12)` 平局保留当前 SOTA。RankingRound、graph ref、SOTA CAS、预算和 no-improvement 计数由 Journal 在单事务提交，完成顺序不得改变结果。

巡检状态：`SearchService.freeze_round()` 已按 `test_score` 和 direction 排序并使用指定 tolerance；Planner 显式标记只有 freeze operation 消费搜索预算，register operation 不消费，第 63 轮对应测试未列入失败项。graph/ranking refs 是 ArtifactStore 引用，但 `sota_experiment_ref` 当前写入 `CandidateEvaluation.candidate_id`，不是可解析的冻结实验 Artifact；仍缺 Journal 级 SOTA compare-and-set、no-improvement、恢复断点、同轮 direction/冻结输入校验，以及四处 SOTA 身份一致性证明。

在比较函数旁放置 `TODO(search-cost-tiebreak)`、`TODO(search-bootstrap)`、`TODO(search-pareto)` 原文。

- [ ] **Step 6: 运行 SEARCH 测试**

```powershell
uv run pytest test/unit/research/test_search.py -q
```

Expected: PASS；leaderboard SOTA、graph SOTA、下一轮父节点和 VALIDATE 冻结对象始终是同一 experiment。

巡检证据：第六十九轮唯一一次相关套件结果为 `190 passed in 26.02s`，包含局部 SEARCH evaluator、graph/ranking/预算、同步 Coordinator、后台 Runtime Coordinator 和 reopen COMPLETED。该证据依赖 fake workers 与固定 predictions/labels；四处 SOTA 身份仍未统一为可解析 experiment Artifact，因此本步骤保持未完成。

- [ ] **Step 7: Commit**

```powershell
git add src/athena/research/evaluation src/athena/research/search.py src/athena/core/research_tree.py src/athena/core/research_models.py src/athena/research/supervisor test/unit/research/test_search.py
git commit -m "feat: add graph based search and unique sota"
```

## Task 7: Add Repair, Target, and Human/Auto Decisions

**Files:**

- Modify: `src/athena/research/supervisor/planner.py`
- Modify: `src/athena/research/supervisor/validator.py`
- Modify: `src/athena/research/supervisor/executor.py`
- Modify: `src/athena/research/supervisor/state.py`
- Modify: `src/athena/cli.py`
- Create: `test/unit/research/test_supervisor_decisions.py`

**Interfaces:**

- Produces: normalized `FailureEvidence`, `AutoDecisionRecord`, versioned `SearchPolicyDecision`。
- Consumes: persistent `HumanRequest` and same-agent `FOLLOWUP_AGENT` operations。

- [ ] **Step 1: 写修复和决策失败测试**

```python
async def test_business_repair_uses_same_agent_and_stops_on_fingerprint(harness) -> None:
    agent_id = await harness.fail_candidate("ImportError: x")
    await harness.repeat_same_failure(3)
    assert harness.followup_agent_ids == [agent_id, agent_id, agent_id]
    assert harness.experiment_status == "FAILED"
    assert harness.hypothesis_status == "INCONCLUSIVE"

async def test_auto_mode_never_creates_human_request(harness) -> None:
    await harness.worker_requests_input(valid_recommendation())
    assert harness.open_requests == []
    assert harness.auto_decision.answer_source == "auto_policy"
```

巡检状态：上一轮无生产消费者的失败指纹、target/auto helpers、3 个记录模型、Journal auto-decision 表/API 和 16 个局部测试已删除，扫描无悬空引用。示例中的 same-agent repair、无 HumanRequest 的 Planner 流程和失败终态均未实现；按当前优先级不提前补建，待主链路跑通且出现可复现故障后再写最小测试。本步骤保持未完成。

- [ ] **Step 2: 区分技术重试与业务 repair**

技术重试只覆盖 timeout、connection reset、HTTP 429/5xx、SQLite busy、临时 I/O；普通最多 4 次，LLM stream reconnect 最多 5 次，指数退避+jitter并尊重 retry-after。合同错误、脚本错误、资源上限、评审失败和分数未改善必须由新 Plan follow-up，最多 8 turns；同 fingerprint 连续三次且无新证据则提前停止。

- [ ] **Step 3: 实现 SEARCH target checkpoint**

每轮收尾后判断 `target_test_score`。auto 达标后记录 `TARGET_REACHED` 并接受当前 SOTA；interactive 创建 `target_decision`，只允许 `ACCEPT_CURRENT_SOTA` 或 `RAISE_TARGET`。提高目标必须严格优于当前 SOTA、保留历史、版本+1、预算足够再完整运行一轮，且不重置预算/no-improvement。

- [ ] **Step 4: 实现通用 HumanRequest/AutoDecision**

worker 只返回候选问题；Planner 决定、Validator 校验、Executor 持久化。interactive 请求无 default/expiry，回答后 follow-up 原 worker。auto 不创建伪 HumanRequest，只接受合法 recommended_answer 并记录 policy/version/evidence/risk；合同无效只允许一次同 Agent 修订，仍无效则 FAILED。

- [ ] **Step 5: 运行决策测试**

```powershell
uv run pytest test/unit/research/test_supervisor_decisions.py test/unit/test_cli.py -q
```

Expected: PASS；关闭并重开项目后仍显示同一 request_id，回答只唤醒一次。

巡检证据：第五十一轮唯一一次相关验证为 `186 passed in 11.49s`；已不包含被删除的 Task 7 局部测试。当前没有 Task 7 生产行为或集成覆盖，本步骤保持未完成。

- [ ] **Step 6: Commit**

```powershell
git add src/athena/research/supervisor src/athena/cli.py test/unit/research/test_supervisor_decisions.py test/unit/test_cli.py
git commit -m "feat: add durable supervisor decisions and repair"
```

## Task 8: Implement VALIDATE, Ablation, and Recoverable Final Test

**Files:**

- Create: `src/athena/research/validation.py`
- Modify: `src/athena/research/supervisor/planner.py`
- Modify: `src/athena/research/supervisor/validator.py`
- Modify: `src/athena/research/supervisor/journal.py`
- Create: `test/unit/research/test_validation.py`

**Interfaces:**

- Produces: `ValidationService.reproduce_sota(snapshot) -> ReproductionResult`。
- Produces: `dependency_closure(graph, intervention_id) -> tuple[str, ...]`。
- Produces: `PlanJournal.reserve_final_test(...) -> FinalTestAttempt` and `advance_final_test(attempt_id, expected_status, refs)`。

- [ ] **Step 1: 写 ablation/final-test 失败测试**

```python
def test_dependency_ablation_removes_transitive_dependents(graph) -> None:
    assert dependency_closure(graph, "feature-a") == ("feature-a", "model-b", "calibration-c")

async def test_existing_final_score_is_committed_without_rescoring(harness) -> None:
    attempt = await harness.crash_after_score_written()
    await harness.reopen_and_run()
    assert harness.final_test_attempt_id == attempt.attempt_id
    assert harness.final_test_score_calls == 1
    assert harness.final_test_status == "COMMITTED"
```

巡检状态：冻结快照中的 `test_validation.py` 有 19 个测试：3 个 dependency closure/scope、5 个 generalization gap/result、10 个 final-test Journal 行为，以及 1 个 `final_test_recovery_action()` 局部测试。后者只验证 `SCORED + score_ref -> commit`、其他状态 `-> evaluate`；尚无生产编排消费者或真实 evaluator crash-after-score/reopen/no-rescore，因此本步骤保持未完成。

- [ ] **Step 2: 先做 frozen SOTA reproducibility preflight**

冻结 experiment、Git commit、dataset version、EvalSpec、accepted lineage。源码、uv.lock、完整 train 训练和 test prediction 必须复现；失败使用 validation environment repair 预算，耗尽后 FAILED，不得绕过。

- [ ] **Step 3: 实现 ablation scope gate**

interactive 首次进入 VALIDATE 无条件创建 `ablation_scope`，选 FULL_LINEAGE 或 BASELINE_ONLY；auto 固定 FULL_LINEAGE。FULL_LINEAGE 对每个 accepted intervention 做依赖闭包 leave-one-out，完整 train + test，默认无 K-fold。每项保存 requested ID、closure、score delta、duration、status、refs。

巡检状态：`dependency_closure()` 以稳定 BFS 返回“自身 + 全部传递依赖者”，`ValidationService.ablation_scope()` 可为所有 accepted interventions 生成闭包映射。尚未注册 validation service，也没有 Planner mode gate、HumanRequest/auto 决策、实际 leave-one-out、完整 train/test 或 AblationRecord。

- [ ] **Step 4: 实现 partial ablation gate**

解释性变体失败可以记录 INCONCLUSIVE，其他项继续。汇总为 `AblationSummary(COMPLETE|PARTIAL)`。interactive 对 PARTIAL 创建 `ablation_incomplete`，只允许 PROCEED_WITH_PARTIAL 或 STOP_BEFORE_FINAL_TEST；接受记录必须绑定 summary hash。auto 在 SOTA 可复现时确定性 proceed。

- [ ] **Step 5: 实现唯一 FinalTestAttempt 状态机**

```text
RESERVED -> RUNNING -> PREDICTIONS_WRITTEN -> SCORED -> COMMITTED
                    -> FAILED_UNRECOVERABLE
```

唯一键为 `(execution_id, sota_experiment_id, final_test_ref, eval_spec_version)`。prediction/score 已存在时只能向前恢复，不能重训/重预测/重评分。只有 trusted evaluator 读取 final labels。成功后按 direction 计算 gap；正 gap 超过 EvalSpec tolerance 时 warning=true，但仍 COMPLETED。

巡检状态：`PlanJournal` 已用 UNIQUE index 强制逻辑键唯一；reserve/advance 在 `BEGIN IMMEDIATE` 内完成 lease 校验、状态 CAS 和不可变 score 检查。Planner 已生成 reserve、RUNNING、PREDICTIONS_WRITTEN、SCORED、COMMITTED operation；SCORED 前调用 `evaluation.candidate_batch`，保存独立 score Artifact，Executor 在 COMMITTED 时写入 `ValidationResult`。第 69 轮直接状态机、同步 Coordinator、后台 Runtime Coordinator 和 reopen 均到 COMPLETED。prediction/labels 仍来自 CodeAgent fallback，也没有 reproduction、ablation summary 或真实模型 final-test，因此本步骤保持未完成。

- [ ] **Step 6: 运行 VALIDATE 测试**

```powershell
uv run pytest test/unit/research/test_validation.py -q
```

Expected: PASS；每 execution 最多一个逻辑 final attempt，真实低分不触发换模型。

巡检证据：第六十九轮对冻结快照只运行一次完整相关套件，结果为 `190 passed in 26.02s`。直接与 Coordinator 路径均证明 final-test 分数来自冻结 evaluator而不是 CodeAgent 的硬编码 `test_score`，并有独立 score Artifact；真实 LLM 入口没有重跑，且模型 predictions 仍是 fallback，因此本步骤保持未完成。

- [ ] **Step 7: Commit**

```powershell
git add src/athena/research/validation.py src/athena/research/supervisor test/unit/research/test_validation.py
git commit -m "feat: add ablation and recoverable final test"
```

## Task 9: Cover the Four Required Crash-Recovery Checkpoints

**Files:**

- Create: `tests/test_supervisor_recovery.py`
- Create: `test/unit/research/_support.py`

**Interfaces:**

- Consumes: real SQLite、real ArtifactStore、deterministic fake Agent/runner/LLM。
- Produces: failure injection harness that crashes after a named durable boundary。

- [ ] **Step 1: 实现四个精确故障注入测试**

```python
@pytest.mark.parametrize("checkpoint", [
    "agent_dispatched_before_journal_completion",
    "test_score_written_before_sota_commit",
    "final_score_written_before_db_commit",
    "human_request_open_before_restart",
])
async def test_restart_preserves_exactly_once_semantics(tmp_path: Path, checkpoint: str) -> None:
    before = await run_until_crash(tmp_path, checkpoint)
    after = await reopen_and_finish(tmp_path)
    assert after.authoritative_facts == before.expected_final_facts
    assert after.budget_has_duplicate_consumption is False
    assert after.artifact_refs_resolve is True
```

巡检状态：已新增 4 个独立 reopen 测试，但没有统一 failure-injection harness。真实旧项目命中了测试未覆盖的边界：PREPARE Plan 已让 ingest 提交 facts、DataAgent 仍在途，重开后同一 Plan 的 `snapshot_version=1` 对 current=4 被 Validator 判 stale；Plan 变为 failed，而 execution 仍 RUNNING。final-test/dispatch/SEARCH/HumanRequest 测试均未覆盖这种 Plan 自身前序 operation 推进 state_version 后的恢复。

- [ ] **Step 2: 为每个场景增加禁止行为断言**

分别断言：不重复 spawn；不重训/重评分且最多一个 SOTA；不创建第二 final attempt；不重复 HumanRequest/follow-up。四项都断言 lease fencing 生效、state_version 单调、预算只消费一次。

巡检状态：SEARCH 测试断言预算、SOTA fact 和 spawn 不重复；final-test 测试断言同一 attempt/score 被复用，并验证恢复动作 helper 的返回值。仍无可信 evaluator train/predict/score 调用计数，helper 分支不能替代 no-rescore 编排证据；也缺少 HumanRequest 回复后单次 follow-up，以及四场景统一的 lease fencing、state_version、真实 Artifact 可解析或权威 facts 对等断言。

- [ ] **Step 3: 运行恢复测试**

```powershell
uv run pytest tests/test_supervisor_recovery.py -q
```

Expected: 4 scenarios PASS with real SQLite/ArtifactStore。

巡检证据：第七十二轮唯一一次相关验证为 `197 passed in 24.71s`；现有恢复局部测试、Coordinator 完成和 reopen 均通过，但真实 stale snapshot 恢复失败直接证明覆盖不足。还缺 Plan 内前序 fact 提交后的 resume、failed Plan execution 终态、evaluator 调用计数与四个精确故障边界的统一不变量，因此本步骤保持未完成。

- [ ] **Step 4: Commit**

```powershell
git add tests/test_supervisor_recovery.py test/unit/research/_support.py
git commit -m "test: cover supervisor crash recovery"
```

## Task 10: Remove Legacy References Without Touching App Server or workflow.py

**Files:**

- Delete: obsolete `ProjectRuntime` exports/tests outside the forbidden historical file
- Delete: old SupervisorAgent/ReportAgent prompts and registrations
- Modify: `src/athena/research/__init__.py`
- Modify: user/developer docs that recommend old public APIs
- Do not modify: `src/athena/app_server/**`
- Do not read or modify: `src/athena/workflow.py`

**Interfaces:**

- Produces: only `ResearchRuntime` and `Athena-cli` as supported workflow APIs。

- [x] **Step 1: 静态扫描允许修改的路径**

```powershell
rg -n "ProjectRuntime|SupervisorAgent|ReportAgent|REPORT|SEARCH_START|VALIDATE_START" src/athena test tests docs -g "!src/athena/workflow.py" -g "!src/athena/app_server/**" -g "!docs/supervisor_design.md" -g "!docs/supervisor_imp_docs.md"
```

Expected: 只列出应删除/重写的位置；命令绝不打开禁读文件。

巡检证据：第二十轮按排除规则执行扫描，未读取禁读 workflow。代码侧仅剩旧命令的负向测试和说明性文字；当时的历史计划仍有大量 `ProjectRuntime`、`SupervisorAgent`、旧阶段命令与 REPORT 流程说明，属于待重写/归档的文档债务。

- [x] **Step 2: 删除旧引用并重命名旧测试**

`test/unit/test_project_runtime.py` 的仍有效行为迁移到 `tests/test_research_runtime.py`，随后删除旧测试文件。禁止保留 import alias 或 compatibility wrapper。

巡检证据：旧测试文件已删除；9 个有效的 coordinator、reopen、worker、控制状态和 phase projection 测试已迁移到 `tests/test_research_runtime.py`。允许代码扫描未发现 `ProjectRuntime` import、alias 或 compatibility wrapper。

- [x] **Step 3: 确认 App Server 零修改**

```powershell
git diff -- src/athena/app_server
```

Expected: empty。

巡检证据：第六十九轮冻结快照执行 `git diff -- src/athena/app_server`，输出为空。

- [ ] **Step 4: 运行静态扫描和全量确定性测试**

```powershell
uv run pytest test/unit tests -q --ignore=tests/test_titanic_acceptance.py
```

Expected: PASS；没有旧流程引用，且本命令不执行真实 LLM 验收。

巡检证据：第二十轮在新增 validation helper 后使用当前 `.venv` 执行等价命令，结果为 `739 passed, 25 subtests passed in 33.51s`；第二十一轮只新增聚焦预算测试，未重跑此广域命令。静态扫描仍发现过时架构/计划文档，因此本步骤保持未完成。

- [ ] **Step 5: Commit**

```powershell
git add src/athena/agents/supervisor.py src/athena/core/agent/prompts/report_agent.md src/athena/research/project_runtime.py src/athena/research/__init__.py test/unit/test_project_runtime.py tests/test_research_runtime.py
git commit -m "refactor: retire legacy research workflow"
```

提交前检查 staged diff，确认没有 staging `src/athena/workflow.py` 或 `src/athena/app_server/**`。

## Task 11: Real End-to-End Acceptance with examples/titanic

**Files:**

- Create: `tests/test_titanic_acceptance.py`
- Create: `scripts/run_titanic_acceptance.ps1`
- Do not modify: `examples/titanic/**`

**Interfaces:**

- Consumes: real configured LLM、real `uv`、real model training/evaluation。
- Produces: one temporary project, one completed execution, diagnostic bundle on failure。

- [ ] **Step 1: 写只做通用约束的验收 harness**

Harness 启动前后递归计算 `examples/titanic` 普通文件的 path/length/SHA-256；创建唯一 `%TEMP%/athena-titanic-<uuid>` project root；禁止路径集合在文件访问 guard 中 fail closed。它只传入目录和自然语言 task intent，不传入已知列、target、文件角色或 Titanic 规则。

- [ ] **Step 2: 执行真实流程**

```powershell
$athenaRunRoot = Join-Path $env:TEMP ("athena-titanic-" + [guid]::NewGuid())
uv run Athena-cli run --project $athenaRunRoot --data examples/titanic --task "discover a valid supervised prediction task and build the best model supported by the data" --mode auto
```

Expected final facts:

```text
phase=COMPLETED
accepted DatasetRoleProposal/DatasetRoleReview
accepted eda_report.md/EDAReview
valid baseline test_score
at least one frozen RankingRound containing every valid hypothesis
one graph/leaderboard SOTA selected by test_score
FULL_LINEAGE AblationSummary
exactly one COMMITTED FinalTestAttempt
test_score, final_test_score, generalization_gap, generalization_warning
```

巡检状态：尚无验收 harness 或真实 COMPLETED 运行。本轮已从全新临时项目用完整 `examples/titanic` 重跑：受管目录摄取成功，DataAgent 生成分析脚本、合法 proposal、报告和 5 张图，但 turn 最终 RuntimeError，`wait:data` failed；execution 仍显示 PREPARE/RUNNING。直接复现又在 89 秒后因脚本末次修订损坏、缺 report/proposal 而失败。目录输入根因已跨过，但角色/EDA 长 turn、错误详情投影、failed Plan 终态和恢复均未闭合；不能把生成文件或 `--data train.csv` 当作通过证据。

- [ ] **Step 3: 按证据修复通用实现并从全新项目重跑**

每次失败只修改通用合同、prompt、Supervisor 或 runner；禁止修改示例数据、降低门槛、反复撞同一 execution 或加入 dataset-specific fallback。每次修复后使用新的临时 project root 从头运行，直到完整通过。

- [ ] **Step 4: 核对不可变与禁区证明**

断言示例目录前后 SHA-256 清单完全一致、受管 raw snapshot hash 一致、原始列 hash 一致、禁区零访问、所有运行产物都位于临时 project、日志中没有 Titanic 专用 fallback。

- [ ] **Step 5: 最终全量验证**

```powershell
uv run pytest test/unit tests -q
uv pip check
git diff --check
```

Expected: deterministic suite PASS、real acceptance PASS、dependency check clean、no whitespace errors。

- [ ] **Step 6: Commit**

```powershell
git add tests/test_titanic_acceptance.py scripts/run_titanic_acceptance.ps1
git commit -m "test: add real supervisor acceptance"
```

## Completion Gate

实现只有同时满足以下条件才能声明完成：

- `ResearchRuntime` 是唯一 composition root，Supervisor 完整管理 PREPARE/SEARCH/VALIDATE；
- `Athena-cli` 可以交互运行和 auto mode 运行，不硬编码任务类型、数据类型、metric 或 target；
- 所有数据读取由 LLM 生成、动态命名、`uv` 冻结 entrypoint Bundle 完成；
- raw bytes、原始列和 split 边界具有自动不变量验证；
- `eda_report.md` 经过独立 LLM Reflection rubric 批准后才能生成 baseline；
- 所有有效 hypothesis 先进入图，唯一 SOTA 严格按 test score 提交；
- K-fold 默认 5、可配置禁用、不会参与 SOTA 接受；
- repair 使用同一 Agent/thread/worktree/lineage，技术重试与业务预算分离；
- ablation gate 和唯一 FinalTestAttempt 可在崩溃后准确恢复；
- 四个关键恢复测试通过；
- 真实 `examples/titanic` 从全新临时项目完整通过且无数据专用适配；
- App Server 零修改，历史 workflow 零读取/导入/执行；
- 指定的六条 TODO 位于真实延期边界，除此之外没有用 TODO 替代首版合同。
