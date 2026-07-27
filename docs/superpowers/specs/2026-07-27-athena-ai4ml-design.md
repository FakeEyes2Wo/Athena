# Athena AI4ML 设计规范

> 日期：2026-07-27
> 状态：设计完成，待实施
> 关联文档：`docs/design.md`（原始设计稿）、`docs/memory_design.md`（Memory 层）、`docs/superpowers/specs/2026-07-25-athena-rust-design.md`（Rust 迁移）

---

## 0. 公共协议

所有模块共用的最小类型和写入原则。

### 0.1 ID 体系

```python
RunId        = NewType("RunId", str)
HypothesisId = NewType("HypothesisId", str)
ExperimentId = NewType("ExperimentId", str)
ArtifactRef  = NewType("ArtifactRef", str)   # "<store_key>:<sha256>"

def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"
```

### 0.2 事件信封

```python
class EventEnvelope(BaseModel):
    kind: str                         # 各模块自定义事件名
    source: str                       # 产生事件的组件
    payload: dict                     # 模块自定义
    state_version: int                # ResearchTree 乐观锁版本
```

### 0.3 Agent 任务/结果

```python
class AgentTask(BaseModel):
    task_id: str
    agent_type: str                   # "DataAgent" | "ResearchAgent" | "CodeAgent" | "PlotAgent"
    command: str
    context_refs: list[ArtifactRef] = []

class AgentResult(BaseModel):
    task_id: str
    status: Literal["OK", "FAIL", "TIMEOUT"]
    output_refs: list[ArtifactRef] = []
    proposed_commands: list[dict] = [] # Agent 建议的状态变更
    error: str | None = None
```

### 0.4 错误

```python
class ErrorRecord(BaseModel):
    severity: Literal["retry", "degrade", "fatal"]
    code: str
    message: str
    retry_count: int = 0
```

### 0.5 预算

```python
class BudgetSnapshot(BaseModel):
    remaining: int                     # 剩余实验次数
    no_improve_streak: int = 0
    max_no_improve: int = 5
    is_exhausted: bool = False
```

### 0.6 运行模式

```python
class RunMode(BaseModel):
    hil: bool = False                  # 每次实验完成暂停，等待人工确认
    debug: bool = False                # 每次 LLM 交互前暂停
```

### 0.7 状态写入原则

> Agent 提交 AgentResult → Supervisor 校验 state_version 并执行状态转换 → Scheduler 发布 DomainEvent → ResearchTree 产生新 state_version。Agent 不直接写 ResearchTree。

---

## 1. 评估体系

### 职责
定义评什么（名字 + 描述），冻结，收结果，比大小 + 显著性检验。

### Schema

```python
class MetricDef(BaseModel):
    name: str
    direction: Literal["maximize", "minimize"]
    description: str                  # 交给 CodeAgent 的 prompt

class EvalSpec(BaseModel):
    """PREPARE 阶段产出，SEARCH 开始前冻结。"""
    primary: MetricDef
    secondary: list[MetricDef] = []
    split_seed: int = 42
    test_ratio: float = 0.2

class EvalResult(BaseModel):
    experiment_id: str
    primary: float
    secondary: dict[str, float]       # {name: value}
    per_sample: ArtifactRef

class ComparisonVerdict(BaseModel):
    winner: Literal["baseline", "candidate", "tie"]
    p_value: float
```

### 工作方式

`EvalSpec` 冻结后通过 `context_refs` 传给 CodeAgent。CodeAgent 在 Sandbox 中写 `eval.py`，读取预测文件和标签，输出 `EvalResult`。Evaluator 拿两个 `EvalResult` 内部的 `per_sample` 做统计检验输出 `ComparisonVerdict`。

默认指标集（按 task_type 自动填充，CodeAgent 可改写）：

| task_type | primary | secondary |
|-----------|---------|-----------|
| classification | f1_macro | accuracy, precision_macro, recall_macro |
| regression | rmse | mae, r2 |
| binary_classification | roc_auc | f1, precision, recall |

