# Athena AI4ML 后端实现设计（Codex 对齐版）

> 日期：2026-07-28
> 状态：Codex 对齐设计，替代旧版
> 参考：
>   `C:/Users/80163/Desktop/挑战杯_2026/codex/codex-rs/`（Codex 上游设计）
>   `docs/design.md`（原始设计稿）
>   `docs/superpowers/specs/2026-07-27-athena-ai4ml-design.md`（AI4ML 管线）
>   `docs/superpowers/specs/2026-07-25-athena-rust-design.md`（Rust 迁移）

## 设计原则（源自 Codex AGENTS.md）

目前 `app_server/` 和 `memory/` 已按 Codex 模式实现。其余模块对齐：

1. **小模块**：每文件 <500 行（不含测试），大文件拆分为子模块
2. **存储边界**：关键数据使用 `*Store` trait + `local_*` / `in_memory_*` 实现（参见 Codex `agent-graph-store/src/store.rs`、`thread-store/src/store.rs`）
3. **类型分离**：每个 package 含 `types.py` 存放数据类，`store.py` 定义 trait，具体实现在 `local_*.py`
4. **agent/ 子结构**：`registry.py`（发现）、`control.py`（生命周期）、`role.py`（能力）、`status.py`（状态）——对齐 Codex `core/src/agent/`
5. **命名对齐 Codex**：
   - `experiment_id`（Codex: `thread_id`）
   - `ExperimentStore`（Codex: `AgentGraphStore`）
   - `HypothesisStore`（类比 Codex `ThreadSpawnEdgeStatus`）
   - `local_experiment_store`（Codex: `local.rs`）

---

## 总览

### 新版目录结构

```
src/athena/
│
├── code/                    # 代码执行引擎（Codex: tools/ 的部分职责）
│   ├── __init__.py
│   ├── types.py             # ExecutionOutput, GenerationResult, EngineResult
│   ├── engine.py            # CodeEngine: generate→execute→observe→iterate
│   ├── runner.py            # subprocess + .ipynb 执行
│   ├── monitor.py           # 超时 + 死锁检测
│   ├── review.py            # diff 审查（确定性，不调 LLM）
│   └── backends/
│       ├── __init__.py
│       ├── base.py          # CodeBackend ABC
│       ├── qoder.py         # Qoder backend
│       └── codex.py         # Codex backend
│
├── agents/                  # Agent = 注册 + 角色 + 控制（Codex: agent/）
│   ├── __init__.py
│   ├── types.py             # AgentResult, AgentRole
│   ├── registry.py          # agent 注册与发现
│   ├── control.py           # 生命周期管理
│   ├── role.py              # 角色定义（code/data/plot）
│   ├── output_specs.py      # 产出约束
│   └── prompts/             # System prompts（Codex: prompts/）
│       ├── code_agent.md
│       ├── data_agent.md
│       └── plot_agent.md
│
├── data/                    # 纯数据操作（Codex: shell/ + utils/）
│   ├── __init__.py
│   ├── types.py             # DataProfile, ColumnSummary, ProcessingLog, ProcessingRecord
│   ├── tools.py             # get_schema, get_summary, get_sample
│   └── operations.py        # sample, clean_column, apply_encoding, split_data
│   # .ipynb 执行在 code/runner.py，ML 模型架构由 LLM+RAG 自行探索
│
├── retrieval/               # 文献 + 模型检索（Codex: web_search.rs）
│   ├── __init__.py
│   ├── types.py             # PaperRef, HFModelRef
│   └── store.py             # Retriever: arxiv → semantic_scholar → web 降级链
│
├── brainstorm/              # 头脑风暴假设生成（Codex: prompts/）
│   ├── __init__.py
│   ├── types.py             # HypothesisInput, BrainStormResult
│   └── generate.py          # brainstorm() + check_falsifiability()
│
├── experiment/              # 实验编排（Codex: agent-graph-store/）
│   ├── __init__.py
│   ├── types.py             # Experiment, ExpCkpt, ExperimentOutcome, ExperimentStatus
│   ├── store.py             # ExperimentStore trait（Codex: AgentGraphStore）
│   ├── local_store.py       # InMemoryExperimentStore
│   ├── ranking.py           # Bradley-Terry + Proximity
│   ├── pipeline.py          # PREPARE 状态机: SAMPLING→EDA→CLEANING→SPLITTING→DONE
│   ├── search_loop.py       # SEARCH 编排
│   ├── validate.py          # Ablation + final test
│   ├── report.py            # Reporter
│   └── baseline.py          # 创建 baseline
│
├── evaluation/              # 评估体系（独立于实验编排）
│   ├── __init__.py
│   ├── types.py             # MetricDef, EvalSpec, EvalResult, ComparisonVerdict
│   ├── comparator.py        # Comparator.paired_test()
│   ├── evaluator.py         # Evaluator.evaluate()
│   └── factory.py           # EvaluatorFactory.build() + freeze()
│
├── integrations/            # 外部平台集成
│   ├── __init__.py
│   └── kaggle/
│       ├── __init__.py
│       ├── types.py         # CompetitionInfo, SubmissionResult 等
│       └── client.py        # KaggleClient（kagglehub 实现）
│
├── core/                    # 不变：schemas, budget, tool, agent, gitutils
├── app_server/              # 不变（已对齐 Codex）
├── memory/                  # 不变（已对齐 Codex）
├── ide/                     # 不变
└── storage/                 # 保留（后续按 store trait 实现）
```

