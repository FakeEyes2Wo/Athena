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

## 阻断项：main 缺少 `athena/research/exp_docs.py`

`origin/main` 的 `supervisor/phases.py:13` 与 `supervisor/settlement.py:10` 都 import
`athena.research.exp_docs` 的 `task_metric_name`、`write_reports`、`write_stage_doc`，
而这个模块**在本仓库的任何 ref 与任何历史提交里都不存在**——`git log --all
--diff-filter=A -- src/athena/research/exp_docs.py` 无结果。import 由合并提交
`ae922b9` 带入。

后果：任何 import 到 `athena.research.supervisor` 的测试模块都无法收集。在干净的
`origin/db2555f` 上执行 `pytest test/unit` 得到 **54 个收集错误**。

本分支不含该模块（它属于另一条未完成的功能，其契约无从推断，凭空补写有冲突风险）。
上面的验证数字是在本地临时补一个 import 桩后取得的，桩没有提交。该模块补齐前，
`main` 与本分支都跑不完整个测试套件。