### 验收
- 同预测文件同分数（幂等）
- 真实提升检出 SUPPORTED，随机噪声检出 REFUTED
- `EvalSpec` 冻结后不可变

---

## 2. DataAnalysis

### 职责
接收原始数据 → 保存副本 → EDA → 数据清洗 → 特征处理 → 产出 `DataProfile` + 清洗后数据 + 数据切分。

### Schema

```python
class DataProfile(BaseModel):
    row_count: int
    col_count: int
    columns: list["ColumnSummary"]
    missing_rate: float
    task_type_hint: str              # DataAgent 推断
    target_col: str | None
    issue_summary: str

class ColumnSummary(BaseModel):
    name: str
    dtype: str
    missing_rate: float
    n_unique: int | None
    sample_values: list[str]         # 前 5 个非空值
    processing: str                   # 对该列的处理记录

class ProcessingLog(BaseModel):
    columns: dict[str, ColumnSummary]
    raw_copy: ArtifactRef
    cleaned_data: ArtifactRef
    splits: dict[str, ArtifactRef]   # {"train": ref, "val": ref, "test": ref}
```

### 流程

```
原始数据 → 保存副本 → 抽样(大数据集, 3 seeds) → EDA → 清洗 → 切分 → DataProfile
```

大数据集（>10万行）自动抽样，3 个不同 seed 取 3 份抽样数据交叉验证分析结论。

### DataAgent 工具

```python
class DataTools:
    def sample(self, seed: int, n: int = 10000) -> ArtifactRef: ...
    def describe(self, data_ref: ArtifactRef) -> str: ...
    def head(self, data_ref: ArtifactRef, n: int = 5) -> str: ...
    def value_counts(self, data_ref: ArtifactRef, col: str) -> dict: ...
    def missing_matrix(self, data_ref: ArtifactRef) -> ArtifactRef: ...
    def correlation_matrix(self, data_ref: ArtifactRef) -> ArtifactRef: ...
```

### EDA 与 PlotAgent 协作

1. DataAgent 生成 `EDA.md` 文字草稿，声明每节需要什么图
2. PlotAgent 根据规格生成图片，放入 `./data_analyze/EDA.md/` 目录
3. 图片引用回填到 `EDA.md`，PlotAgent 不参与数据分析决策

### 状态机

```
SAMPLING → EDA → CLEANING → SPLITTING → DONE
   ↑         ↑        ↑           ↑
   └─── 每步可回退到上一步 ──────────┘
```

### 验收
- 大数据集 3-seed 抽样一致性
- `ProcessingLog` 完整可追溯每一列的处理
- train/val/test 无重叠

---

## 3. IdeaGeneration + RAG

### 职责
基于 `DataProfile` 检索相关论文/技术方案 → 生成可证伪 `Hypothesis` → 挂到 `ResearchTree`。

### Schema

```python
class PaperRef(BaseModel):
    title: str
    source: str                      # "arxiv" | "semantic_scholar" | "web"
    url: str
    key_findings: str                # LLM 提取的摘要
    relevance: str                   # 与本任务的关联说明
    markdown_ref: ArtifactRef        # PDF→Markdown 产物

class HFModelRef(BaseModel):
    repo: str
    revision: str
    license: str
    param_count: int | None
    task_match: str

class Hypothesis(BaseModel):
    id: str
    parent_id: str | None            # 挂到哪个 ExpCkpt 下
    statement: str                   # 可证伪陈述
    intervention: str                # 最小改动
    expected_effect: str             # 预期效果
    sources: list[str]               # PaperRef.url 或 HFModelRef.repo
    status: Literal["proposed", "selected", "supported", "refuted", "failed"] = "proposed"
```

### 流程

