# AutoResearch 论文图与可信实验 / 消融设计

日期：2026-08-15
状态：设计文档（待实现）
上游：`2026-08-15-autoresearch-ts-plugin-design.md`、`2026-08-15-autoresearch-detailed-design.md`

---

## 0. 2026-08-20 质量闭环集成

本文的可信实验、消融、图表来源和 EDA 隔离规则成为 ML 动态 rubric 的领域规则
来源，而不是新的控制平面 Service 列表。Rubric Generator 根据任务选取适用规则，
Rubric Reviewer 检查它们是否可执行且未被弱化；实验前冻结后，Athena adapter 和
写作 handoff 共同执行。

图生成沿用已验证的降级顺序：draw.io MCP → HTML/SVG → native SVG。数据图、
消融表和论文数字仍必须从 evidence 确定性生成，不因控制平面精简而放宽。

## 1. 本文目的

补齐 AutoResearch 中两块需要严谨性的设计：

1. **论文图流水线**：论文架构图 / 方法图由 LLM 设计，产出 **SVG** 源文件，再渲染为 LaTeX 可用的 PDF/PNG。
2. **可信实验设计与消融设计**：多数据集场景下，实验、消融、统计比较都必须有明确协议；EDA 阶段图不进入论文。

## 2. 已发现的 SVG 图 Skill（供实现期绑定）

> 本节保留 2026-08-15 的候选发现记录，不决定当前后端顺序。ML v1 的运行顺序
> 以 §0 的 draw.io MCP → HTML/SVG → native SVG 为准；绑定任何社区 skill 前仍需
> 单独确认 license、输入输出和本地可用性。

通过 skills-browser 与 web 检索，推荐以下 skill 用于“LLM 生成 SVG 架构图”。实现期应打开对应仓库检查 `SKILL.md`、license 与脚本后再决定绑定哪一个。

