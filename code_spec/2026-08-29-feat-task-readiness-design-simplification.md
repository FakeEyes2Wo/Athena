# feat/jw-fd-tess-task-readiness 程序设计简化方案

日期：2026-08-29
目标：不是简化“新增机制”，而是简化**函数/类/接口设计**，让维护者面对的入口更少、更稳定、更不容易传错参数。

---

## 1. 设计原则

- **一个职责对应一个值对象**：参数成组出现时，把它们绑定成不变对象。
- **执行上下文必须显式、一次注入**：不通过可变全局状态/后端 mutable env 在不同阶段之间切换。
- **公共接口尽量少**：复杂流程只暴露一个入口，内部细节隐藏。
- **流程状态机公开**：长函数如果有多个阶段，用清晰的阶段对象表达，而不是 if/action 堆积。
- **可复现参数集中**：划分、超时、预测目标等应该由配置对象统一携带，而不是散落在各个函数签名里。

---

## 2. 当前主要设计问题

### 2.1 `run_validation_plan` 参数过多

当前签名（约 13 个 keyword-only 参数）：

```python
run_validation_plan(
    *,
    input,
    agents,
    git,
    workspace,
    execution,
    evaluator,
    store,
    independent_review,
    result_ref,
    checkpoint,
    publish,
    experiment_timeout_s,
    predict_features,
)
```

问题：

- 同时包含“身份/持久化输入”“基础设施依赖”“执行选项”三类不同生命周期。
- 调用者必须知道每个基础设施的含义。
- 新增一个执行选项就要在签名、调用点、测试三处改。

### 2.2 `_execute_predictions` 参数过多且依赖可变后端环境

当前有 6 个参数，并通过：

```python
execution.predicting(predict_features)
```

临时修改后端/环境管理器全局状态。

问题：

- `EnvironmentManager` 新增 `_predict_features` attr。
- `ExecutionBackend` / `LocalBackend` / `MirroredBackend` / `SshBackend` 都要实现 `set_predict_features()`。
- `SshBackend` 还要显式拒绝。
- `ExecutionRuntime.predicting()` 是一个“临时改全局状态再恢复”的上下文管理器。
- 这些接口全部只为一个功能服务，却扩大了整个执行后端协议面。

### 2.3 `splitter` 两个入口参数重复且 `materialize_csv_split` 过长

```python
split_ids(row_ids, *, search_frac, final_frac, seed, groups)
materialize_csv_split(source_csv, output_dir, target_column, *, search_frac, final_frac, seed, group_column)
```

问题：

- 同一组划分参数在多个函数中重复。
- `materialize_csv_split` 同时做：读 CSV、校验列、分组、切分、写 5 个文件、写 manifest。
- `_write_split_manifest` 又单独接收 6 个参数。

### 2.4 `run_prepare_phase` 是超长 monolith

新增内容后，它同时承担：

- 平台数据划分；
- evaluator workspace 隔离；
- 生成 `data_contract`；
- 生成 `candidate_task`；
- 生成 search/final evaluator；
- EDA；
- baseline；
- run_prepare_plan。

大量本地字符串拼装和阶段逻辑挤在一个函数里，维护者很难看清每段边界。

### 2.5 `PlanState` / `Supervisor` 语义耦合

`PlanState` 新增 `last_failure` 是合理数据，但：

- 失败反馈的“写”、“读”、“清空”分散在 `supervisor.py`、`plans.py`、`experiment.py` 三处。
- 失败摘要字符串的构造和解析也散落在多个 block 函数中。
- 需要一个小对象把“失败反馈”的生命周期封装起来。

---

## 3. 简化后的目标接口

### 3.1 拆分参数为值对象

#### `SplitSpec`

```python
@dataclass(frozen=True, slots=True)
class SplitSpec:
    search_frac: float = 0.2
    final_frac: float = 0.2
    seed: int = 0
    group_column: str | None = None
```

然后：

```python
split_ids(row_ids, spec: SplitSpec) -> SplitManifest
materialize_csv_split(source_csv, output_dir, target_column, spec: SplitSpec) -> SplitManifest
_write_split_manifest(output_dir, source_csv, target_column, spec: SplitSpec) -> Path
```

