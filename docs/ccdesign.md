# ccdesign：统一脚本生成机制 / EDA 知识注入 / eval 冻结

> **Status**: proposed
> **Owner**: Athena maintainers
> **Date**: 2026-07-31
> **Scope**: 主链修复（D1/D2/D3）+ EDA 重构（CodeAgent 写脚本 + 知识注入 + rubric 验收）+ eval 冻结（A 为主）
> **Supersedes**: 本文档提出的设计将取代 `workflows/search/code_agent.py` 中现有 eval.py 硬编码与 `run_experiment.py` stub 逻辑。

---

## 0. 文档约定

- 本文所有模块路径以**当前合并后**的规范路径为准（`a9c180d merge: integrate main into core reorganization` 之后）。
- 涉及"人类开发者"可实现的承诺：每个新类型给出完整字段定义；每个检查给出**确定性判定逻辑**；每个里程碑给出**可验证的验收标准**。
- 本文是设计文档，不是实施清单。实施顺序见 §8。

---

## 1. 背景与问题（基于当前代码逐条核对）

### 1.1 D1｜代码生成后端未接进主链

`athena.code.engine.CodeEngine`（generate→run→observe→iterate 循环）与 `athena.code.backends.QoderBackend / CodexBackend` 在代码库中**零引用**。主链实际执行的 `CodeAgent.execute`（`workflows/search/code_agent.py:96`）不做代码生成：

- `_write_evaluator`（`code_agent.py:142`）写入**硬编码** eval.py。
- `_ensure_experiment_entrypoint`（`code_agent.py:167`）只做存在性检查；文件不存在时写入一个 `raise RuntimeError("code generation backend did not produce run_experiment.py")` 的 stub（`code_agent.py:188-192`）。

**后果**：除非外部 composition root 预先在 worktree 放好 `run_experiment.py`，否则 PREPARE baseline、SEARCH 每个候选、VALIDATE 每次消融都会失败。`ResearchRuntime._run_search`（`research/runtime.py`）只注入 `prepare_baseline` / `search_factory`，无法补上这一步。**端到端闭环目前不成立。**

### 1.2 D2｜eval.py 硬编码，冻结评估器红线（R1）被绕过

`_write_evaluator`（`code_agent.py:142-165`）把主指标硬编码为 `f1_macro`、副指标硬编码为 `accuracy`，**完全无视传入的 `eval_spec`**。而 `EvaluatorFactory.build`（`workflows/prepare/evaluator_factory.py`）会按 `task_type` 生成 `rmse` / `roc_auc` 等 spec。两者脱节：

- 回归任务会被一个用 F1 打分的 eval.py 评估。
- R1（"评价指标参数不能改"）形同虚设——Agent 改不了它，但它本身就不符合 spec。
- `Evaluator.evaluate`（`evaluation/evaluator.py`）只重读 eval_result.json，不做 spec 一致性校验。

### 1.3 D3｜PREPARE 的 EDA / 清洗 / 切分没有接入运行时

`DataPipeline`（`experiment/pipeline.py:62`，SAMPLING→EDA→CLEANING→SPLITTING→DONE 状态机 + PlotAgent 回调）**零调用**。`create_baseline`（`workflows/prepare/baseline.py:75`）需要 `data_profile` + `processing_log`（含 splits），而 `ResearchWorkflowDependencies.prepare_baseline` 的签名是 `Callable[[TaskMetaData, ResearchTree], Awaitable[str]]`——**数据准备产物没有来源**。数据清洗、切分、EDA 全部依赖外部 composition root。

### 1.4 E4｜DataTools 分析能力薄弱

`DataTools`（`workflows/prepare/data_analysis.py`）只有 5 个方法：`sample / describe / head / missing_matrix / correlation_matrix`。缺失：泄漏检测、target 分布画像、`feature_process.csv` 落盘、大表分块读取、多 seed 采样、EDA 产出验收。

### 1.5 E5｜附带缺陷

| # | 缺陷 | 位置 | 说明 |
|---|---|---|---|
| E5-1 | `CodegenResult.diff` 是占位符 | `code_agent.py:136` | `f"artifact://diffs/{experiment_id}"`，不是真实 git diff，`Supervisor` 的 diff 审查从未被喂真实 diff |
| E5-2 | `CodeRouter.route` 伪路由 | `code_agent.py:77-86` | 按 intervention 文本关键词挑 codex/qoder，与后端能力无关 |
| E5-3 | 超时硬编码 | `code_agent.py:117-120` | 300s/60s 写死，不读 `plan.budget` |
| E5-4 | 无日志流式/进度事件 | `code_agent.py:38` | `_run_python` 等脚本结束才写日志，与 app_server 被动事件观测脱节 |
| E5-5 | `_PROTECTED_FILES` 未接进主链 | `code/review.py` | `review_diff` 定义了 protected 检查但主链未调用 |

---

## 2. 设计目标与非目标

### 2.1 目标

1. **让闭环成立**（D1）：代码生成后端（Qoder/Codex）真正接进 `CodeAgent.execute`。
2. **让评估可信**（D2）：eval 由 CodeAgent 生成一次、在 PREPARE 冻结、运行时始终校验；R1 有字节级保证。
3. **让 PREPARE 真实**（D3）：数据采样/EDA/清洗/切分接入运行时，`ProcessingLog` 成为事实源。
4. **EDA 由 CodeAgent 写脚本**（E4）：注入 EDA 知识，用 rubric 验收产出，knowledge 与 rubric 分离且版本化。

