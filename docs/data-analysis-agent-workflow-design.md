# Athena DataAnalysis Agent 工作流设计

> 状态：详细设计，待用户审阅
> 日期：2026-08-08
> 上位设计：[dynamic-agent-orchestration-design.md](dynamic-agent-orchestration-design.md)
> 编排设计：[supervisor-agent-design.md](supervisor-agent-design.md)
> 范围：DataAgent、PlotAgent、ReflectionAgent、DataAnalysis Bundle、rubric 与修订版本链

## 1. 目标

DataAnalysis 阶段输出可追溯的数据分析报告，而不是把大量 EDA 文本塞进 Agent 返回对象。正式产物是一个不可变目录 Bundle，至少包含 `report.md` 和一张图片；ReflectionAgent 只评价，只有原 DataAgent 实例能提交修订版本。

## 2. 当前代码判断

当前实现存在三条需要收敛的路径：

1. [experiment/pipeline.py](../src/athena/experiment/pipeline.py) 通过 `_data_agent.run()` 和 `_plot_agent.run()` 直接串联对象，没有共享 Kernel 身份和 follow-up。
2. [workflows/prepare/data_analysis.py](../src/athena/workflows/prepare/data_analysis.py) 返回 `artifact://本地路径`，绕过 SHA-256 ArtifactStore。
3. PREPARE 主要输出 `DataProfile` 和 `ProcessingLog`，尚未形成包含报告、图片和版本 lineage 的 DataAnalysis Artifact。

目标实现继续复用确定性数据操作和 DataProfile，但把 Agent 编排与正式报告迁入统一 AgentKernel。

## 3. Agent 职责

### 3.1 DataAgent

DataAgent 是 DataAnalysis 的唯一作者和版本提交者：

- 检查数据、采样、EDA 和清洗效果；
- 使用数据工具读取摘要，不把完整数据集放入上下文；
- 编写 `report.md`；
- 按需启动 PlotAgent；
- 选择并引用图片；
- 提交 DataAnalysis v1；
- 接收评审后由同一实例提交 v2、v3。

DataAgent 不制定评审分数，也不能绕过 EvaluationPolicy。

### 3.2 PlotAgent

PlotAgent 是通用绘图能力，不属于 DataAnalysis 专用流程。任何需要图片的 Agent 都可以按 `agent_type="plot"` 创建它。

PlotAgent 输入：

- 一段明确的绘图目标；
- 数据或统计 ArtifactRef；
- 可选的风格与尺寸要求。

PlotAgent 输出：

- 图片 ArtifactRef；
- 在消息 `content` 中返回图注和观察；
- 可选的绘图代码或数据 ArtifactRef。

PlotAgent 不提交 DataAnalysis、不修改调用方报告、不拥有报告版本。

### 3.3 ReflectionAgent

ReflectionAgent 是只读评审者：

- 根据任务目标和当前评估协议生成或追加 rubric；
- 按指定 rubric 版本逐项评分；
- 为每项分数提供证据引用和修改意见；
- 输出 rubric/review Artifact；
- 不能修改 `report.md`；
- 不能提交新的 DataAnalysis 版本。

评审完成后由 Supervisor 把结果 follow-up 给原 DataAgent。

### 3.4 EvaluationPolicy

EvaluationPolicy 是普通确定性服务，不是 Agent。它只读取结构化 rubric 与 score：

- 检查必选项是否达到阈值；
- 计算约定的总分条件；
- 返回 `passed` 或 `failed`。

它不编写标准、不解释报告、不选择下一 Agent。

## 4. DataAnalysis Bundle

### 4.1 逻辑目录

正式版本表现为：

```text
DataAnalysis/
  report.md
  figures/
    <one-or-more-images>
```

可选的分析表格、脚本和统计摘要可以放入其他子目录，但首版结构有效性只要求：

- 根级恰有一个 `report.md`；
- `figures/` 下至少一张图片；
- report 中每个本地图片引用都能解析到同一 Bundle；
- 不允许绝对路径和 `..` 越界引用。

### 4.2 内容寻址 Manifest

文件分别写入 ArtifactStore，最后写入规范化 Manifest：

```json
{
  "schema_version": 1,
  "kind": "directory",
  "files": {
    "report.md": "sha256:...",
    "figures/missing-values.png": "sha256:..."
  },
  "parent_ref": null
}
```

Manifest 自身也写入 ArtifactStore，其 ArtifactRef 就是 DataAnalysis 版本引用。v2 的 `parent_ref` 指向 v1 Manifest ref。

Manifest 是存储层内部 schema，不增加 `DataAnalysisManifest` 公共 DTO。Agent 只传最终 ArtifactRef。

### 4.3 原子提交

提交顺序：

1. 写入 report、图片和可选文件，得到内容引用；
2. 构建并校验规范 Manifest；
3. 写入不可变 Manifest；
4. 原子更新 StateStore 的 `latest_ref`。

Manifest 写入前的文件只是暂存 Artifact。任何一步失败都不能移动 `latest_ref`，上一正式版本继续有效。

## 5. 版本所有权

首次提交时，StateStore 创建内部记录：

```text
analysis_id
owner_agent_id
latest_ref
```

后续提交必须满足：