`SplitSpec` 直接来自 CLI/ResearchConfig，不再逐层展开成多个参数。

#### `ValidationDeps`

```python
@dataclass(frozen=True, slots=True)
class ValidationDeps:
    agents: AgentRuntime
    git: GitWorkspace
    workspace: GitWorkBranch
    execution: ExecutionRuntime
    evaluator: TrustedEvaluator
    store: ArtifactStore
    independent_review: Callable[[str], Awaitable[ValidationDiffReview]]
    checkpoint: CheckpointValidation
    publish: EmitEvent | None = None
```

#### `ValidationOptions`

```python
@dataclass(frozen=True, slots=True)
class ValidationOptions:
    timeout_s: int = DEFAULT_EXPERIMENT_TIMEOUT_S
    predict_features: Path | None = None
```

最终公共入口：

```python
async def run_validation_plan(
    *,
    input: ValidationInput,
    deps: ValidationDeps,
    options: ValidationOptions,
    result_ref: ArtifactRef | None,
) -> ValidationResult:
```

公共参数从 **13 个降到 4 个**。

#### `PredictionRun`

```python
@dataclass(frozen=True, slots=True)
class PredictionRun:
    execution: ExecutionRuntime
    git: GitWorkspace
    workspace: GitWorkBranch
    store: ArtifactStore
    publish: EmitEvent | None
    timeout_s: int
    predict_features: Path | None
```

`_execute_predictions(run: PredictionRun) -> tuple[ArtifactRef, str]`

### 3.2 移除后端可变预测目标

**目标**：不再给 `ExecutionBackend` 增加 `set_predict_features`，不再给 `EnvironmentManager` 增加 `_predict_features`，不再需要 `predicting()` 上下文。

推荐方案：

1. `ExecutionContext` 增加 `predict_features: Path | None = None`。
2. `ExecutionRuntime.run(context, ...)` 把 `context.predict_features` 传给后端。
3. `LocalBackend.run` 接收该值，并在构建 `CommandExecutor` env 时使用。
4. 删除：
   - `ExecutionBackend.set_predict_features`
   - `LocalBackend.set_predict_features`
   - `MirroredBackend.set_predict_features`
   - `SshBackend.set_predict_features`
   - `EnvironmentManager._predict_features`
   - `EnvironmentManager.set_predict_features`
   - `ExecutionRuntime.predicting`
   - `prepare_phase` 中 `rt.execution.set_predict_features(...)`

这样执行目标从“全局可变状态”变成“一次调用的显式上下文”，接口更少、并发更安全。

### 3.3 拆分 `prepare_phase` 为阶段函数

建议结构：

```python
async def run_prepare_phase(runtime, run_handoff_agent) -> PrepareResult:
    split = await _prepare_platform_split(runtime)
    evaluator_ref = await _prepare_search_evaluator(runtime, split)
    final_evaluator_ref = await _prepare_final_evaluator(runtime, split)
    await _run_eda(runtime, run_handoff_agent, split)
    await _run_baseline_design(runtime, run_handoff_agent, split)
    return await _run_prepare_plan(runtime, split, evaluator_ref)
```

其中：

- `split` 是一个 `PlatformSplit` 值对象：
  - `split_dir`
  - `train_csv`
  - `search_features_csv`
  - `final_features_csv`
  - `candidate_task`
  - `data_contract`
  - `grouping_description`

这样 `prepare_phase` 中重复的路径字符串和提示词拼接不再散落各处，而是由 `PlatformSplit` 统一提供。

### 3.4 封装数据契约生成

新建：

```python
@dataclass(frozen=True, slots=True)
class DataContract:
    train_csv: Path
    predict_features: Path
    group_column: str | None
    raw_dataset: Path | None

    def candidate_task(self, original_task: str) -> str: ...
    def prompt_block(self) -> str: ...
    def state_value(self) -> str: ...
```

`prepare_phase`、`supervisor`、`experiment` 不再各自拼字符串，只消费 `DataContract`。

### 3.5 封装失败反馈生命周期

新建：

```python
@dataclass(frozen=True, slots=True)
class PlanFailure:
    kind: str
    detail: str

    def summary(self) -> str: ...
    def prompt_block(self) -> str: ...
```