### 2.2 非目标（明确不做，各自独立立项）

- R4 多指标退化守卫（secondary guard）。
- Reflection 闸门 / 假设质量审查。
- 语义邻近度（embedding 距离）替换词面 Jaccard。
- 失败记忆 / 修复协议（AgentFix）。
- 论文类型分类器 / lever 抽取。
- 实验阶段协议 / 复制节点。
- Elo 锦标赛排序设计。
- Monitor 重复修复环检测。
- 每个实验动态重生成 eval（方案 B）——本设计只做**预留接口**，见 §4.4.5。

---

## 3. 总体设计：统一脚本生成机制

### 3.1 概念模型

系统内所有"由 CodeAgent 写脚本再运行"的任务（EDA、清洗、实验、评估）共享同一机制：

```
KnowledgePack（输入启发式，版本化）
  +
ScriptRubric（输出验收，版本化）
  ──注入──▶ backend 生成脚本源码（Qoder/Codex）
            ──▶ Supervisor 静态审查（rubric 的 static_checks）
            ──▶ 运行（固定脚本名，超时/监控）
            ──▶ 运行时校验（rubric 的 runtime_checks）
            ──▶ 不通过 → 反馈重试（上限 max_rounds）
```

四个不可变约定：

1. **脚本名固定**：程序只按固定名字运行脚本，不猜测文件名。
2. **knowledge 是输入**，**rubric 是输出验收**，二者分离、版本化、可替换。
3. **静态审查在运行前**，**运行时校验在运行后**，两个门都过才算成功。
4. **失败显式抛错**，不返回占位成功（延续项目既有哲学）。

### 3.2 脚本任务清单

| kind | 固定脚本名 | 产出 | 消费者 |
|---|---|---|---|
| `EDA` | `eda.py` | `EDA.md`（+ 可选图） | 人类/PlotAgent/后续假设 |
| `CLEAN` | `clean.py` | `cleaned.csv` + `feature_process.csv` | SPLITTING → baseline |
| `EXPERIMENT` | `run_experiment.py` | `predictions.csv` | eval.py |
| `EVALUATION` | `eval.py`（生成一次，冻结） | `eval_result.json` | Evaluator |

### 3.3 worktree 布局契约

每个实验 worktree 必须呈现以下布局（PREPARE 负责把数据文件放入 baseline worktree 并提交，SEARCH/VALIDATE 从父 commit 继承）：

```
<worktree>/
  train.csv            # 训练 split
  validation.csv       # 验证 split（SEARCH 阶段 eval 用）
  test.csv             # 测试 split（final-test 用；实验脚本禁读）
  run_experiment.py    # 实验脚本（CodeAgent 生成）
  eval.py              # 冻结评估器（从冻结 artifact 复制，禁改）
  predictions.csv      # 实验脚本产出
  eval_result.json     # eval.py 产出
  athena_context.json  # 运行上下文（experiment_id / eval_spec / split 路径）
  athena_logs_*.txt    # 运行日志
```

---

## 4. 详细设计

### 4.1 新模块 `athena/code/scriptgen.py`

#### 4.1.1 数据类型

```python
"""Unified script generation mechanism: generate → static review → run → runtime validate."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal

from athena.core.contracts import ArtifactRef
from athena.evaluation.types import EvalSpec


class ScriptKind(str, Enum):
    EDA = "eda"
    CLEAN = "clean"
    EXPERIMENT = "experiment"
    EVALUATION = "evaluation"


class ScriptEntrypoint(str, Enum):
    EDA = "eda.py"
    CLEAN = "clean.py"
    EXPERIMENT = "run_experiment.py"
    EVALUATION = "eval.py"


@dataclass(frozen=True)
class StaticCheck:
    """运行前的源码级检查。判定逻辑见 checks.py。"""
    code: str                    # 唯一标识，如 "metric_declared"
    description: str
    params: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeCheck:
    """运行后的产物级检查。判定逻辑见 checks.py。"""
    code: str
    description: str
    params: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ScriptRubric:
    """一个脚本任务的验收规则集。版本化。"""
    version: str
    static_checks: list[StaticCheck] = field(default_factory=list)
    runtime_checks: list[RuntimeCheck] = field(default_factory=list)
    max_rounds: int = 3          # 生成-审查重试上限
    timeout_s: float = 300.0     # 运行超时；可由 plan.budget 覆盖
    allowed_files: frozenset[str] = field(default_factory=frozenset)   # 允许写入；空 = 不限
    protected_files: frozenset[str] = frozenset({"eval.py", "splits.json", "athena_context.json"})


@dataclass(frozen=True)
class KnowledgePack:
    """注入给生成 backends 的领域知识。版本化，与 rubric 分离。"""
    version: str
    kind: ScriptKind
    system_prompt: str           # 知识正文（或正文模板）
    refs: tuple[str, ...] = ()   # 可引用文档 artifact


@dataclass(frozen=True)
class ScriptTask:
    """一次具体的脚本生成-运行任务。"""
    kind: ScriptKind
    entrypoint: ScriptEntrypoint
    knowledge: KnowledgePack
    rubric: ScriptRubric
    target_dir: str              # worktree 路径
    inputs: dict[str, object]    # 注入给生成与检查的输入（eval_spec / profile / splits...）
    budget_override: dict[str, float] = field(default_factory=dict)  # 覆盖 timeout_s


@dataclass
class ScriptResult:
    task: ScriptTask
    files_created: list[str]
    files_modified: list[str]
    output: str                  # 合并 stdout+stderr
    returncode: int
    rounds_used: int
    checks: dict[str, bool]      # check.code -> passed
    ok: bool


class ScriptGenerationError(RuntimeError):
    """生成、审查或运行失败后的显式错误。带阶段标记。"""
    def __init__(self, stage: str, message: str, *, logs: ArtifactRef | None = None):
        super().__init__(f"[{stage}] {message}")
        self.stage = stage       # "generate" | "static_review" | "run" | "runtime_validate"
        self.logs = logs
```