### 与旧设计的对应关系

| 旧路径 | 新路径 | 对齐方式 |
|--------|--------|---------|
| `knowledge/retrieval.py` | `retrieval/store.py` | Codex `web_search.rs` 命名 |
| `knowledge/brainstorm.py` | `brainstorm/generate.py` | 动词命名，对齐 prompts/ |
| `knowledge/` | 删除 | 拆为 `retrieval/` + `brainstorm/` |
| `research/tree.py` | `experiment/store.py` + `experiment/local_store.py` | Codex `agent-graph-store/` 模式 |
| `research/ranking.py` | `experiment/ranking.py` | 留在实验域 |
| `research/evaluation.py` | `evaluation/` | 独立 package |
| `research/supervisor.py` | `experiment/` 或 `code/review.py` | Supervisor 决策留在 experiment/ |
| `research/pipeline.py` | `experiment/pipeline.py` | 状态机留在实验域 |
| `research/search_loop.py` | `experiment/search_loop.py` | 编排 |
| `research/validate.py` | `experiment/validate.py` | 验证 |
| `research/report.py` | `experiment/report.py` | 报告 |
| `agents/factory.py` | `agents/registry.py` + `agents/control.py` | 对齐 Codex agent/ 子结构 |

### 删除目录

- `execution/` — 迁到 `code/monitor.py` + `code/review.py`
- `workflows/` — 迁到 `experiment/` + `evaluation/`
- `core/research/` — 迁到 `experiment/store.py`
- `research/` — 拆为 `experiment/` + `evaluation/`
- `knowledge/` — 拆为 `retrieval/` + `brainstorm/`
- `agents/control/`, `agents/policy/`, `agents/prepare/`, `agents/search/` 空壳 — 删除

---

## 1. code/ — 代码执行引擎

### 1.1 types.py

```python
"""code/ 数据类型（对齐 Codex tools/src/tool_spec.rs 模式）"""
from dataclasses import dataclass, field


@dataclass
class ExecutionOutput:
    """子进程执行结果（对齐 Codex exec/ 模块）"""
    stdout: str
    stderr: str
    returncode: int
    files: list[str] = field(default_factory=list)


@dataclass
class GenerationResult:
    """LLM 代码生成结果"""
    files_created: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    output: str = ""


@dataclass
class EngineResult:
    """CodeEngine 迭代循环结果"""
    rounds: int = 0
    final_output: ExecutionOutput | None = None
    files: list[str] = field(default_factory=list)
    success: bool = False
```