`PlanState.last_failure` 存 `summary` 字符串不变，但生成/解析统一收敛到 `PlanFailure`。

---

## 4. 具体改动清单

| 文件 | 当前问题 | 简化动作 |
|---|---|---|
| `splitter.py` | 参数重复、函数过长 | 引入 `SplitSpec`；收缩 `materialize_csv_split`；拆分 CSV 写出与 manifest |
| `supervisor/validation.py` | 13 个参数、长函数 | 引入 `ValidationDeps` / `ValidationOptions` / `PredictionRun`；拆分 `run_validation_plan` 为 run/score/commit 子流程 |
| `execution/backend.py` | 新增 `set_predict_features` | 删除该协议方法，改为执行上下文传递 |
| `execution/remote/mirrored.py` | 转发 `set_predict_features` | 删除 |
| `execution/remote/ssh.py` | 显式拒绝 `set_predict_features` | 删除 |
| `execution/runtime.py` | `EnvironmentManager` 多 attr、`predicting` 上下文 | 删除 mutable predict target；`ExecutionContext.predict_features` 作为一次调用上下文 |
| `research/prepare_phase.py` | 超长 monolith | 拆阶段函数 + `PlatformSplit` / `DataContract` 值对象 |
| `research/cli.py` | `_runtime_options` 返回长 dict | 引入 `CliRunConfig` 值对象再转为 runtime kwargs |
| `research/supervisor/plans.py` / `supervisor.py` / `experiment.py` | 失败反馈分散 | `PlanFailure` 封装 summary/prompt block |
| `research/report.py` | 报告增长但函数可读 | 可拆 `_iteration_table` / `_failed_experiments` / `_next_steps` 小函数 |

---

## 5. 预期接口变化

### 减少的 public interface

- `ExecutionBackend` 协议少 1 个方法：`set_predict_features`。
- 后端实现少 3 个方法。
- `ExecutionRuntime` 少 1 个公共方法：`predicting`。
- `EnvironmentManager` 少 1 个 attr：`_predict_features`。
- `run_validation_plan` 参数从约 13 个降到 4 个。
- `_execute_predictions` 参数从 6 个降到 1 个对象。
- `split_ids` / `materialize_csv_split` 参数各减少 3 个左右。
- `run_prepare_phase` 内部不再需要维护十多个局部变量。

### 新增的 public interface

- `SplitSpec`
- `ValidationDeps`
- `ValidationOptions`
- `PredictionRun`
- `PlatformSplit`
- `DataContract`
- `PlanFailure`
- 每项都是小、不可变、语义明确的值对象。

---

## 6. 实施顺序

1. 先改 `splitter.py`：引入 `SplitSpec`。
2. 再改 execution：把 `predict_features` 移到 `ExecutionContext`，删除 `set_predict_features` 全链路。
3. 改 validation：引入 `ValidationDeps` / `ValidationOptions` / `PredictionRun`。
4. 改 prepare_phase：拆分阶段 + 引入 `PlatformSplit` / `DataContract`。
5. 改 supervisor 失败反馈：引入 `PlanFailure`。
6. 改 CLI 与 report 的可读性（低优先级）。

---

## 7. 验收

- 公共函数/类数量变化满足“接口更少”的目标。
- 任何新增执行选项只改一个值对象，不扩散到所有调用点。
- 不存在“执行预测目标需要临时改全局状态”的代码。
- `prepare_phase` 每个子阶段可在不读完整函数的情况下单独理解。
- 现有测试仍通过；新增测试覆盖值对象序列化/默认值。

---

## 8. 更多可简化的内容

### 8.1 `CommandRequest`：把命令执行参数收拢成一个对象

当前：

```python
ExecutionRuntime.run(
    context,
    command=None,
    *,
    argv=None,
    timeout_s=120,
    workdir=None,
    emit=None,
)

LocalBackend.run(
    *,
    command=None,
    argv=None,
    workspace_root,
    workdir,
    timeout_s,
    emit=None,
)
```

问题：

- 命令、超时、工作目录、事件回调是同一件事的四个面。
- 后端协议与前端门面各重复一遍参数。
- 以后新增“是否注错”“是否保留完整输出”又要继续加参数。