#### 4.1.2 `ScriptGenerator` 状态机

```python
class ScriptGenerator:
    """统一脚本生成循环。"""

    def __init__(
        self,
        backend: CodeBackend,
        monitor: AgentMonitor,
        supervisor: Supervisor,          # 提供 review_script（见 §4.5）
        checks: object | None = None,    # checks.py 的判定实现
    ) -> None:
        ...

    async def generate_approved(self, task: ScriptTask) -> str:
        """generate → static_review，通过即返回脚本源码。**不运行**。

        eval.py 在 PREPARE 冻结时使用：此时尚无 predictions.csv，不能运行。
        静态审查不过 → 反馈重试；耗尽 → ScriptGenerationError(stage="static_review")。
        """
        for round_num in range(1, task.rubric.max_rounds + 1):
            gen = await self._backend.generate(
                prompt=self._compose_prompt(task),
                target_dir=task.target_dir,
                previous_outputs=...,   # 反馈上一轮错误
                history=...,
            )
            # 从固定脚本名读取源码（不依赖 GenerationResult 增加字段）
            source = (Path(task.target_dir) / task.entrypoint.value).read_text(encoding="utf-8")
            verdict = self._supervisor.review_script(task, source, task.rubric)
            if verdict.action == "approve":
                return source
        raise ScriptGenerationError(
            "static_review",
            f"{task.kind} static review failed after {task.rubric.max_rounds} rounds",
        )

    async def generate_and_run(self, task: ScriptTask) -> ScriptResult:
        """完整循环：generate → static_review → run → runtime_validate。
        用于 EDA / CLEAN / EXPERIMENT（生成后即可运行）。
        任何一步失败都抛 ScriptGenerationError，不返回部分成功。
        """
        # 复用 generate_approved 获取通过静态审查的脚本源码
        source = await self.generate_approved(task)
        # 3. 运行（固定脚本名 + 超时 + 监控）
        exec_out = await self._monitor.watch(
            run_script(task.entrypoint.value, cwd=task.target_dir),
            timeout_s=task.rubric.timeout_s,
        )
        # 4. 运行时校验（运行后）
        checks = run_runtime_checks(task, task.target_dir, task.rubric, self._checks)
        if exec_out.returncode == 0 and all(checks.values()):
            return ScriptResult(..., ok=True)
        # 失败 → 反馈，继续下一轮
        for round_num in range(2, task.rubric.max_rounds + 1):
            gen = await self._backend.generate(
                prompt=self._compose_prompt(task),
                target_dir=task.target_dir,
                previous_outputs=[exec_out, ...],   # 带上运行失败输出
                history=...,
            )
            source = (Path(task.target_dir) / task.entrypoint.value).read_text(encoding="utf-8")
            verdict = self._supervisor.review_script(task, source, task.rubric)
            if verdict.action != "approve":
                continue
            exec_out = await self._monitor.watch(
                run_script(task.entrypoint.value, cwd=task.target_dir),
                timeout_s=task.rubric.timeout_s,
            )
            checks = run_runtime_checks(task, task.target_dir, task.rubric, self._checks)
            if exec_out.returncode == 0 and all(checks.values()):
                return ScriptResult(..., ok=True)
        raise ScriptGenerationError(
            "run",
            f"{task.kind} failed after {task.rubric.max_rounds} rounds",
        )
```

要点：

- **不通过就反馈重试**：静态审查拒绝与运行时校验失败都作为 `previous_outputs` / `history` 反馈给生成器，直到 `max_rounds`。
- **超时**：`rubric.timeout_s`，可被 `task.budget_override`（来自 `plan.budget`）覆盖。
- **监控**：复用 `athena.code.monitor.AgentMonitor`。
- **失败显式抛错**：不返回占位 `ScriptResult`。

#### 4.1.3 检查判定（`athena/code/checks.py`）

所有检查都是**确定性程序**，不用 LLM 判分。

**静态检查（源码 AST 层）：**

| code | 适用 kind | 判定逻辑 |
|---|---|---|
| `entrypoint_present` | 全部 | 目标脚本存在且非空（AST 可 parse） |
| `metric_declared` | EVALUATION | AST 提取模块级 `PRIMARY_METRIC`（str）与 `SECONDARY_METRICS`（list[str]）；`PRIMARY_METRIC == eval_spec.primary.name` 且 `SECONDARY_METRICS ⊆ {s.name for s in eval_spec.secondary}`。未声明即拒绝 |
| `reads_only_allowed` | EVALUATION | AST 收集 `pd.read_csv` / `open` 的文件名；允许 `predictions.csv`、由 `ATHENA_EVAL_SPLIT` env 决定的分割文件；**禁止** `train.csv`、绝对路径、`..` |
| `no_test_read` | EXPERIMENT | AST 收集所有文件读取；禁止 `test.csv` |
| `protected_untouched` | EXPERIMENT/CLEAN/EDA | AST 收集所有写入；禁止写入 `eval.py` / `splits.json` / `athena_context.json` |
| `writes_contract` | 全部 | AST 收集所有写入；目标脚本必须包含其声明的产出（EXPERIMENT→`predictions.csv`，CLEAN→`cleaned.csv`+`feature_process.csv`，EDA→`EDA.md`） |
| `no_raw_mutate` | EDA/CLEAN | 禁止写入 raw/sample 源文件（由 `inputs["raw_copy"]` 与 `inputs["sample"]` 指定） |