```
DataProfile → LLM 生成搜索查询 → 多源检索(arxiv/semantic_scholar/web/HuggingFace)
    → PDF→Markdown 转换 → LLM 逐论文提取关键发现
    → LLM 生成 3-5 个 Hypothesis，每个包含可证伪陈述 + 最小干预 + 文献引用
    → 自检可证伪性 → 通过检查的挂到 ResearchTree
```

### 检索降级

| 优先级 | 来源 | 失败时 |
|--------|------|--------|
| 1 | arxiv API | 降级到 semantic_scholar |
| 2 | semantic_scholar | 降级到 web search |
| 3 | web search | 降级到 LLM 常识生成（标记无文献支撑） |
| 4 | HuggingFace | 独立于论文检索，失败不影响 Hypothesis 生成 |

### 验收
- 每个 Hypothesis 可证伪（可通过一次实验判定对错）
- 每个 Hypothesis 有来源引用或标注无文献支撑
- 检索失败不阻塞管线

---

## 4. Hypothesis Ranking + Proximity

### 职责
Bradley-Terry 评分 + UCB 选择 + Proximity 去重。纯算法，不调 LLM。

### 算法

```python
class HypothesisRanker:
    """Bradley-Terry 评分 + UCB 选择 + Proximity 去重。"""

    def update(self, comparisons: list[tuple[str, str, bool]]) -> None:
        """(winner_id, loser_id, is_tie) → 批量 MLE 更新强度参数。"""

    def select(
        self,
        hypotheses: list[Hypothesis],
        proximity: "ProximityGraph",
        beta: float = 2.0,
    ) -> str:
        """返回 selection_score 最高的 hypothesis_id。

        selection_score = μ + β·σ - λ·min_distance_to_executed(h)

        其中 μ 为 Bradley-Terry 估计强度，σ 为 Bootstrap 标准差。
        执行次数少的 Hypothesis 天然 σ 大 → 自动探索。
        λ 小，仅用于去重，不影响探索/利用平衡。
        """

class ProximityGraph:
    def add(self, h: Hypothesis) -> None: ...
    def min_distance_to_executed(self, h: Hypothesis) -> float: ...
```

相似度 = `statement + intervention` 的 embedding 余弦距离。Embedding 使用本地模型，不调 API。

### 对比实验结果更新评分

| 结果 | ELO 操作 |
|------|---------|
| SUPPORTED | winner 击败 parent |
| REFUTED | loser 输给 parent |
| TIE | 平局，不更新 |

### 验收
- 少执行的高分 Hypothesis 优先探索（σ 大 → UCB 高）
- 已探索 Hypothesis 的相近变体被 proximity 压低
- 收敛后稳定选中最佳

---

## 5. Code Generation + AgentMonitor

### 职责
接收 Hypothesis → 隔离 worktree 生成代码 → Sandbox 运行 → 返回结果 + diff。

### Schema

```python
class CodegenResult(BaseModel):
    experiment_id: str
    commit: str
    diff: ArtifactRef
    eval: EvalResult
    logs: ArtifactRef
    wall_time_s: float
```

### 路由

```python
class CodeRouter:
    def route(self, hypothesis: Hypothesis, parent_codebase: ArtifactRef) -> str:
        if hypothesis.intervention == "from_scratch":
            return "codex"
        return "qoder"

class CodeAgent:
    def __init__(self, backend: Literal["codex", "qoder", "auto"] = "auto"):
        self.backend = backend          # "auto" 走路由，"codex"/"qoder" 强制
```

### 执行流程

```
parent_commit → GitWorkspace.prepare() → 隔离 worktree + 分支
    → Qoder/Codex 修改代码
    → Supervisor 审查 diff（是否修改 eval.py/数据切分，是否越界）
    → Sandbox 运行（训练 + eval.py）
    → Evaluator.evaluate() → EvalResult
    → git commit + diff 写入 ArtifactStore
    → release_workspace()
```

### Supervisor 审查规则