### 1.2 backends/base.py — CodeBackend trait

对齐 Codex `model-provider/` 的 provider trait 模式：

```python
class CodeBackend(ABC):
    """LLM 代码生成后端（trait，对齐 Codex Provider trait）"""
    @abstractmethod
    async def generate(self, prompt: str, target_dir: str,
                       previous_outputs: list[ExecutionOutput],
                       history: list[dict]) -> GenerationResult: ...
```

### 1.3 engine.py — CodeEngine

对齐 Codex `tools/src/orchestrator.rs` 循环模式。

### 1.4 review.py — CodeReview

确定性 diff 审查，对齐 Codex `core/src/review_format.rs`。

---

## 2. agents/ — Agent 系统

对齐 Codex `core/src/agent/` 目录结构：

### 2.1 registry.py（对齐 Codex `agent/registry.rs`）

```python
"""Agent 注册表：发现、创建、销毁 agent"""
def create_agent(role: AgentRole, target_dir: str, engine: CodeEngine) -> Agent: ...
def list_roles() -> list[AgentRole]: ...
```

### 2.2 role.py（对齐 Codex `agent/role.rs`）

```python
class AgentRole(Enum):
    CODE = "code"      # ML 实验代码
    DATA = "data"      # 数据分析
    PLOT = "plot"      # 图表生成
```

### 2.3 control.py（对齐 Codex `agent/control.rs`）

```python
class Agent:
    """Agent 生命周期控制（对齐 Codex AgentControl）"""
    async def run(self, task: str, context: dict | None = None) -> AgentResult: ...
    def interrupt(self, reason: str) -> None: ...
```

### 2.4 types.py

```python
@dataclass
class AgentResult:
    success: bool = False
    output: str = ""
    files: list[str] = field(default_factory=list)
    rounds: int = 0
```

---

## 3. data/ — 数据操作

对齐 Codex `shell/` + `file-system/` 的纯工具模式。无状态，无 LLM。

### 3.1 types.py

```python
"""数据域类型（对齐 Codex types 模式）"""
from pydantic import BaseModel, Field
from athena.core.schemas import ArtifactRef

class ColumnSummary(BaseModel): ...
class DataProfile(BaseModel): ...
class ProcessingLog(BaseModel): ...

@dataclass
class ProcessingRecord:
    col: str
    operation: str
    params: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
```

### 3.2 tools.py / operations.py / notebook.py

保持原有设计，不调 LLM。

---

## 4. retrieval/ — 文献检索

对齐 Codex `web_search.rs` 的单文件检索模式。

### 4.1 types.py

```python
@dataclass
class PaperRef:
    title: str
    source: str  # "arxiv" | "semantic_scholar" | "web" | "llm_常识"
    url: str = ""
    key_findings: str = ""
    relevance: str = ""
    markdown_ref: str = ""

@dataclass
class HFModelRef:
    repo: str
    revision: str = "main"
    license: str = ""
    param_count: int | None = None
    task_match: str = ""
```

### 4.2 store.py

```python
class Retriever:
    """检索 trait（对齐 Codex web_search 抽象）"""
    async def search_papers(self, query: str, n: int = 5) -> list[PaperRef]: ...
    async def search_models(self, query: str, n: int = 3) -> list[HFModelRef]: ...
```

---

## 5. brainstorm/ — 假设生成

对齐 Codex `prompts/` 的 prompt 驱动模式。

### 5.1 types.py

```python
@dataclass
class HypothesisInput:
    data_profile: DataProfile
    papers: list[PaperRef]
    models: list[HFModelRef]
    existing_hypotheses: list[Hypothesis]

class FalsifiabilityError(ValueError): ...
```

