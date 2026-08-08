# Athena DataAnalysis Agent 工作流设计

> 状态：一次性迁移专题设计
> 日期：2026-08-08
> 上位设计：[dynamic-agent-orchestration-design.md](dynamic-agent-orchestration-design.md)
> 完整流程：[agent-end-to-end-workflow-design.md](agent-end-to-end-workflow-design.md)

## 1. 目标

DataAnalysis 是不可变目录 Bundle，不是返回对象。DataAgent 是版本唯一作者；PlotAgent 生成图片；ReflectionAgent 生成 rubric 和 review；EvaluationPolicy 只做确定性通过判断。

## 2. 角色边界

### DataAgent

- 读取 training-only 数据、profile 和采样引用；
- 完成 EDA、解释和 `report.md`；
- 按需 spawn PlotAgent 并等待必需图片；
- 提交 v1；
- 接收 review 后由同一实例提交 v2+。

### PlotAgent

- 可被任何需要图片的 Agent 创建；
- 返回图片、图注、观察和可选绘图代码引用；
- 不修改报告，也不提交 DataAnalysis 版本。

### ReflectionAgent

- 先生成或选择 rubric 版本，再逐项评分；
- 每项分数必须有证据和修改意见；
- 只提交 Review Bundle，不修改 DataAnalysis。

### EvaluationPolicy

- 校验 required criterion 和总分阈值；
- 返回 passed/failed；
- 不编写 rubric，不解释报告，不选择下一 Agent。

## 3. 数据边界

split 在 DataAgent 启动前冻结：

```text
raw -> train / validation / final-test
       |
       -> training-only profile and EDA samples
```

DataAgent 只能读取 train、training-only profile 和不含封存标签的 schema。validation/final-test labels 只交给受信任 evaluator。

当 train 超过 EDA 上限时，SamplingService 使用三个不同且记录的 seed 生成等规模样本。DataAgent 必须区分跨样本稳定结论和样本敏感结论。样本只服务 EDA，不能进入训练、验证或 final-test。

## 4. DataAnalysis Bundle

```text
DataAnalysis/
  report.md
  figures/
    <one-or-more-images>
  tables/       # optional
  analysis/     # optional
```

正式版本至少满足：

- 根级一个 `report.md`；
- `figures/` 至少一张可解析图片；
- report 内本地图片引用不越界；
- 不包含原始数据、validation/final-test labels 或绝对路径。

每个文件写入 ArtifactStore，目录 Manifest 记录路径到内容引用的映射。Manifest 本身的 ref 是版本 ref。

## 5. 版本所有权

内部状态只需：

```text
analysis_id
owner_agent_id
latest_ref
```

提交 v2+ 必须同时满足：

- 调用者是原 `owner_agent_id`；
- `parent_ref == latest_ref`；
- 新 Bundle 结构有效；
- Manifest 成功写入后才原子移动 latest。

DataAgent 可以直接回写工作区中的当前 `report.md` 草稿，但正式提交总是创建新 Manifest，旧版本不能删除。

## 6. 评审与修订

```text
DataAgent -> PlotAgent -> DataAnalysis v1
Supervisor -> ReflectionAgent
  -> rubric -> score -> review
EvaluationPolicy
  -> passed: accept v1
  -> failed: followup original DataAgent -> v2
```

Review Bundle：

```text
DataAnalysisReview/
  rubric.json
  score.json
  review.md
```

JSON 供 Policy 读取，Markdown 供 Agent 和用户阅读。

## 7. Rubric 版本

Rubric 采用 append-only 版本链：

- 可以在尾部追加新 criterion；
- 不能删除、重排或改写旧 criterion；
- 每个版本保存完整快照和 parent_ref；
- score 绑定精确 rubric version；
- 不同版本的分数不能直接比较；需要比较时按同一版本重评。

追加 rubric 不自动重评历史版本，旧评分也不覆盖。

## 8. 当前切换要求

当前确定性骨架已经证明 owner/latest、目录 Bundle 和 v1/v2 流程，但尚未使用真实数据、三个 EDA 样本、真实绘图和模型 rubric。一次性切换需要把这些能力接入现有边界，而不是增加第二套 DataAnalysis schema。

## 9. 验收

- DataAgent 只读 training 边界；
- 大数据产生恰好三个可复现 EDA 样本；
- DataAgent 通过 Kernel spawn/wait PlotAgent；
- v1 含 report 和有效图片；
- Reflection 先写 rubric 再评分，且不修改 report；
- failed 后 follow-up 原 DataAgent 产生 v2；
- 另一 DataAgent、Plot、Reflection 或 Supervisor 不能提交 v2；
- rubric append 不删除旧条目；
- 提交失败时 latest 保持上一版本。