**运行时校验（产物层）：**

| code | 适用 kind | 判定逻辑 |
|---|---|---|
| `predictions_produced` | EXPERIMENT | `predictions.csv` 存在、非空、所有值有限 |
| `eval_json_valid` | EVALUATION | `eval_result.json` 存在；`experiment_id == 输入的 experiment_id`；`primary` 有限；每个 `secondary` 值有限且键名 ⊆ spec |
| `sample_aligned` | EVALUATION | `predictions.csv` 行数 == 对应分割文件行数 |
| `frozen_untampered` | EVALUATION | worktree 内 `eval.py` 的 sha256 == 冻结 digest（见 §4.4.3） |
| `eda_md_produced` | EDA | `EDA.md` 存在；含 §4.3.2 要求的全部章节 |
| `plot_resolved` | EDA | 若 `plot_agent` 已配置：每个 `[PLOT: ...]` 指令都有同名图片产物；若未配置：`EDA.md` 中不允许存在未回填的 `[PLOT:]` |
| `cleaned_produced` | CLEAN | `cleaned.csv` 存在且非空 |
| `feature_process_produced` | CLEAN | `feature_process.csv` 存在，且每个被处理列有 ≥1 行记录 |

### 4.2 `CodeAgent` 重构（`workflows/search/code_agent.py`）

#### 4.2.1 新 `CodeAgent.execute`

```python
class CodeAgent:
    """统一脚本执行器：生成实验脚本 → 运行 → 冻结评估 → 运行时校验。"""

    def __init__(
        self,
        generator: ScriptGenerator,           # 必填；内含 backend + monitor + supervisor
        freezer: EvaluatorFreezer,            # 必填；eval 冻结与物化
        workspace: GitWorkspace,              # 用于产出真实 diff
    ) -> None:
        ...

    async def execute(
        self,
        experiment_id: str,
        hypothesis: Hypothesis,
        plan: ExperimentPlan,
        parent_commit: str,
        eval_spec: EvalSpec,
        worktree: GitWorkBranch,
        *,
        frozen: FrozenEvaluator,              # 来自 PREPARE 冻结
        split_file: str = "validation.csv",   # SEARCH 用 validation；final-test 用 test
    ) -> CodegenResult:
        # 0. 准备 worktree：确保数据文件存在（baseline 由 PREPARE 放入；其余继承）
        # 1. 生成并运行实验脚本
        task = _experiment_task(
            experiment_id, hypothesis, plan, eval_spec, worktree.path,
            frozen=frozen, split_file=split_file,
        )
        result = await self._generator.generate_and_run(task)
        # 2. 物化冻结评估器并运行（同一 eval.py，字节一致）
        await self._freezer.materialize(frozen, worktree.path, experiment_id, split_file)
        eval_result = await self._run_frozen_evaluator(worktree.path, experiment_id, eval_spec)
        # 3. 真实 git diff
        diff_ref = await self._workspace.diff(worktree, parent_commit)
        return CodegenResult(
            experiment_id=experiment_id,
            commit=parent_commit,
            diff=diff_ref,
            eval=eval_result,
            logs=...,
            wall_time_s=...,
        )
```

- 删除 `_write_evaluator`、`_ensure_experiment_entrypoint`、`CodeRouter`。
- `CodegenResult.diff` 现在是真实 diff 的 artifact ref（E5-1 修复）。
- `plan.budget` 中的 `timeout`/`max_rounds` 覆盖 rubric 默认值。

`_experiment_task(...)` 是构建 `ScriptTask` 的辅助函数（模块私有，`athena/code/tasks.py`）：

```python
def _experiment_task(
    experiment_id: str, hypothesis: Hypothesis, plan: ExperimentPlan,
    eval_spec: EvalSpec, target_dir: str,
    *,
    frozen: FrozenEvaluator, split_file: str,
) -> ScriptTask:
    """构建 kind=EXPERIMENT 的任务。

    - knowledge: experiment_v1.md（任务类型 + baseline/EDA 结论 + 数据切分说明）
    - rubric: static=[entrypoint_present, no_test_read, protected_untouched, writes_contract],
              runtime=[predictions_produced]
    - inputs: {"experiment_id", "hypothesis", "plan", "eval_spec",
               "frozen_digest": frozen.digest, "split_file": split_file}
    - budget_override: 从 plan.budget 读 timeout / max_rounds
    """
```

同理 `_evaluation_task(eval_spec, target_dir)` 构建 kind=EVALUATION 的任务：`knowledge=evaluation_v1.md`、`rubric.static=[metric_declared, reads_only_allowed, writes_contract]`、`inputs={"eval_spec"}`（不运行，只生成+审查）。

#### 4.2.2 后端选择

`backend` 由 **composition root 显式注入**（任务类型/可用性决定），不再由 intervention 文本猜（E5-2 修复）。提供 `athena.code.backends.from_config`（已存在）作为工厂。

### 4.3 EDA / CLEAN 任务（kind=EDA / CLEAN）

#### 4.3.1 数据流