| 检查项 | 结果 |
|--------|------|
| 修改了 eval.py 或数据切分 | rejected |
| 修改超出 hypothesis.intervention 声明范围 | revise |
| 引入未声明外部依赖 | revise |
| 以上均否 | approved |

### AgentMonitor

```python
class AgentMonitor:
    async def watch(self, task: AgentTask, timeout_s: int = 600) -> AgentResult:
        # 1. 超时 → interrupt + TIMEOUT
        # 2. 连续 10 步无文件变更 → warning（仅日志）
        # 3. Sandbox OOM/崩溃 → 捕获 + FAIL
```

不监控 token 消耗、LLM 调用链、性能 tracing。

### 验收
- 代码修改不越界（不碰 eval.py/数据切分）
- 超时正确中断并返回 TIMEOUT
- auto 路由正确分派 codex vs qoder
- 审查拒绝后 Agent 可 revise 重新提交

---

## 6. SEARCH：Supervisor 主导的事件化协作

### 职责
串联模块 1-5，推进实验循环。Supervisor 独占状态转换和终止决策权。

### 角色

```
Scheduler   — 事件投递、任务队列、超时
Supervisor  — 状态转换、预算、终止、接受/拒绝（唯一决策者）
Agent       — 消费任务、提交 AgentResult
ResearchTree — 持久化状态机
```

事件是通信机制，不是决策机制。

### 主循环

```python
class SearchLoop:
    def __init__(self, budget: BudgetSnapshot, mode: RunMode): ...

    async def run(self) -> list[ExpCkpt]:
        while not self._budget.is_exhausted:
            # 1. 补充 Hypothesis（待处理不足 2 个时触发）
            if len(self._tree.pending_hypotheses()) < 2:
                await self._dispatch("ResearchAgent", task)

            # 2. 选择（模块 4）
            h = self._ranker.select(self._tree.pending_hypotheses(), self._proximity)

            # 3. 执行（模块 5）
            result = await self._dispatch("CodeAgent", task_from(h))

            # 4. 评估 + 比较（模块 1）
            verdict = self._evaluator.compare(baseline.result, result.eval)

            # 5. Supervisor 决策
            decision = self._supervisor.decide(verdict, self._budget)

            # 6. 更新状态
            self._tree.checkpoint(h, result, decision)
            self._budget.consume()
            self._ranker.update(h, result.eval.primary)

            # 7. HiL 暂停
            if self._mode.hil:
                await self._wait_human()
```

### Supervisor 决策

```python
class Supervisor:
    def decide(self, verdict: ComparisonVerdict, budget: BudgetSnapshot) -> "Decision":
        # SUPPORTED + 预算有余 → ACCEPT（推进 SOTA）
        # SUPPORTED + 预算耗尽 → STOP
        # REFUTED + no_improve_streak < max → REJECT（继续）
        # REFUTED + no_improve_streak >= max → STOP
        # TIE → REJECT（不计为提升）
```

### 并发模型

同时只跑一个 CodeAgent 实验（单 GPU/CPU 约束）。ResearchAgent 可在实验运行时并行检索生成新 Hypothesis。

### 验收
- 预算耗尽正确终止
- 连续 K 次无提升正确终止（`no_improve_streak >= max_no_improve`）
- HiL 暂停后人工确认可继续
- ResearchTree 每步 state_version 单调递增
- 单次实验 FAIL 不终止循环，消耗一次预算继续

---

## 7. VALIDATE + REPORT

### 职责
对 SOTA 做消融 + 最终测试 + 生成报告。代码已不可变保存在 ResearchTree 的 Git 中，无需复现。

### VALIDATE

```python
class Validator:
    async def ablate(self, sota: ExpCkpt, tree: ResearchTree) -> list[tuple[Hypothesis, EvalResult]]:
        """对 SOTA 路径上的每个 Hypothesis，checkout 其 parent commit 重跑，衡量该 intervention 的贡献。"""

    async def final_test(self, sota: ExpCkpt) -> EvalResult:
        """在 test split 上评估最终模型。只跑一次。"""
```