建议：

```python
@dataclass(frozen=True, slots=True)
class CommandRequest:
    command: str | None = None
    argv: list[str] | None = None
    timeout_s: int = 120
    workdir: Path | None = None
    emit: EmitEvent | None = None
    predict_features: Path | None = None
```

之后：

```python
await runtime.run(context, request)
await backend.run(workspace_root, request)
```

好处：

- `ExecutionRuntime.run` 从 6 个参数降到 2 个。
- `LocalBackend.run` 从 6 个参数降到 2 个。
- `predict_features` 也自然成为请求的一部分，不需要再单独设计 setter/context manager。

---

### 8.2 `LlmConfig`：把 settings 的多个小读取收拢成一次加载

当前 `settings.py` 有多个独立小函数：

```python
provider_kind()
model_name()
pro_model_name()
temperature()
seed()
enable_thinking()
base_url()
max_retries()
```

问题：

- 每个函数都重复读环境变量 / config。
- 调用者无法一眼看出哪些配置属于同一逻辑组。
- 后续新增一个 LLM 配置项，又要新增一个函数。

建议：

```python
@dataclass(frozen=True, slots=True)
class LlmConfig:
    provider: str
    model: str
    pro_model: str
    base_url: str | None
    temperature: float
    seed: int | None
    enable_thinking: bool
    max_retries: int

    @classmethod
    def load(cls) -> "LlmConfig": ...
```

之后：

- `settings.llm()` 返回一个 `LlmConfig`。
- provider / models / create_provider 只接受配置对象，不再自己到处 `settings.xxx()`。
- 新增配置项只改 `LlmConfig`，不扩散到所有 provider / runtime。

---

### 8.3 `PromptBlock`：统一所有 prompt 块格式

当前 `experiment.py` 里存在多个结构相同但手写格式的 block：

- `hypothesis_block`
- `handoff_block`
- `data_contract_block`
- `failure_block`
- `_corpus_block`

它们都重复“边界标记 + 正文 + 结尾标记”的字符串格式。

建议：

```python
def prompt_block(title: str, body: str, *, instruction: str = "") -> str:
    return (
        f"\n\n--- {title} ---\n"
        f"{body}\n"
        f"{('' + instruction) if instruction else ''}"
        f"--- end of {title} ---"
    )
```

每个 block 只负责生成 body：

```python
def failure_block(kind: str, error: str) -> str:
    body = f"Failure kind: {kind}\nDetail: {detail}"
    return prompt_block("Previous attempt failed", body, instruction="...")
```

好处：

- 格式统一，修改视觉边界只需改一处。
- 减少重复字符串模板。
- 新 block 不需要重抄边界格式。

---

### 8.4 `ReportBuilder`：把 `build_final_report` 拆成小节

当前 `build_final_report` 一个函数承担：

- SOTA 概要
- 实验总数
- VALIDATE 结论
- 次要指标
- 迭代对照
- 失败实验与原因
- 待选假设
- 下一步验证方案
- 实验记录

建议：

```python
class ReportBuilder:
    def __init__(self, tree, validation): ...
    def sota_section(self) -> list[str]: ...
    def iteration_table(self) -> list[str]: ...
    def failed_experiments(self) -> list[str]: ...
    def next_steps(self) -> list[str]: ...
    def build(self) -> str: ...
```

或者至少改成私有纯函数：

```python
_sota_section(data, sota_id)
_iteration_table(data, sota_id)
_failed_experiments(data)
_pending_hypotheses(data)
_next_steps(data)
```

好处：

- 每个 section 可独立测试。
- 新增报告段落不会继续膨胀主函数。
- 评审只需要看对应 section 函数。

---

### 8.5 分组运行配置：已收缩 `ResearchRuntime.__init__`

当前构造器只有 `project_root`、`session`、`research`、`dependencies` 四个输入。数据集划分由四字段 `DatasetConfig` 负责，执行数据根目录由三字段 `ExecutionConfig` 负责；两者不再混在一个长参数列表中。