```
DataPipeline.run(data_path)
  ├─ SAMPLING（确定性程序）：sample seeds [17, 42, 97]，选首个采样
  ├─ EDA（CodeAgent）：生成 eda.py → 静态审查 → 运行 → 校验 EDA.md + [PLOT:]
  │     └─ PlotAgent（可选）：读 EDA.md [PLOT:] → 生成图 → 回填
  ├─ CLEANING（CodeAgent）：生成 clean.py → 静态审查 → 运行 → 校验 cleaned.csv + feature_process.csv
  ├─ SPLITTING（确定性程序）：split_dataset(cleaned, seed, ratios) → train/validation/test
  └─ DONE：返回 (DataProfile, ProcessingLog)
```

设计要点：

- **采样与切分是确定性程序**（`data/operations.py` 已有 `sample` / `split_dataset`），只有 EDA 与清洗是 CodeAgent 写脚本。
- `DataPipeline.run` 返回值从 `DataProfile` 改为 `(DataProfile, ProcessingLog)`。`ProcessingLog` 的 `splits` 在 SPLITTING 阶段由 `split_dataset` 确定性填充（`prepare_dataset` 的既有逻辑，`experiment/pipeline.py:15`）。
- `DataTools` 降级为 EDA 脚本的**只读辅助**（schema 概览），不作为 EDA 主体。此决策见 ADR-2。

#### 4.3.2 EDA knowledge pack v1

存放于 `athena/code/knowledge/eda_v1.md`，`KnowledgePack.system_prompt` 注入。结构：

1. **按任务类型的检查清单**（`inputs["task_type"]` 决定侧重）：
   - `classification`：类分布、类别不平衡比、每个特征×target 的分组统计/箱线图、是否存在易混淆类别。
   - `binary_classification`：同上 + 正类占比、阈值敏感性提示。
   - `regression`：target 直方图/QQ、离群点、残差模式、log/Box-Cox 变换候选。
   - `time_series`：趋势、季节、平稳性（ADF）、滞后相关。
   - `clustering`：缩放敏感性、维度、相关结构。
   - 通用表格：缺失率、基数（高基数→是否 id 列/类别）、常量列、重复行、类型误判（object 数值列）、target 泄漏（与 target 强相关的特征、时间泄漏）。
2. **画图美学**（对应设计文档"画图美观的专家知识"）：图类型选择表、分辨率 ≥150dpi、尺寸 (10,6)、颜色盲友好、坐标轴/标题/图例规范、每图一个 `plot_*.png`。
3. **常见陷阱**：共线性、target 泄漏、时间泄漏、train/test 分布漂移、稀有类别。

#### 4.3.3 EDA rubric v1

`EDA.md` 必须包含的章节（`eda_md_produced` 运行时检查的判定依据）：

1. 数据总览（行/列数、dtype、内存）。
2. 数据质量（缺失矩阵、重复行、类型问题、id 列识别）。
3. 单变量分布（数值：直方图+统计量；类别：频数）。
4. 双变量 / 特征-target 关系。
5. 相关性 / 共线性。
6. 泄漏风险评估。
7. **可测试的处理建议**（供后续 hypothesis 直接引用）。

`feature_process.csv` 结构（`clean.py` 产出，`feature_process_produced` 校验）：

```csv
column,operation,params
age,fillna,"{""value"": 0}"
income,clip,"{""lower"": 0}"
cat,encode_label,"{}"
```

与 `data/operations.py` 的 `OPERATION_HANDLERS` 白名单保持一致。

### 4.4 eval 冻结（kind=EVALUATION）

#### 4.4.1 冻结策略

**A 为主**：PREPARE 阶段生成 eval.py 一次 → Supervisor 静态审查 → 冻结为 artifact + 记录 sha256。SEARCH / VALIDATE 所有实验复用**同一字节**的 eval.py，仅注入 `experiment_id` 与分割文件。

#### 4.4.2 生成契约（EVALUATION rubric）

`eval.py` 生成提示要求满足（静态检查 `metric_declared` / `reads_only_allowed` 的判定依据）：

```python
import os
import json
import pandas as pd

experiment_id = os.environ["ATHENA_EXPERIMENT_ID"]
split_file = os.environ.get("ATHENA_EVAL_SPLIT", "validation.csv")
PRIMARY_METRIC = "f1_macro"          # 必须 == eval_spec.primary.name
SECONDARY_METRICS = ["accuracy"]     # 必须 ⊆ {s.name for s in eval_spec.secondary}

predictions = pd.read_csv("predictions.csv")
labels = pd.read_csv(split_file)
# ... 计算 PRIMARY_METRIC 与 SECONDARY_METRICS ...
with open("eval_result.json", "w", encoding="utf-8") as f:
    json.dump({"experiment_id": experiment_id, "primary": ..., "secondary": {...}}, f)
```

`experiment_id` 与 `split_file` 通过 **env** 注入，使同一冻结文件适用于所有实验与 final-test。

#### 4.4.3 `EvaluatorFreezer`（`athena/code/evaluator_freezer.py`）

