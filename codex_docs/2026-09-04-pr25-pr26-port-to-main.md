# 把 PR #25 / #26 的内容补进 main

## 背景

PR #25（`fix/validate-unfence-and-output-freshness`）以 `main` 为 base，至今 OPEN。
PR #26（`fix/validate-repair-loop-feedback`）已合并，但 base 是 PR #25 的分支而不是
`main`，squash 成 `ed54d89` 落在那条分支上。两条分支与 `main` 的 merge-base 仍是
`e3ad802`，`main` 上没有任何一次合并把它们带进来。

其间 `main` 走了 147 个提交，其中 `1085efe`（快照提交）独立重写了 PR #25 的产物形态
修复，并做了大规模目录重排：`research/phase_runner.py` → `research/runtime/phase_runner.py`，
`research/prepare_phase.py` → `research/prepare/`，`research/agent_turn_runner.py` →
`research/turns/`，另有 `research/evaluation/`、`research/literature/` 两层新分层。
因此两条分支无法直接合并，本分支逐项手工搬运。

## 已在 main 上的部分（不重复搬运）

| 内容 | 落在 main 的方式 |
|---|---|
| 声明产物的文件/目录形态（`REPORT.md` 被 mkdir 成目录） | `1085efe` 独立重写，命名不同（`was_directory` / `populated`） |
| `test_experiment.py`、`test_prepare_plan.py` 的 execution 替身 | `1085efe` 已改为 `CommandRequest` 签名 |
| 预测行覆盖检查（仅 VALIDATE） | `main` 自己演进得更强：按**首列**取身份而非写死 `__athena_row_id`，并检出重复与多余身份值 |
| fork 丢失 `evaluator_ref` | `b028b6c` 一行修复，写进 `state.json` |

## 本分支搬运的内容

| 来源 | 内容 |
|---|---|
| PR #25 `a3c1168` | 共享 `athena.core.fenced_json.unfence_json`；VALIDATE 独立评审解析前先剥围栏 |
| PR #25 `4bf9efd` | `archive_output_roots` 的路径逃逸报 `GitWorkspaceError`；文件型产物的四个用例 |
| PR #26 `278a2c2` | `ValidationRunFailed` / `PredictionsRejected` 两个具名异常；`_run_failure_feedback` 把失败正文回灌给 validate Agent；预算耗尽时点名最后一次失败；`DataContract.moving_target` |
| PR #26 `36e80a4` | `plan_agent.md`：候选继承的绝对路径属于父实验 |
| PR #26 `688b176` | 评审 diff 剔除声明产物；`declared_output_paths` 在 manifest 被改动时放弃剔除 |
| PR #26 `aa7bed2` | fork 携带 `resume.json` 里的 PREPARE 字段，并按目标自身的 digest 重写 |
| PR #26 `4ab1ce3` | 覆盖检查下沉到 `research/predictions_cover.py`，`PlanRunner.run_turn` 在打分前调用 |
| PR #26 `7b90bec` | SEARCH 前置条件：`run_search` 断言 + 点名缺哪一项；SOTA 拒绝语说明 PREPARE 才是baseline 的归属 |
| PR #26 `c7eb694` | 语义闸门只扫改动行、跳过产物文件、点名命中的 marker |
| 分支后续 `a9de3e8` | 结构化输出按 schema 有效性找回唯一 JSON；重试回传 schema；耗尽时落 raw response artifact；`ATHENA_DATA_CSV` 全链路注入；同一 diff 重复被拒即判 no progress |
| 分支后续 `ab36f94` | idea gate 复用 runtime client（原先每次 falsifiability/review 调用都新建默认 client，绕开配置的网关） |
| 分支后续 `7e6fdaa` | 嵌套在合法包装对象里的 schema 命中判为歧义 |

## 与分支原版的差异

三处按 `main` 的现状调整，均为保留 `main` 已有的更强实现：

