# feat/jw-fd-tess-task-readiness 简化合并方案

日期：2026-08-29
分支：`origin/feat-jw-fd-tess-task-readiness`
Worktree：`.worktrees/tess-task-readiness`
状态：仅方案，未合并

---

## 1. 简化原则

只合并会改变“分数正确性 / 数据泄漏 / 静默失败”的改动，延后纯体验、报告、provider、lock 等非关键项。

优先级：

- **P0 核心正确性**：必须合并，否则真实评测任务会得到看似合理但错误的结果。
- **P1 强烈建议**：防止常见静默损坏，成本低。
- **P2 可选/延后**：改善体验、可观测性、依赖可复现性，不影响当前 correctness。

---

## 2. P0：核心正确性

### 2.1 平台数据划分真正生效

**目的**：CLI 的 `--data/--target/--group-column` 等参数真正传进 runtime，而不是只写进提示词。

**文件**：

- `src/athena/cli.py`
- `src/athena/research/config.py`
- `src/athena/research/runtime.py`
- `src/athena/research/prepare_phase.py`

**机制**：

- `dataset_path` / `target_column` / `group_column` / `split_seed` / `tolerance` / `data_root` 传入 `ResearchRuntime`。
- 只有本地 CSV + 明确 target 才触发平台划分。
- 划分结果写入 `search_features.csv` / `final_features.csv` 等文件。
- 候选任务提示改为“只允许读官方 train split，预测 `ATHENA_PREDICT_FEATURES` 指向的行”。

### 2.2 分组防泄漏

**目的**：同一组数据（活性区、恒星、受试者）不能跨 train/search/final。

**文件**：

- `src/athena/research/splitter.py`

**机制**：

- `split_ids(..., groups=...)`。
- 按组整体洗牌，同组不拆。
- 按行数而不是组数切分，避免组大小不均时切错量。
- 生成 `split_manifest.json`，记录源文件哈希、参数、产物哈希。

### 2.3 数据契约进入候选 prompt

**目的**：候选 agent 看不到任务文本，必须通过 `content` 收到数据约束。

**文件**：

- `src/athena/research/supervisor/state.py`
- `src/athena/research/supervisor/supervisor.py`
- `src/athena/research/supervisor/experiment.py`
- `src/athena/research/prepare_phase.py`

**机制**：

- `ResearchState.data_contract` 持久化：
  - 训练只能使用哪份文件；
  - 必须读取 `ATHENA_PREDICT_FEATURES`；
  - 不得读取原始数据/其他 split。
- `data_contract_block()` 拼进每一轮 SEARCH 的 `content`。
- 不再依赖到不了模型面前的 `context_refs`。

### 2.4 VALIDATE 超时与换靶

**目的**：VALIDATE 复跑不能比 SEARCH 更早超时，且必须预测 final split，而不是重复 search split。

**文件**：

- `src/athena/research/supervisor/validation.py`
- `src/athena/research/phase_runner.py`
- `src/athena/execution/runtime.py`
- `src/athena/execution/backend.py`
- `src/athena/execution/remote/mirrored.py`
- `src/athena/execution/remote/ssh.py`

**机制**：

- `experiment_timeout_s` 传到 VALIDATE。
- `ATHENA_PREDICT_FEATURES` 在 VALIDATE 重跑期间指向 final features。
- 重跑结束后恢复原值。
- 校验预测文件是否覆盖最终标签行，否则报出具体原因。

### 2.5 修复 SEARCH 崩溃：解包错误

**目的**：真机上每次候选跑完就崩的 `not enough values to unpack`。

**文件**：

- `src/athena/research/supervisor/run_state.py`
- `src/athena/research/supervisor/search_loop.py`

**机制**：

- `running_tasks` 保留。
- 新增 `running_items` 返回 `(plan_id, task)`。
- search_loop 用 `running_items` 定位已完成 plan，避免把已完成 Task 迭代成空。

### 2.6 失败反馈回到 Agent

**目的**：失败轮不能让 Agent 以为“什么都没发生”。

**文件**：

- `src/athena/research/supervisor/plans.py`
- `src/athena/research/supervisor/supervisor.py`
- `src/athena/research/supervisor/experiment.py`
- `src/athena/research/supervisor/statistics.py`
- `src/athena/research/supervisor/plan_lifecycle.py`

**机制**：

- `PlanState.last_failure` 持久化，读取后清空。
- `failure_block()` 拼进下一轮 prompt。
- `PlanTurnResult.error/kind` 写入 `last_failure`。
- 统计上补充 uncorrected p-value；SOTA 指针若只凭点估计迁移而区间不显著，记录警告。

### 2.7 失败后可续跑

**文件**：

- `src/athena/research/runtime.py`

**机制**：

- `state.status == "FAILED"` 在 runtime 构造时降为 `IDLE`。
- 显式 `search_limit` 覆盖持久化旧值。

---

## 3. P1：强烈建议

### 3.1 单项目单进程锁

**文件**：

- `src/athena/core/project_lock.py`
- `src/athena/cli.py`

**机制**：

- OS 级独占锁，防止两个进程同时写 `state.json` / `research_tree.json`。
- 第二个进程立即退出并提示。
- 锁随进程结束释放。

### 3.2 EDA 异步 publish 与跳过已有报告

**文件**：

- `src/athena/research/eda_todo.py`

**机制**：

- `publish` 改为 async，修复首个事件丢失导致 EDA 假失败。
- 已存在且大小正常的报告直接跳过，不重跑。

### 3.3 evaluator 契约核心

**文件**：

- `src/athena/agents/prompts/evaluator_agent.md`
- `src/athena/research/supervisor/prepare.py`

**机制**：

- 强制按 `__athena_row_id` join。
- 禁止按位置、禁止截断、禁止 glob-concat 多个预测文件。
- `HANDOFF.md` 增加机器可读的 `prediction_column` / `prediction_id_column`。
- 必须输出 `test_se` / `test_n`。
- 缺失 `metric.json` 时报出目录内容与绝对路径。

---

## 4. P2：可选 / 延后

| 项目 | 文件 | 说明 |
|---|---|---|
| Qwen provider | `provider.py`, `settings.py`, `__init__.py`, `.env.example`, `config.example.toml` | 如不接阿里云百炼可延后 |
| 采样温度/种子 | `models.py`, `runtime.py`, `settings.py` | 外部评测要求可申报时才需要 |
| 最终报告增强 | `report.py` | 体验增强，不改变研究正确性 |
| `uv.lock` 提交 | `uv.lock`, `.gitignore` | 可复现性，不是 correctness |
| astro extra | `pyproject.toml` | 仅天文任务需要 |
| 大量新增测试 | `test/` | 随 P0/P1 对应合并，不单独全量引入 |

---

## 5. 实施顺序

1. P0 数据划分 + 分组 + 数据契约。
2. P0 VALIDATE 超时/换靶。
3. P0 SEARCH 崩溃修复。
4. P0 失败反馈 + 续跑。
5. P1 项目锁。
6. P1 EDA 修复。
7. P1 evaluator 契约 prompt。
8. P2 按需要选择。

---

## 6. 验收点

- 平台划分实际执行，且同一分组不跨 split。
- 候选 prompt 中能看到数据契约。
- VALIDATE 使用 final split，且有正确超时。
- SEARCH 不再因解包错误崩溃。
- 失败原因进入下一轮 Agent prompt。
- 两个进程不能同时跑同一项目。
- 关键测试通过：
  - splitter
  - validation
  - search_loop / running_items
  - prepare_phase
  - project_lock
  - evaluator prompt