```python
runtime = ResearchRuntime(
    project_root=project,
    session=SessionConfig(session_id="default"),
    research=ResearchOptions(
        task=TaskConfig(text=task),
        dataset=DatasetConfig(path=dataset),
    ),
    dependencies=RuntimeDependencies(
        provider=ProviderConfig(model=model),
    ),
)
```

`ResearchRuntime` 只接受项目根目录和三个职责分组，不再接受平铺的模型、任务、搜索、确认、数据集和执行关键字。

好处：

- 新增数据集划分配置只改 `DatasetConfig`，执行环境配置只改 `ExecutionConfig`。
- 构造 runtime 的调用者不再需要理解 30 多个散参。
- GUI / CLI / TUI 只构造自己需要改变的职责分组。

---

### 8.6 `ValidationSession`：把 `run_validation_plan` 的恢复状态机变成类

当前函数内用 `action ="run"/"score"/"commit"` 驱动状态流转，状态变量散落在函数局部。

建议：

```python
@dataclass
class ValidationSession:
    input: ValidationInput
    deps: ValidationDeps
    options: ValidationOptions
    result_ref: ArtifactRef | None
    current: ValidationResult | None = None

    async def run(self) -> None: ...
    async def score(self) -> None: ...
    async def commit(self) -> None: ...
    async def execute(self) -> ValidationResult:
        action = await recovery_action(...)
        if action == "run": await self.run()
        ...
        return self.current
```

好处：

- `current`/`action` 不再作为函数局部变量在超长函数中穿梭。
- 每个阶段可独立测试。
- 恢复逻辑的“run/score/commit”一目了然。

---

### 8.7 `SplitIO`：把 CSV 读取/写出从 `materialize_csv_split` 中剥离

当前 `materialize_csv_split` 内部包含：

- 读取 CSV
- 构造 `by_id`
- 两个内部闭包 `feature_rows` / `label_rows`
- 写 5 个文件
- 写 manifest

建议：

```python
class CsvTable:
    fieldnames: list[str]
    rows: list[dict[str, str]]

    def by_id(self) -> dict[str, dict[str, str]]: ...
    def feature_rows(self, manifest: SplitManifest) -> dict[str, list[dict]]: ...
    def label_rows(self, manifest: SplitManifest, target: str) -> dict[str, list[dict]]: ...

class SplitWriter:
    def __init__(self, output_dir: Path): ...
    def write_all(self, table: CsvTable, manifest: SplitManifest, target: str) -> None: ...
    def write_manifest(self, source_csv, target, spec) -> None: ...
```

好处：

- `materialize_csv_split` 只剩“读取 → 切分 → 写出”三步。
- 局部闭包和重复循环消失。
- 文件布局变更只影响 `SplitWriter`。

---

### 8.8 `EvaluatorWorkspace`：封装 evaluator 隔离逻辑

当前 `_run_evaluator_agent` 中每次都要：

- 建目录
- 判断 registry 是否已有 evaluator
- unregister / register
- 跑 evaluator plan
- 返回 ref

建议：

```python
@dataclass(frozen=True, slots=True)
class EvaluatorWorkspace:
    directory_name: str
    agent_id: str
    plan_id: str
    label: str
    is_final: bool = False

    async def run(self, rt, task: str) -> ArtifactRef: ...
```

`prepare_phase` 不再手动处理 registry 生命周期，只构造两个 `EvaluatorWorkspace`。

---

### 8.9 CLI 配置翻译：已删除字符串字典与 `CliRunConfig`

`_runtime_options` 直接返回 `ResearchOptions` 与 `RuntimeDependencies`。CLI 不再先创建 18 字段中间类，再把它转换成字符串键字典。

结果：

- 不再靠字符串键传递。
- 新增参数有类型检查。
- 测试更容易构造。

---

## 9. 更远期的接口收敛方向

- 将所有“Prompt 块”统一为 `PromptBlock`。
- 将所有“执行请求”统一为 `CommandRequest`。
- 将所有“运行配置”统一为 `ResearchOptions` / `RuntimeDependencies`。
- 将 `Supervisor` 的多个 `read_*` 查询合并为 `SnapshotService`。
- 将 `PhaseMachine` 的控制动作收敛为 `ControlCommand` 协议。

这些不是必须立刻做，但能显著减少后续维护者需要认识的公开接口数量。