```python
@dataclass(frozen=True)
class FrozenEvaluator:
    ref: ArtifactRef          # artifact://frozen/eval.py
    digest: str               # sha256
    eval_spec: EvalSpec
    rubric_version: str       # 生成时使用的 EVALUATION rubric 版本
    generated_at: str


class EvaluatorFreezer:
    async def freeze(self, task: ScriptTask) -> FrozenEvaluator:
        """生成一次 → 静态审查 → 冻结。**不运行**（PREPARE 阶段尚无 predictions.csv）。

        审查不过或生成失败 → ScriptGenerationError(stage="static_review")。
        """
        source = await self._generator.generate_approved(task)  # 只生成+审查，不运行
        (target_dir / "eval.py").write_text(source)
        digest = sha256(target_dir / "eval.py")
        put artifact...
        return FrozenEvaluator(...)

    async def materialize(
        self, frozen: FrozenEvaluator, target_dir: str, experiment_id: str, split_file: str
    ) -> None:
        """把冻结 eval.py 复制进 worktree（字节一致），由 runner 设置 env 后运行。"""
        copy(frozen.ref, target_dir / "eval.py")
        assert sha256(target_dir / "eval.py") == frozen.digest
```

- `freeze` 只调用一次（PREPARE）。冻结产物进入 ArtifactStore，digest 记入 tree 的 task 配置。
- `materialize` 每次实验调用，复制后立即校验 digest（双保险，配合 `frozen_untampered` 运行时检查）。

#### 4.4.4 运行时校验（`Evaluator` 扩展）

`evaluation/evaluator.py` 的 `Evaluator` 增加 `validate`：

```python
class Evaluator:
    def validate(self, result: EvalResult, spec: EvalSpec) -> None:
        """失败即抛 ValueError，不返回部分成功。"""
        # 1. result.primary 有限
        # 2. 每个 secondary 值有限且键名 ∈ spec.secondary 名称集合
        # 3. （experiment_id 一致性已在 _load_evaluation 校验；此处再核）
```

`CodeAgent._run_frozen_evaluator` 在 `_load_evaluation` 后调用 `validate`。

#### 4.4.5 方案 B 预留接口

在 `EvaluatorFreezer` 上保留 `refreeze(spec, evidence) -> FrozenEvaluator` 的**签名空位**（标注 `TODO(advanced-ranking)` 同类风格），本设计不实现。语义：有充分证据表明需重冻结时才允许，重冻结必须改变 `rubric_version` 与 `digest`，旧冻结产物留档。

### 4.5 `Supervisor` 扩展（`experiment/supervisor.py`）

```python
@dataclass
class ScriptReviewVerdict:
    action: Literal["approve", "revise"]
    reasons: list[str]


class Supervisor:
    def decide(self, verdict, budget) -> Decision: ...   # 现有，不变

    def review_script(self, task: ScriptTask, source: str, rubric: ScriptRubric) -> ScriptReviewVerdict:
        """对生成脚本源码做静态审查。只返回 approve/revise。"""
        # 逐条运行 rubric.static_checks（checks.py 的确定性判定）
        # 全部通过 → approve；任一失败 → revise + reasons（作为生成器反馈）
```

`athena.code.review.review_diff`（E5-5）同时被接进 `Supervisor`，接收 `CodegenResult.diff` 的真实 diff，拒绝才进入实验决策（`decide`）。`_PROTECTED_FILES` 成为 `ScriptRubric.protected_files` 的默认来源。

### 4.6 PREPARE 运行时接线（D3）

#### 4.6.1 `PreparePipeline`（`workflows/prepare/pipeline_runner.py`）

```python
@dataclass
class PrepareContext:
    task: TaskMetaData
    data_path: str
    base_commit: str
    eval_spec: EvalSpec
    frozen: FrozenEvaluator
    profile: DataProfile
    processing_log: ProcessingLog


async def run_prepare(
    *,
    task: TaskMetaData,
    data_path: str,
    workspace: GitWorkspace,
    tree: ResearchTree,
    data_pipeline: DataPipeline,
    freezer: EvaluatorFreezer,
    code_agent: CodeAgent,
    baseline_agent=None,          # 用于生成 BaselineDraft（可选）
) -> str:
    """完整 PREPARE：采样/EDA/清洗/切分 → 冻结 eval → baseline。返回 baseline 实验 ID。"""
    # 1. 数据准备（CodeAgent 写 eda.py/clean.py + 确定性切分）
    profile, log = await data_pipeline.run(data_path)
    # 2. 冻结评估协议
    eval_spec = EvaluatorFactory.build(task, profile)
    frozen = await freezer.freeze(_evaluation_task(eval_spec, ...))
    # 3. baseline（复用 create_baseline，注入 frozen）
    baseline_id = await create_baseline(
        workspace, base_commit, tree,
        data_profile=profile, processing_log=log, eval_spec=eval_spec,
        code_agent=code_agent, agent=baseline_agent, frozen=frozen,
    )
    return baseline_id
```

`create_baseline` 签名增加 `frozen: FrozenEvaluator`，并把数据文件（train/validation/test）复制进 baseline worktree 后提交——SEARCH/VALIDATE 继承。

#### 4.6.2 运行时签名变更

`research/runtime.py`：

```python
PrepareBaselineFn = Callable[[TaskMetaData, str, ResearchTree], Awaitable[str]]  # + data_path

@dataclass(frozen=True)
class ResearchWorkflowDependencies:
    prepare_baseline: PrepareBaselineFn | None = None
    search_factory: SearchFactory | None = None
    validator: object | None = None
    reporter: object | None = None
```

- `ResearchRuntime.__init__(..., data_path: str | Path | None = None)`。
- `_run_search` 调用 `prepare(task, self._data_path, self._tree)`。
- `_search_start` 在无 baseline 时要求 `data_path` 非空，否则显式失败（延续"缺注入必须失败"）。

---

## 5. 数据结构变更汇总

### 5.1 新增