### 5.2 generate.py

```python
async def brainstorm(input: HypothesisInput, llm=None) -> list[Hypothesis]: ...
def check_falsifiability(h: Hypothesis) -> None: ...
```

---

## 6. experiment/ — 实验编排

对齐 Codex `agent-graph-store/` 的 store trait 模式。

### 6.1 types.py

对齐 Codex `agent-graph-store/src/types.rs`：

```python
class ExperimentStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

@dataclass
class Experiment:
    id: str                             # experiment_id
    parent_id: str | None               # 父实验（Codex: parent_thread_id）
    commit: str                         # Git commit
    hypothesis: Hypothesis
    plan: ExperimentPlan
    metric_type: str
    result: str | float
    status: ExperimentStatus = ExperimentStatus.PENDING

@dataclass
class ExperimentOutcome:
    experiment_id: str
    eval_result: EvalResult
    verdict: ComparisonVerdict | None = None
    is_sota: bool = False
```

### 6.2 store.py — ExperimentStore trait

对齐 Codex `agent-graph-store/src/store.rs`：

```python
class ExperimentStore(ABC):
    """实验存储边界（对齐 Codex AgentGraphStore trait）"""

    @abstractmethod
    async def upsert_experiment(self, experiment: Experiment) -> None:
        """插入或更新实验节点"""

    @abstractmethod
    async def get_experiment(self, experiment_id: str) -> Experiment | None:
        """按 ID 查询实验"""

    @abstractmethod
    async def list_children(self, parent_id: str,
                            status_filter: ExperimentStatus | None = None) -> list[str]:
        """列出子实验 ID（对齐 Codex list_thread_spawn_children）"""

    @abstractmethod
    async def list_descendants(self, root_id: str,
                               status_filter: ExperimentStatus | None = None) -> list[str]:
        """按 BFS 列后代实验 ID（对齐 Codex list_thread_spawn_descendants）"""

    @abstractmethod
    async def best_experiment(self) -> str | None:
        """返回 SOTA 实验 ID"""

    @abstractmethod
    async def set_sota(self, experiment_id: str, is_sota: bool) -> None:
        """标记 SOTA"""

    @abstractmethod
    async def add_hypothesis(self, hypothesis: Hypothesis) -> None:
        """注册假设"""

    @abstractmethod
    async def pending_hypotheses(self) -> list[Hypothesis]:
        """待执行假设"""

    @abstractmethod
    async def update_hypothesis_status(self, hypothesis_id: str,
                                       status: str) -> None:
        """更新假设状态"""
```

### 6.3 local_store.py — InMemoryExperimentStore

```python
class InMemoryExperimentStore(ExperimentStore):
    """内存实现（对齐 Codex in_memory.rs / local.rs）"""
    def __init__(self):
        self._experiments: dict[str, Experiment] = {}
        self._hypotheses: dict[str, Hypothesis] = {}
        self._sota_id: str | None = None
        self._children: dict[str, list[str]] = {}  # parent_id → [child_id]
        ...
```

### 6.4 pipeline.py — PREPARE 状态机

```python
class DataPipeline:
    """PREPARE: SAMPLING→EDA→CLEANING→SPLITTING→DONE（对齐 Codex session 状态机）"""
    async def run(self, data_path: str, data_agent: Agent, plot_agent: Agent | None = None) -> DataProfile: ...
```

### 6.5 search_loop.py — SEARCH 编排

保持现有逻辑不变，依赖注入改为：
```python
class SearchLoop:
    def __init__(self, experiment_store: ExperimentStore, ...): ...
```

### 6.6 其余文件

`validate.py`、`report.py`、`baseline.py`、`ranking.py` 保持原有逻辑，接口适应新类型。

---

## 7. evaluation/ — 评估体系

从 `research/evaluation.py` 独立为新 package，对齐 Codex 单职责模式。

