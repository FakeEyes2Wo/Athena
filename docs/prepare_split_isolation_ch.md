# PREPARE 的切分隔离：GUI 路径上平台切分不启用，两份 held-out 重叠

Status: current
Owner: Athena maintainers
Last verified: 2026-08-29
Source of truth: `src/athena/research/prepare_phase.py`,
`src/athena/research/splitter.py`, `src/athena/gui/service.py`,
`src/gui_gateway/handler.py`

## 症状

GUI 里跑完一次 PREPARE，SEARCH evaluator 与 FINAL evaluator 的 held-out 标签
有相当比例重叠——SEARCH 在调参时能碰到最终测试集的一部分。CLI 用 `--data`
起的运行不受影响。

## 根因

平台级确定性切分是**有条件**启用的，而 GUI 永远不满足那个条件。

[`prepare_phase.py:150`](../src/athena/research/prepare_phase.py#L150)：

```python
# Optional platform-level data split: when the task names a local CSV,
# generate train/search/final files so the evaluator does not split itself.
if rt.config.dataset_path is not None and rt.config.target_column is not None:
    split_dir = rt.workspaces_root / "data_split"
    materialize_csv_split(...)
```

- CLI 有 `--data`，能把 `dataset_path` 填进 config，分支成立，
  [`splitter.py`](../src/athena/research/splitter.py) 的 `SplitManifest.validate()`
  强制 train/search/final 三者不相交。
- **GUI 从没有设置这两个字段**：`dataset_path` / `target_column` 在
  `src/athena/gui/service.py` 与 `src/gui_gateway/handler.py` 里零命中。用户在
  GUI 里发的是一段自由文本任务，没有数据集/目标列的入口。

分支不成立时，两个 evaluator agent 各自从原始数据里随机切一份 held-out。唯一的
约束是写在任务文本里的一句话（[`prepare_phase.py:240`](../src/athena/research/prepare_phase.py#L240)）：

```
Use a held-out split disjoint from the SEARCH evaluator's split.
```

没有任何代码校验它是否真的做到了。`_validate_frozen_evaluator` 只检查结构性质
（labels.csv 的列、row id 非空、评估脚本能跑），不比对两份切分。

`splitter.py` 的模块 docstring 早就写明了这个状态：

> The current research runtime still relies on LLM-created evaluator splits;
> this module provides the deterministic primitive to replace that step.

## 证据（实测）

2026-08-29，GUI 会话 `s-1788023555139`（工作区 `D:\tmp\test`，Titanic，891 行）：

```
workspaces/evaluator/labels.csv        178 行
workspaces/final_evaluator/labels.csv  178 行
两者 __athena_row_id 交集               43 行  (24%)
workspaces/data_split/                 不存在   ← 平台切分未启用
```

43 正好是随机切分的期望重叠（178² / 891 ≈ 36），说明两个 agent 之间没有任何协调。

transcript 显示 FINAL evaluator **主动尝试过**满足要求，仍然没做到：

> I need to create a disjoint held-out split for the FINAL evaluator.
> I have the SEARCH evaluator's 178 PassengerIds.

## 复现

1. GUI 里新建会话，发一个带本地 CSV 的任务（不经 CLI `--data`）。
2. 等 PREPARE 跑过两个 evaluator。
3. 比对 `workspaces/evaluator/labels.csv` 与 `workspaces/final_evaluator/labels.csv`
   的第一列 id 集合——交集非空。
4. `workspaces/data_split/` 不存在，佐证平台切分没走。

## 修复方案（两条，可独立落地）

**A. 冻结校验（治标，改动小，立刻兑现 disjoint）**

`_validate_frozen_evaluator` 或 `run_evaluator_plan` 的冻结环节：FINAL evaluator
冻结时比对它与 SEARCH evaluator 的 labels row id，有交集就把重叠行数作为反馈
打回给 agent 重切。好处是不依赖数据从哪来，GUI / CLI / headless 全都覆盖。
需要把 SEARCH 的 labels 路径（或其 row id 集合）传进 FINAL 那一轮。

**B. 平台接管切分（治本，面大）**

给 GUI 补数据集入口（`dataset_path` / `target_column`），经 `start_search` 的
config 传到后端，让 `materialize_csv_split` 真正启用。这条要动前端表单、
`docs/athena-gui-design.md` 的 `start_search` 契约与 `GuiService`。做完之后
evaluator 不再自己切，`SplitManifest.validate()` 天然保证不相交。

两条不冲突：A 是兜底闸门，B 是把职责收回平台。建议先 A 后 B。

## 涉及文件

- `src/athena/research/prepare_phase.py`（条件分支与 FINAL 的任务文本）
- `src/athena/research/supervisor/prepare.py`（`_validate_frozen_evaluator` 冻结校验）
- `src/athena/research/splitter.py`（已有的确定性切分原语）
- 方案 B 另需：`src/athena/gui/service.py`、`athena-gui/`、`docs/athena-gui-design.md`

## 验收

- 单测：给定两份有交集的 labels.csv，冻结校验必须失败并报出重叠行数。
- 单测：不相交时冻结通过。
- 手测：GUI 跑一次 PREPARE，两份 labels.csv 的 id 集合交集为空。

## 附：本文结论的验证状态

| 结论 | 验证方式 |
|---|---|
| 两份 held-out 交集 43/178 | 实测（直接比对两份 labels.csv 的 `__athena_row_id`） |
| `data_split/` 不存在 | 实测（列会话 workspaces 目录） |
| GUI 从不设置 `dataset_path`/`target_column` | 代码检索（`gui/service.py`、`gui_gateway/handler.py` 零命中） |
| 无 disjoint 校验 | 代码检索（`disjoint` 在 src/ 仅出现在任务文本与 splitter 内部） |