1. **`predictions_cover.py`** 取 `main` 的实现（首列身份、重复与多余身份值检查），
   再并入 PR #26 的"部分重叠 = 把行号当行 id"提示与类型化的 `PredictionsCoverageError`。
   分支原版写死 `__athena_row_id`，会丢掉 `main` 已有的三项检查。
   `test_predictions_cover.py` 因此按合并后的语义重写。
2. **`core/agent/runtime.py`** 的 `_unfenced` 在 `main` 上已扩展为按位置猜正文里的 JSON。
   该启发式被 `_parse_structured_output` 的 schema-有效性挑选取代——按位置猜会在正文
   本身含花括号时挑错对象。`main` 原有的三条相关用例仍然通过。
3. **fork** 保留 `b028b6c` 写入 `state.json` 的 `evaluator_ref` 作为无 `resume.json`
   时的兜底，`_carry_resume_fields` 按分支原版原样搬运源文件的字段。

## 验证

`test/unit`：2458 passed（见下一节的前置条件），`test/integration/research/test_validate_agent_contract.py`：14 passed。
`test_experiment.py` 44 passed、`test_validation_plan.py` 46 passed，与两个 PR 声明的数字一致。

## 与 main 的合并（2026-09-05 复核）

`origin/main` 此后又推进了 88 个提交，本节记录对当时那版 main 的复核结果。

### 曾经的阻断项已经解除

写这份文档时，`supervisor/phases.py` 与 `supervisor/settlement.py` 都 import
`athena.research.exp_docs`，而该模块在任何 ref 与任何历史提交里都不存在（由合并提交
`ae922b9` 带入 import 却没带文件），导致 54 个测试模块无法收集，本分支的验证只能靠
一个本地 import 桩完成、桩未提交。

main 已用 `research/experiment_documents/`（models / projector / store）取代它，
`phases.py` 不再引用 `exp_docs`。**现在 `origin/main` 收集错误为 0**，本分支的验证
不再需要任何桩。

### 仍未解除的一项

`BaselineAuthorityStore` 仍然只是 `prepare/authority.py` 里的一个 Protocol，全仓库
没有生产实现（只有两个集成测试里的内存替身），`cli.py` 与 `athena_tui/entrypoint.py`
都不传它。因此 `prepare_baseline_design` 里那句

```python
if authority is None:
    raise BaselineAuthorityError("PREPARE requires an external baseline authority capability")
```

仍会在 PREPARE 第一步抛出——**基于检索的 baseline 门禁至今无法启用**。本分支不含该
实现：它属于另一条未完成的功能，契约无从推断。

### 试合并的结果

在一个临时 worktree 里把本分支并进 `origin/main` 并跑 `test/unit`（用 `PYTHONPATH`
指向该 worktree 的 `src`，否则可编辑安装会让测试跑到主仓库的源码上）：

| | 失败 | 通过 | 收集错误 |
|---|---|---|---|
| `origin/main` 自身 | 19 | 2598 | 0 |
| 合并后 | 20 | **2668** | 0 |

净增 70 个通过用例。多出的那一条 `test_predictions_api.py::test_post_to_wrong_route_is_404`
是不稳定用例：两棵树单独跑该文件都是 22 passed，而本分支从未触碰 `serving/`。

首次试合并曾出现 **2 条真实回归**，都在 main 新增的 skip-validate 续跑用例上。根因是
移植 PR26 的 SEARCH 前置门禁时把冻结评估器的判据映射到了 `state.evaluator_ref`，而
权威记录在 SOTA 实验上（`fork.py` 正是从基线记录里读的）；续跑时该字段为空，门禁把
合法续跑判成 `SEARCH/FAILED`。该判据同时是冗余的——有 SOTA 就必然有评估器。已改为
只查 SOTA（提交 `d9096c9`），这也正是 2026-09-02 那次事故真正需要的判据。

这个缺陷在旧 main 上无法暴露，因为那两条续跑测试是这 88 个提交里才有的。