### 7.1 types.py

```python
class MetricDef(BaseModel):
    name: str
    direction: Literal["maximize", "minimize"]
    description: str

class EvalSpec(BaseModel):
    model_config = {"frozen": True}
    primary: MetricDef
    secondary: list[MetricDef] = []
    split_seed: int = 42
    test_ratio: float = 0.2

class EvalResult(BaseModel):
    experiment_id: str  # 对齐 Codex 命名
    primary: float
    secondary: dict[str, float] = {}
    per_sample: ArtifactRef

class ComparisonVerdict(BaseModel):
    winner: Literal["baseline", "candidate", "tie"]
    p_value: float
```

### 7.2 evaluator.py / comparator.py / factory.py

保持原有实现，类型引用更新为 `evaluation/types.py`。

---

## 8. integrations/kaggle/ — Kaggle 客户端

对齐 Codex `mcp-server/` 的外部集成模式。

### 8.1 types.py

```python
@dataclass
class CompetitionInfo: ...
@dataclass
class CompetitionSummary: ...
@dataclass
class SubmissionResult: ...
```

### 8.2 client.py

```python
class KaggleClient:
    """Kaggle API 客户端（对齐 Codex MCP client 模式）"""
    def set_competition(self, competition_id: str) -> None: ...
    def get_competition_info(self) -> CompetitionInfo: ...
    async def download_dataset(self, target_dir: str | None = None) -> ArtifactRef: ...
    # 预留接口:
    async def list_competitions(self, ...) -> list[CompetitionSummary]: ...
    async def submit(self, ...) -> SubmissionResult: ...
    async def leaderboard(self) -> list[LeaderboardEntry]: ...
```

---

## 9. 变量命名对齐（Codex ↔ Athena）

| Codex | Athena | 说明 |
|-------|--------|------|
| `ThreadId` | `ExperimentId` / `experiment_id` | 主标识符 |
| `AgentGraphStore` | `ExperimentStore` | 存储 trait |
| `list_thread_spawn_children` | `list_children` | 子节点查询 |
| `list_thread_spawn_descendants` | `list_descendants` | 后代查询 |
| `upsert_thread_spawn_edge` | `upsert_experiment` | 插入/更新 |
| `ThreadSpawnEdgeStatus` | `ExperimentStatus` | 生命周期状态 |
| `tool_call_id` | `execution_id` | 执行标识 |
| `AgentControl` | `Agent.control` | 生命周期管理 |
| `agent_resolver` | `agent/registry.py` | Agent 发现 |
| `context/` → 多个小文件 | `memory/` → 保持不变 | 已对齐 |

---

## 10. 实现顺序

```
Phase 1: code/types.py + code/ (CodeEngine)        — 不依赖新目录结构
Phase 2: agents/ (registry + control + role)        — 对齐 Codex agent/ 子结构
Phase 3: data/types.py + data/ (工具 + 操作)        — 类型分离
Phase 4: retrieval/ + brainstorm/                   — 原 knowledge/ 拆分
Phase 5: evaluation/                                — 原 research/evaluation.py 独立
Phase 6: experiment/ (store + pipeline + loop)       — 编排层，原 research/ 迁移
Phase 7: integrations/kaggle/                       — Kaggle 客户端
Phase 8: 删除旧目录 + 全量测试                      — cleanup
```

---

## 11. 验收标准

1. 所有新 package 含 `types.py`（类型分离）
2. `experiment/store.py` 含 `ExperimentStore` trait + `local_store.py` 含 `InMemoryExperimentStore` 实现
3. `agents/` 含 `registry.py`、`control.py`、`role.py`（对齐 Codex agent/ 子结构）
4. 旧目录 `execution/`、`workflows/`、`research/`、`knowledge/` 删除
5. 命名对齐 Codex：`experiment_id`、`ExperimentStore`、`list_children` 等
6. 全量测试通过