| Skill | 来源 | 能力 | 信任 | 建议用途 |
|---|---|---|---|---|
| [fireworks-tech-graph](https://github.com/ninehills/fireworks-tech-graph) | GitHub 社区 | Claude Code skill，自然语言 → 生产级 SVG+PNG 技术图；8 类图、5 种视觉风格，深 AI/Agent 领域知识 | medium | 早期 SVG 候选；仅在当前降级链需要额外 skill 时评估 |
| [drawio-skill](https://github.com/Agents365-ai/drawio-skill) | GitHub 社区 | draw.io 图，11 类预设，36 工具，导出 PNG/SVG/PDF/JPG，视觉自检 | medium | 备选：复杂可编辑架构图 |
| [ink-graph](https://github.com/qaz1230sp/ink-graph) | GitHub 社区 + npm | 自然语言 → 动画 SVG 技术图，11 主题，14 图类，zero runtime | low-medium | 备选：需要动画 SVG 的演示场景 |

设计原则：AutoResearch 只依赖 **SVG 源文件 + 渲染产物** 的目录契约，不依赖某个 skill 的内部实现；skill 通过 DSH skill 机制作为 Paper Figure Agent 的工具/提示词加载。

## 3. 论文图流水线

> 可直接使用的 FigureDesigner prompt 文本样例：`examples/articles_tests/2026-08-15-autoresearch-figure-prompt-examples.md`。

```
论文图需求
  ├── 架构/方法/流程类示意图  → FigureDesigner（LLM + SVG skill）→ figures/*.svg
  │        │
  │        ├── 自检（SVG 语法、尺寸、可读性）
  │        └── SvgRenderer（resvg-js / inkscape / rsvg-convert）→ figures/*.pdf / *.png
  │
  └── 实验数据图（折线/柱状/表）→ EvidencePlotter（从 experiment_evidence.json 确定性生成）
           → figures/*.pdf / *.png（多数据集分面）
```

### 3.1 FigureDesigner 服务

```ts
interface FigureDesigner {
  design(input: FigureDesignInput): Promise<FigureDesignResult>
}

interface FigureDesignInput {
  run_id: string
  paper_dir: string
  kind: "architecture" | "method" | "flow" | "ablation_overview"
  context: {
    sota_path: string[]              // 论文主张的方法链
    evidence: PaperEvidenceBundle
    draft_section_refs: ArtifactRef[]  // 已写好的方法/实验章节
  }
  constraints: FigureConstraints
}

interface FigureConstraints {
  format: "svg"                      // 论文图一律 SVG 源文件
  max_width_px: number               // 默认 1600
  max_height_px: number              // 默认 1200
  font_family: string                // 与论文模板字体一致
  palette: string[]                  // 色盲安全配色
  no_external_assets: true           // SVG 内联，无外部位图/字体
  labels_editable: true              // 所有文本为可编辑 text 节点
}

interface FigureDesignResult {
  svg_ref: ArtifactRef
  svg_path: string                   // figures/architecture.svg
  rendered_pdf_ref?: ArtifactRef     // LaTeX 用
  rendered_png_ref?: ArtifactRef     // Markdown 用
  self_check: {
    syntax_ok: boolean
    viewbox_ok: boolean
    text_legible: boolean
    palette_ok: boolean
    issues: string[]
  }
}
```

### 3.2 FigureDesigner 内部流程

```
1. 读取 draft section + evidence bundle
2. 调用 SVG skill（DSH skill / subagent prompt）生成 figures/<name>.svg
3. 语法自检：
   - XML/SVG 解析通过
   - 存在 viewBox / 或 width/height
   - 无外部 <image href="http...">
   - 文本节点可编辑
4. SvgRenderer 渲染：
   - Node: @resvg/resvg-js 输出 PNG（Markdown 内嵌）
   - 本地 inkscape --export-type=pdf 输出 PDF（LaTeX 用，若可用）
   - 无 inkscape 时：LaTeX 使用 SVG 原文件（需 svg 包支持），Markdown 使用 SVG 原文件
5. 返回 FigureDesignResult
```

### 3.3 论文 LaTeX 中的 SVG 使用策略

| 环境 | 策略 |
|---|---|
| Overleaf / 本机 TeX 且 `svg` 包可用 | `\includesvg[width=\linewidth]{figures/architecture.svg}` |
| 本机有 `inkscape` | 预渲染 `architecture.pdf`，LaTeX `\includegraphics{figures/architecture.pdf}` |
| Markdown draft | 直接引用 `figures/architecture.svg`（GitHub/多数 Markdown 可显示） |
| 降级 | 渲染 PNG 插入 Markdown；LaTeX 侧保留 SVG 源并记录编译日志 |

### 3.4 实验数据图

- 所有实验数据图由 `EvidencePlotter` 从 `experiment_evidence.json` **确定性生成**（非 LLM 生成），保证数字与表格一致。
- 多数据集分面：每个数据集一个子图，统一色盲安全配色，主指标 + 误差线/方差。
- 图表标题、轴标签由模板固定，不允许 LLM 自由写数字。

## 4. EDA 图与论文图的边界（多数据集）

**规则：EDA 阶段产生的图不进入论文。**

原因：AutoResearch 面向多数据集；EDA 图是内部诊断，分布/缺失/相关性等探索性分析一旦进入论文，容易造成数据泄漏观感、审稿质疑与视觉不一致。

| 图类型 | 来源 | 是否进论文 |
|---|---|---|
| EDA 报告图（分布、相关性、缺失值） | PREPARE Data/Prepare Agent | 否，只留在 EDA worktree |
| 动态 EDA 补充图 | Data Agent 追加 `figures/` | 否，只用于 Ideator 证据 |
| 论文架构/方法/流程图 | FigureDesigner（LLM + SVG skill） | 是 |
| 实验主结果图 | EvidencePlotter（确定性） | 是 |
| 消融表/图 | AblationCompiler（确定性） | 是 |
| SOTA 对比表 | ExperimentEvidence | 是 |

实现约束：

- `PaperComposer` 可读取 EDA 目录作为背景，但不得把 EDA `figures/` 复制进 `paper/<run_id>/figures/`。
- `PaperReviewer` 的 `template_compliant` 增加检查：论文 figure 目录中不允许出现 EDA 工作区路径或 EDA 图 hash。
- `experiment_evidence.json` 中的 `figure_refs` 只能是两类来源：`FigureDesigner` 或 `EvidencePlotter`。

## 5. 可信实验设计

### 5.1 `ExperimentDesign` 契约

每个假设进入 `HypothesisPool` 为 `QUEUED` 前，Ideation 阶段必须附带（或由 AutoResearch 自动补全）一个 `ExperimentDesign`：

```ts
interface ExperimentDesign {
  hypothesis_id: string
  independent_variable: string          // 只改变一个核心变量
  controlled_variables: string[]        // 必须保持不变的所有变量
  datasets: DatasetSplit[]              // 多数据集，每个数据集独立 split
  seeds: number[]                       // 重复实验的随机种子；默认 [0, 1, 2]
  metric: string                        // 主指标，与 evaluator 冻结一致
  direction: "maximize" | "minimize"
  tolerance: number                     // 平局阈值，默认 0.0
  statistical_test: StatisticalTest     // 比较方式
  ablation: AblationDesign
}

interface DatasetSplit {
  dataset_id: string
  train_split: string                   // 冻结 split 标识
  valid_split: string
  test_split: string
  test_visible: false                   // 可信实验：test 只在 VALIDATE 暴露
}

interface StatisticalTest {
  method: "paired_bootstrap" | "win_draw_loss" | "none"
  n_bootstrap: number                   // paired_bootstrap 默认 1000
  alpha: number                         // 默认 0.05
  tie_rel_tol: number                   // 1e-9
  tie_abs_tol: number                   // 1e-12
}
```

### 5.2 可信实验规则

1. **单一变量**：每次实验只允许 `independent_variable` 变化；`controlled_variables` 与 baseline 完全一致。
2. **冻结 evaluator / split**：evaluator 与 train/valid/test split 由 PREPARE 冻结；SEARCH 期间任何 agent 不可修改。
3. **多数据集**：每个数据集独立训练/评估；主结论要求在所有数据集上报告，不允许只挑好的数据集。
4. **种子**：默认 3 个种子，报告均值 ± 标准差；`experiment_evidence.json` 必须含逐种子原始结果。
5. **比较**：WIN/DRAW/LOSS 使用 `math.isclose` 平局规则（`rel_tol=1e-9, abs_tol=1e-12`）叠加 `tolerance` 作为可选 `min_effect`；若配置 `paired_bootstrap`，额外报告 p 值与置信区间。
6. **无 test 泄漏**：SEARCH 只能用 valid；test 仅在 VALIDATE 对最终 SOTA 暴露一次。

### 5.3 实验证据记录

每次结算写入 `ExperimentEvidence`：

```ts
{
  experiment_id: string
  hypothesis_id: string
  dataset_id: string
  split: "valid" | "test"
  metric: string
  direction: "maximize" | "minimize"
  values: number[]                       // 逐种子
  mean: number
  std: number
  reference_mean: number
  delta: number
  outcome: "WIN" | "DRAW" | "LOSS" | "INCONCLUSIVE"
  artifacts: { predictions_ref: ArtifactRef; evidence_ref: ArtifactRef }
}
```

## 6. 消融设计

### 6.1 消融类型

| 类型 | 定义 | 生成方式 |
|---|---|---|
| `remove_component` | 去掉方法 A 的某组件（如去掉 attention 分支） | Ideation 生成 + 门禁校验 |
| `replace_component` | 用 baseline 对应组件替换（如 Transformer → LSTM） | Ideation 生成 + 门禁校验 |
| `hyperparameter_sweep` | 关键超参数在小范围网格/随机搜索 | ExperimentDesign 预定义 |
| `data_ablation` | 减少训练数据比例/去掉某数据集 | ExperimentDesign 预定义 |
| `seed_stability` | 同配置多种子 | 每个实验默认必做 |

### 6.2 消融矩阵

`AblationCompiler` 从 `HypothesisPool` 与 `ExperimentEvidence` 确定性生成消融表：

```
| Variant | Dataset A (valid) | Dataset B (valid) | Δ vs SOTA | outcome |
|---------|-------------------|-------------------|-----------|---------|
| full    | 0.86 ± 0.01       | 0.81 ± 0.01       | +0.06     | WIN     |
| -comp1  | 0.83 ± 0.01       | 0.79 ± 0.02       | +0.03     | DRAW    |
| -comp2  | 0.80 ± 0.02       | 0.77 ± 0.01       | +0.00     | DRAW    |
```

规则：

- 每个被论文主张为贡献的组件必须至少有一行 `remove_component` 消融。
- 每个数据集都必须出现，不允许缺列。
- 表中数字只能来自 `ExperimentEvidence`，不允许 LLM 编造。
- 消融表写入 `tables/ablation.tex` 与 `paper_draft.md`。

### 6.3 消融设计图

- 若需要“组件关系图”，由 `FigureDesigner` 生成 SVG（`kind="ablation_overview"`），只展示组件关系，不展示数字。
- 数字一律在表格中，避免图/表不一致。

## 7. 全流程交互（含新组件）

```
IDEATION
  Ideator → ExperimentDesign + Hypothesis → HypothesisPool(QUEUED)

EXPERIMENT（Athena 子集）
  PREPARE → frozen evaluator + frozen splits + EDA worktree（EDA 图内部使用）
  SEARCH → 每个假设按 ExperimentDesign 执行，写 ExperimentEvidence
  VALIDATE → 最终 SOTA 一次 test

WRITING
  PaperComposer → sections/*.tex + paper_draft.md
  FigureDesigner → figures/architecture.svg（LLM + SVG skill）
  EvidencePlotter → figures/results_*.pdf/png（确定性）
  AblationCompiler → tables/ablation.tex

REFINEMENT
  PaperReviewer 新增检查：
    - figure_source_valid：论文图只来自 FigureDesigner / EvidencePlotter
    - eda_figures_absent：paper/figures/ 中无 EDA 图
    - ablation_complete：每个贡献组件都有消融行
    - experiment_design_ok：单一变量 / seeds / 多数据集完整
    - statistical_report_ok：比较方法、p 值、容差已写明

PACKAGING
  packaging/paper.pdf + paper_draft.md + experiment_evidence.json + SVG 源文件
```

## 8. 事件与状态增量

| 事件 | 触发 |
|---|---|
| `figure_design_completed` | FigureDesigner 产出 SVG + 渲染产物 |
| `figure_design_failed` | SVG 语法/自检不过 |
| `evidence_plot_completed` | EvidencePlotter 产出数据图 |
| `ablation_table_completed` | AblationCompiler 产出消融表 |
| `experiment_design_invalid` | 某假设 ExperimentDesign 不满足可信规则 |

## 9. 验收场景增量

### 场景 A：架构图生成

```
Given WRITING 阶段开始
When PaperComposer 需要 architecture 图
Then FigureDesigner 调用 SVG skill 生成 figures/architecture.svg
And 自检通过（语法/viewBox/可编辑文本/内联资源）
And 渲染产物 pdf/png 落盘
```

### 场景 B：EDA 图不进入论文

```
Given PREPARE 产生了 EDA figures
When WRITING 完成后检查 paper/figures/
Then 不存在 EDA 图文件
And PaperReviewer.figure_source_valid = 1
```

### 场景 C：消融完整

```
Given 论文 claims 组件 A 有效
When REFINEMENT 检查
Then ablation.tex 至少包含一行 remove_component(A)
And 所有数据集列都存在
```

### 场景 D：可信比较

```
Given 两个实验 primary 差值为 1e-13
When 结算比较
Then outcome = DRAW（数值平局）
And 若配置 paired_bootstrap，报告 p 值与置信区间
```

## 10. 实现顺序增量

1. `ExperimentDesign` / `ExperimentEvidence` schema 与校验（可信规则）。
2. `AblationCompiler`（纯函数，从 pool/evidence 生成表）。
3. `EvidencePlotter`（确定性数据图）。
4. `FigureDesigner` + SVG skill 集成 + `SvgRenderer`。
5. `PaperReviewer` 新增 figure/ablation/experiment-design rubric。
6. 端到端场景测试（场景 A–D）。

## 11. 开放项

- 最终绑定哪个 SVG skill（`fireworks-tech-graph` / `drawio-skill` / `ink-graph`）需实现期打开仓库确认 license 与输入输出。
- `paired_bootstrap` 的计算在 TS 端自研（无 scipy），需定义 bootstrap 实现与性能预算。
- 本机 SVG→PDF 渲染器选择：`inkscape` / `rsvg-convert` / `@resvg/resvg-js`，需按 Windows/Overleaf 环境验证。
- 色盲安全配色与模板字体是否随各会议模板变化。