| 类型 | 模块 | 说明 |
|---|---|---|
| `ScriptKind` / `ScriptEntrypoint` | `athena/code/scriptgen.py` | 脚本任务种类与固定脚本名 |
| `StaticCheck` / `RuntimeCheck` / `ScriptRubric` | `athena/code/scriptgen.py` | 验收规则（版本化） |
| `KnowledgePack` | `athena/code/scriptgen.py` | 输入知识（版本化） |
| `ScriptTask` / `ScriptResult` / `ScriptGenerationError` | `athena/code/scriptgen.py` | 任务与结果 |
| `FrozenEvaluator` / `EvaluatorFreezer` | `athena/code/evaluator_freezer.py` | 冻结评估器 |
| `ScriptReviewVerdict` | `experiment/supervisor.py` | 静态审查结果 |
| `PrepareContext` / `run_prepare` | `workflows/prepare/pipeline_runner.py` | PREPARE 编排 |

### 5.2 修改

| 类型/函数 | 位置 | 变更 |
|---|---|---|
| `CodeAgent.__init__` / `execute` | `workflows/search/code_agent.py` | 注入 generator/freezer/workspace；删除 `_write_evaluator`/`_ensure_experiment_entrypoint`/`CodeRouter`；真实 diff |
| `DataPipeline.run` | `experiment/pipeline.py` | 返回 `(DataProfile, ProcessingLog)`；EDA/CLEANING 委托 CodeAgent 脚本；SPLITTING 用确定性 `split_dataset` |
| `create_baseline` | `workflows/prepare/baseline.py` | 增加 `frozen`；把数据文件放入 worktree 并提交 |
| `Supervisor` | `experiment/supervisor.py` | 增加 `review_script`；接上 `review_diff` |
| `Evaluator` | `evaluation/evaluator.py` | 增加 `validate(result, spec)` |
| `ResearchWorkflowDependencies` / `ResearchRuntime` | `research/runtime.py` | `prepare_baseline` 加 `data_path`；`__init__` 加 `data_path` |
| `EvalSpec` | `evaluation/types.py` | 不新增字段（`frozen=True` 已保证不可变） |

---

## 6. 错误处理

| 场景 | 行为 | 说明 |
|---|---|---|
| 后端不可用（`BackendUnavailableError`） | PREPARE 显式失败 | 不产生占位 baseline |
| 静态审查拒绝 | 反馈生成器重试，至 `max_rounds` | 拒绝原因进入 `history` 反馈 |
| 重试耗尽 | `ScriptGenerationError(stage="generate"/"static_review")` | 实验标记 FAILED，携带日志 |
| 运行超时 | `ScriptGenerationError(stage="run")` | 超时从 `plan.budget`/rubric 读，日志保留 |
| 运行时校验失败 | `ScriptGenerationError(stage="runtime_validate")` | 列出未通过的 check.code |
| 冻结 digest 不匹配 | `ScriptGenerationError` → 实验 FAILED | `frozen_untampered` 检查触发 |
| eval_result.json 与 spec 不一致 | `Evaluator.validate` 抛 `ValueError` | 校验后失败，不读部分结果 |
| PREPARE 缺 `data_path` | `_search_start` 显式失败 | 缺注入必须失败 |

---

## 7. 边界情况

1. **空数据集 / 全 NaN 列**：CLEAN 阶段 `cleaned.csv` 为空 → `cleaned_produced` 失败 → PREPARE 失败并给出明确错误，不进入 SEARCH。
2. **secondary 为空**：eval 只算 primary；`SECONDARY_METRICS == []` 合法。
3. **多指标**：`SECONDARY_METRICS` 可多个，运行时校验每个值有限且键名 ⊆ spec。
4. **实验脚本读 test.csv / 写 eval.py**：`no_test_read` / `protected_untouched` 静态拒绝。
5. **冻结文件被篡改**：`materialize` 后 digest 断言 + `frozen_untampered` 运行时检查双重拦截。
6. **PlotAgent 未配置**：`plot_resolved` 要求 EDA.md 无未回填 `[PLOT:]`；配置时则每个指令必须有对应图。
7. **`timeout` 未在 plan.budget**：用 rubric 默认值；超时后的进程 kill 语义沿用现有 `_run_python`。
8. **final-test**：`split_file="test.csv"`，同一冻结 eval.py；`Validator` 设置 env 后运行，禁止二次调优语义由既有 final-test rubric 保持。
9. **后端仅 Qoder 可用**：`CodexBackend.from_config` 抛 `BackendUnavailableError` 时，composition root 回退 Qoder；两者都不可用则显式失败。

---

## 8. 实施顺序与验收标准

> 每个里程碑独立可合并。验收标准是**自动化测试**，不是人工检查。

### M0 — 骨架与静态审查基础

新增：`scriptgen.py`、`checks.py`（AST 检查工具）、`Supervisor.review_script`。
验收：
- 用**假 backend**（返回固定脚本）驱动 `ScriptGenerator.generate_and_run`（EXPERIMENT kind）成功。
- `metric_declared` 拒绝未知指标；`protected_untouched` 拒绝写 eval.py；`no_test_read` 拒绝读 test.csv。
- 静态审查拒绝 → 反馈重试，`rounds_used` 正确累计。

### M1 — eval 冻结 + CodeAgent 重构（D1 + D2）