- 调用者 `agent_id == owner_agent_id`；
- `parent_ref == latest_ref`；
- 新 Bundle 结构有效；
- Manifest 写入成功。

因此 ReflectionAgent、PlotAgent、SupervisorAgent 和另一个 DataAgent 实例都不能提交 v2。即使同为 `agent_type="data"`，不同 agent_id 也不是原作者。

## 6. v1 生成流

```text
Supervisor spawn DataAgent
  -> DataAgent 使用数据工具获取样本和统计
  -> DataAgent 编写 report 草稿和绘图目标
  -> DataAgent 按需 spawn 一个或多个 PlotAgent
  -> DataAgent wait_for(plot_agent_ids)
  -> Plot completion 唤醒原 DataAgent
  -> DataAgent 选择图片、补充图注与结论
  -> DataAnalysisStore 提交 v1
  -> result_ref 返回 v1 Manifest ref
```

如果 PlotAgent 失败，DataAgent 根据真实 completion 决定重新绘图或调整方案。未满足至少一张有效图片前，不能提交正式版本。

## 7. Reflection 流

```text
Supervisor spawn ReflectionAgent
  context_refs = [task_ref, protocol_ref, data_analysis_v1_ref]

ReflectionAgent
  -> 生成或追加 rubric
  -> 记录 rubric_version
  -> 逐项读取报告证据
  -> 生成 score 和 review
  -> EvaluationPolicy 计算 passed/failed
  -> completion 返回 Supervisor
```

Reflection 输出是独立评审 Bundle：

```text
DataAnalysisReview/
  rubric.json
  rubric.md
  score.json
  review.md
```

- JSON 文件供 EvaluationPolicy 确定性读取。
- Markdown 文件供 DataAgent 和用户阅读。
- AgentMessage 只携带该 Bundle 的 ArtifactRef，不展开全部评审内容。

## 8. Rubric 版本

Rubric 条目至少包含：

```text
criterion_id
description
score_min
score_max
pass_threshold
required
```

Rubric 版本还包含：

```text
rubric_version
parent_ref
criteria
overall_pass_threshold
```

规则：

- rubric 采用 append-only 版本链：允许追加新条目，但不能删除或改写已有条目；
- 已有 `criterion_id` 的定义与顺序保持不变；
- 每次 append 产生新版本并保留 parent_ref；
- score 必须绑定精确 rubric_version；
- 不同 rubric 版本的总分不能直接比较。

为降低读取复杂度，每个 rubric 版本保存完整 criteria 快照，而不是只保存 delta。新版本的已有条目必须与父版本一致，只允许在列表尾部追加新 criterion_id。

## 9. 按需重评

Rubric append 后不自动重评历史 DataAnalysis。只有 Supervisor 确实需要比较指定版本时，才让 ReflectionAgent 使用同一 rubric 版本重新评分：

```text
compare(v1, v2)
  -> score v1 with rubric r2
  -> score v2 with rubric r2
  -> EvaluationPolicy / comparison reads same rubric_version
```

旧评分保留，不被静默覆盖。

## 10. 修订流

```text
EvaluationPolicy = failed
  -> Supervisor followup(original_data_agent_id)
     context_refs = [v1_ref, rubric_ref, review_ref]
  -> 原 DataAgent 恢复私有记忆
  -> 修改 report、复用或新增图片
  -> 提交 v2，parent_ref = v1
  -> 再次 Reflection
```

DataAgent 可以直接重写工作区中的当前 report 草稿，但正式提交会生成新 Manifest；旧版本始终保留。

`failed` 不强制 Supervisor 无限返工。Supervisor 可以根据预算终止、请求用户或保留当前结果，但不能把它标记为 `passed`。

## 11. 与确定性 PREPARE 的关系

现有 `prepare_workflow_data()` 继续负责：

- 原始数据副本；
- 确定性切分；
- 基础缺失值处理；
- DataProfile；
- ProcessingLog；
- 可信验证输入。

DataAgent 消费这些 Artifact 和摘要，负责解释、扩展 EDA 和形成报告。Agent 不直接改写 host-owned test labels 或评估输入。

`EvalSpec` 的单个快照可以保持 Pydantic immutable；评估协议整体通过 parent_ref 形成只追加版本链。DataAnalysis rubric 与模型预测指标属于不同 Artifact 类型，不能混为同一个 schema。

## 12. 失败反馈

首版不设计 Plot、Data、Reflection 各自的错误码。所有实际运行失败统一形成 RunSummary 并返回 Supervisor。

只保留影响正式状态的检查：

- DataAnalysis 所有权；
- parent_ref 必须指向 latest；
- Manifest 原子提交；
- report 和至少一张图片可解析；
- EvaluationPolicy 使用一致 rubric_version。

其他检查根据真实端到端运行结果补充。

## 13. 最小验证

- PlotAgent 可被不同类型 Agent 创建，且不能提交 DataAnalysis；
- DataAgent 等待 PlotAgent 时释放执行槽；
- v1 包含 report 和图片；
- Reflection 不修改 report；
- failed 后 follow-up 原 DataAgent 产生 v2；
- v2 owner 与 v1 相同，parent_ref 正确；
- rubric append 不删除旧条目，也不自动重评；
- 同 rubric_version 的评分可被 EvaluationPolicy 确定性处理；
- Manifest 失败时 latest_ref 保持上一版本。