### REPORT

```python
class Reporter:
    async def generate(
        self,
        sota: ExpCkpt,
        tree: ResearchTree,
        ablation: list[tuple[Hypothesis, EvalResult]],
        final: EvalResult,
    ) -> ArtifactRef:
        """生成 Markdown 报告，包含：
        1. 问题定义和数据集摘要
        2. SOTA Hypothesis 链
        3. 每个实验的 metric 变化 + p-value
        4. 消融结果（每个 intervention 的独立贡献）
        5. 最终测试集结果
        """
```

### 验收
- 消融正确识别每个 intervention 的独立贡献
- 测试集在 SEARCH 阶段从未被 CodeAgent 接触
- 报告包含完整证据链（Hypothesis → experiment → metric → p-value）

---

## 8. 横切一致性收敛

### 8.1 全局 ID 追踪

| ID | 产生于 | 消费于 |
|----|--------|--------|
| `RunId` | Scheduler | 所有模块 |
| `HypothesisId` | 模块 3 | 模块 4, 5, 6, 7 |
| `ExperimentId` | 模块 5 | 模块 1, 4, 6, 7 |
| `ArtifactRef` | 所有产出 | 所有消费 |

### 8.2 阶段数据流

```
PREPARE                       SEARCH                        VALIDATE          REPORT
TaskMetaData ──► EvalSpec ──► FrozenEvaluator ──────────────────────────► Validator ──► Reporter
                    (冻结)         │                                          │
DataProfile ──► HypothesisGen ──► Ranker ──► CodeAgent                       │
ProcessingLog        │                │           │                           │
(splits)             ▼                ▼           ▼                           │
               ResearchTree ◄────── ExpCkpt + EvalResult ─────────────────────┘
```

约束：
- `EvalSpec` 冻结后 SEARCH 中不可修改
- `test split` 在整个 SEARCH 阶段对 CodeAgent 不可见
- REPORT 只读，不产生新实验

### 8.3 HiL / Debug 行为

| 模式 | PREPARE | SEARCH | VALIDATE |
|------|---------|--------|----------|
| `hil=true` | 每次清洗操作后暂停 | 每次实验完成后暂停 | 消融完成后暂停 |
| `debug=true` | 每次 LLM 交互后暂停 | 每次 LLM 交互后暂停 | 每次 LLM 交互后暂停 |
| 均为 false | 全自动 | 全自动（预算控制） | 全自动 |

### 8.4 错误传播

```
模块内部 retryable → 自动重试（最多 3 次）
模块内部 degrade → 记录 + 降级继续
模块内部 fatal → Supervisor 接收 → Phase 关闭 → 已产生结果保留
```

---

## A. Kaggle MCP 集成

```python
class KaggleClient:
    """最简封装：全局设置比赛 + 拉取数据。"""

    def set_competition(self, competition_id: str) -> None: ...
    def get_competition_info(self) -> dict: ...
    def download_dataset(self) -> ArtifactRef: ...
```

使用 Kaggle 官方 MCP (`https://www.kaggle.com/docs/mcp`)，只启用 `get_competition`、`download_dataset` 等必要工具，通过 Model Inspector 筛选。

## B. HuggingFace 集成

```python
class HFTools:
    async def search_models(self, query: str, n: int = 5) -> list[HFModelRef]: ...
    async def download_model(self, repo: str, revision: str) -> ArtifactRef: ...
```

约束：
- 必须记录 repo、revision、license、digest
- `trust_remote_code=False`
- 下载后缓存为本地 artifact，固定 revision
- Tabular 任务不默认推荐 Transformer

## C. 单轮对话工具

```python
async def single_turn(prompt: str, model: str = "haiku") -> str: ...
```

不保存历史，用于内部非 Agent 场景（如文本摘要、字段名推断）。