新增：`evaluator_freezer.py`、EVALUATION 任务、`Evaluator.validate`；重构 `CodeAgent.execute`；真实 diff；删除旧 stub 逻辑。
验收：
- 同一 `FrozenEvaluator` 物化到两个 worktree，eval.py 字节一致（sha256 相等）。
- 篡改 worktree 内 eval.py → 实验 FAILED。
- 回归任务生成的 eval.py 主指标 == `rmse`（不再硬编码 f1）。
- `CodegenResult.diff` 是真实 diff ref，`review_diff` 能解析并拒绝越界文件。

### M2 — EDA / CLEAN（E4）

新增：`eda_v1.md` / `clean_v1.md` knowledge、EDA/CLEAN 任务与 rubric；重构 `DataPipeline`（返回 `(DataProfile, ProcessingLog)`，SPLITTING 确定性）。
验收：
- 小数据集（iris）PREPARE 产出 `EDA.md`（含 7 章节）、`feature_process.csv`、`cleaned.csv`、splits。
- 缺 `feature_process.csv` → PREPARE 失败；`[PLOT:]` 未回填 → 失败。
- `DataTools` 只读辅助可被 EDA 脚本调用。

### M3 — PREPARE 运行时接线（D3）

新增：`pipeline_runner.py`；修改 `research/runtime.py` 签名。
验收：
- `ResearchRuntime` 从裸 `data_path` 出发跑通 PREPARE→SEARCH（无需外部 composition root 预跑数据准备）。
- `_search_start` 无 `data_path` 时显式失败。

### M4 — 端到端与边界加固

验收：
- iris 端到端：PREPARE → SEARCH（≥2 候选）→ VALIDATE → REPORT 全部成功；eval 通过校验；冻结 digest 不变。
- 后端不可用 → 显式失败（无占位成功）。
- 全 NaN 列数据集 → CLEAN 失败，错误信息明确。

### M5 — 清理与文档

- 删除 `_write_evaluator` / `_ensure_experiment_entrypoint` / `CodeRouter` 残留引用。
- `docs/architecture/current.md` 更新主链描述（EDA 由 CodeAgent 脚本驱动、eval 冻结）。
- 回归：`uv run pytest -q tests test/unit` 全绿。

---

## 9. 测试计划

### 9.1 单元测试（`tests/` 或 `test/unit/`）

| 被测 | 用例 |
|---|---|
| `checks.static` | 每项静态检查的通过/拒绝各一例 |
| `checks.runtime` | 每项运行时检查的通过/拒绝各一例 |
| `EvaluatorFreezer.freeze/materialize` | digest 稳定；篡改检测 |
| `Evaluator.validate` | experiment_id 不匹配、primary 非有限、secondary 键名越界 |
| `Supervisor.review_script` | approve / revise + reasons |
| `DataPipeline.run` | 返回 `(DataProfile, ProcessingLog)`；splits 无泄漏（`validate_disjoint_indices`） |

### 9.2 集成测试

- **端到端 iris**：PREPARE→SEARCH→VALIDATE→REPORT 全成功；eval_result.json 通过校验。
- **端到端回归任务**：生成的 eval 主指标为 rmse，跑通。
- **后端不可用**：PREPARE 显式失败。

### 9.3 属性测试

- 冻结不变性：同一 `FrozenEvaluator` 物化 N 次，eval.py sha256 恒等。
- 确定性切分：同 seed 的 `split_dataset` 输出恒等，且 splits 互斥完备。

---

## 10. 决策记录（ADR）

### ADR-1：eval 冻结策略 A 为主，B 预留
- **决策**：eval.py 在 PREPARE 生成一次并冻结，SEARCH/VALIDATE 复用同一字节文件；仅 env 注入 `experiment_id` / 分割文件。B（每实验动态重生成）只保留 `refreeze` 签名空位。
- **理由**：R1 需要字节级保证；结果可比性依赖同一评估路径。Co-Scientist 消融表明评估一致性的收益远大于灵活性。
- **代价**：新指标需 PREPARE 一次想清楚；接受之。

### ADR-2：EDA 由 CodeAgent 写脚本 + 知识注入，而非固定工具集
- **决策**：EDA/CLEAN 由 CodeAgent 写固定名脚本，`DataTools` 降级为只读辅助。`eda_v1.md` 提供知识，`EDA rubric` 提供验收。
- **理由**：数据形态千差万别，固定工具集无法覆盖；知识/验收分离使 EDA 能力可迭代而不改机制。
- **代价**：依赖后端可用；PREPARE 失败显式化。

### ADR-3：knowledge 与 rubric 分离且版本化
- **决策**：`KnowledgePack`（输入）与 `ScriptRubric`（验收）独立版本化。
- **理由**：knowledge 更新不改变验收标准，反之亦然；可追溯"哪个版本的知识+哪个版本的 rubric 产出了该实验"。

### ADR-4：失败显式抛错，不返回占位成功
- **决策**：后端不可用、审查不过、校验失败均显式抛错/标记 FAILED。
- **理由**：延续项目既有哲学（"缺少工作流后端时明确抛错，不返回占位成功"）；占位成功会污染树与报告。

---

## 11. 与现有文档的关系

- 本设计是对 `docs/architecture/current.md` 主链描述的具体化与修正：PREPARE 加入真实 DataPipeline 驱动，SEARCH 加入代码生成后端，评估走冻结协议。
- `docs/architecture/target.md` 的"已达成"清单需在 M5 更新（PREPARE baseline 真实执行、SEARCH 代码生成）。
- 涉及"兼容路径只重导出"原则：`athena.experiment.baseline` / `search_loop` 等重导出不变，`create_baseline` / `SearchLoop` 签名变更在重导出层同步即可。
