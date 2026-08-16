# Athena 断点续传修复方案（new_kaggle_test 复盘）

- 日期：2026-08-16
- 状态：**待确认（确认前不改任何代码）**
- 复盘对象：`C:\Users\80163\Desktop\挑战杯_2026\new_kaggle_test`
- 关联运行：2026-08-16 11:44 首轮 → 23:01 用户发 `continue`

---

## 1. 问题摘要

首轮运行在 11:44 开始任务理解，11:48–12:16 由 general worker 完成 Kaggriculture 调研并产出
`kaggriculture_summary.md` 等文件；随后首轮进程在 supervisor 回合未完成时中断。
23:01 用户发 `continue`（session log seq=2861）后，后端**没有消费任何断点**：

- seq=2862 再次出现“任务理解中…”（首轮已做过的阶段被重做）；
- seq=2863 任务理解因 `APIConnectionError` 失败降级；
- seq=2864–2866 进入 PREPARE（初始化仓库 / EDA 工作区 / 冻结评估器）；
- 23:14 前后 evaluator agent 在文件系统里**偶然**读到 `kaggriculture_summary.md`
  （seq=2884），这是 agent 自主探索，不是续传机制注入；
- 截至复盘时（23:17+），evaluator 已完成（`evaluator.jsonl` 41 KB），prepare agent
  正在运行（`prepare.jsonl` 437 KB）。

结论：**“continue”在行为上等价于冷启动**——已完成或已中断的阶段没有从持久化状态恢复。

## 2. 诊断自查（自我 reflexion：上一轮结论哪些对、哪些错）

| # | 上一轮说法 | 复核结果 |
|---|---|---|
| 1 | “23:01 是重跑 PREPARE / 重新冻结评估器” | **错。** 第一轮从未进入 PREPARE：`workspaces/eda`、`workspaces/evaluator`、`logs/agents/evaluator.jsonl` 的 CreationTime 都是 23:01:35，首轮只有 git init（11:47:41）。23:01 是 PREPARE 首次执行；真正被重复的是**任务理解**（以及若连接正常会再次派发 general 调研）。 |
| 2 | “seq=2889 的 state 快照证明 `record_task_understanding` 被调用” | **错。** seq=2889 是 evaluator agent 用 `shell_command` 读 `state.json` 的 stdout，不是状态发布。`record_task_understanding` 是否被调用只能推断，无直接日志。 |
| 3 | “`state.json` CreationTime=23:01:35 证明第一轮从未写过 state” | **论据不成立。** `atomic_write_json` 是 tmp + `os.replace`，每次保存都会产生新的 CreationTime；且 `runtime_events._publish` 只把 `output` 追加进 session log，**state 快照根本不落 session log**，所以“日志里没有 state 快照”也不能证明任何事。现有证据无法判定首轮是否写过 `state.json`。**但这不影响主结论**：无论旧 state 是否存在，23:01 的代码路径都没有消费它（见根因 R1/R4，代码级事实）。 |
| 4 | “第一轮调研成果完全没用上” | **不准确。** evaluator agent 在 23:14 自己读了 `kaggriculture_summary.md`（seq=2884–2885），属于文件系统探索的偶然复用；不是断点机制注入。 |
| 5 | “PREPARE 无任何恢复能力” | **部分错。** Thread 层已具备恢复能力：`RolloutRecorder` 按 `logs/agents/{agent_id}.jsonl` 逐条 flush（`memory/rollout.py:122-128`），evaluator/prepare 使用固定 agent id（`supervisor/prepare.py:29-32`），`thread_manager.py:90-92` 启动时对非空 rollout 调用 `resume_context_sync` 恢复记忆。缺的是 **phase 编排层**的 checkpoint 与“跳过已完成步骤”。修复应复用现有机制，不新造。 |
| 6 | 未提及 Kaggle 决策 | **补充发现。** `Supervisor.set_kaggle_enabled`（`supervisor.py:184-188`）只写内存 `_kaggle_download`，不写 state；重启后 `kaggle_enabled` 归 False。即使跳过任务理解，Kaggle 接入决定也会丢。 |
| 7 | “`supervisor.jsonl` 0 字节” | **确认正确且原因锁定。** `_run_turn` 成功路径会 `record_items`（`thread_runtime.py:601-604`），取消路径也会留痕（614-619），唯独**失败路径先 rollback、不写**（621-630）。同目录 `evaluator.jsonl`/`prepare.jsonl` 有内容而 `supervisor.jsonl` 为 0 字节，与“两个 supervisor 回合都失败/中断”完全吻合。 |

## 3. 事实与证据（只列可证事实）

| 时间 | 证据 |
|---|---|
| 11:44:01 | session log seq=1：任务 URL + “现在请你参加这个比赛进行尝试” |
| 11:47:41 | `.athena/repo` initial commit（`athena/prepare` 分支）；`supervisor.jsonl` 0 字节创建 |
| 11:48:04 → 12:16:39 | `logs/agents/fc684ae....jsonl` 447,957 B；产出 pull_1..5.json、decoded_agent.py、`kaggriculture_summary.md`（12:16:13） |
| 12:16:39 之后 | 首轮 supervisor 回合再未产生任何输出（session log 无 supervisor 来源记录） |
| 23:01:35 | seq=2861 `continue`；seq=2862 “任务理解中…”；seq=2863 `APIConnectionError` 降级；seq=2864–2866 PREPARE 三连；`workspaces/eda`、`evaluator`、`state.json`、`evaluator.jsonl` 均在该秒创建 |
| 23:11:17 / 23:17:30 | `evaluator.jsonl` 41 KB / `prepare.jsonl` 437 KB —— 成功回合正常落盘 |
| 复盘时 | session log 已增至 6,274 行；`supervisor.jsonl` 仍 0 字节 |

## 4. 根因（代码级，全部在 main 当前工作区核实）

- **R1 任务理解无“已存在”守卫**：`research/runtime.py:566-570` 只要
  `provider 非空 and phase == "PREPARE" and task_text 非空` 就重跑任务理解，从不检查
  `state.task_understanding`。
- **R2 失败/中断回合不留痕**：`app_server/thread_runtime.py:621-630` 失败路径
  rollback 且不 `record_items` → supervisor 回合（含“已完成的 general 调研工具结果”）
  在重启后没有任何可恢复对话；`supervisor.jsonl` 0 字节是直接后果。
- **R3 general 调研成果不入 state**：`research/supervisor/state.py:19-36` 没有
  `task_research_ref` / `task_research_agent_id`；`agent_turn_runner.run_general_turn`
  （464-500）每次 `create_root` 随机新 id，结果只回到 supervisor 内存；
  `Supervisor.dispatch_general`（supervisor.py:1090-1096）不持久化。
- **R4 PREPARE 无 phase 级 checkpoint**：`Supervisor.start()` 在 phase==PREPARE 时
  无条件 `_run_prepare()`（supervisor.py:347-348）；`_run_prepare` 即使
  `tree.best_experiment_id()` 已存在也无条件重跑 `run_prepare_phase`
  （supervisor.py:411）；`PhaseRunner.run_prepare_phase` 只在建好 EDA worktree 后写一次
  `eda_dir`（phase_runner.py:76-79），frozen `evaluator_ref` 只存 `Supervisor` 内存。
- **R5 Kaggle 决策不持久化**：见自查 #6。
- **R6 续跑任务文本错位**：GUI `usePipeline.ts` 把用户输入的短消息本身作为
  `start_search` 的 task（392-427 行 `task: content`，429-447 行 `startSearch({task})`），
  所以 23:01 后端 `_task_text` 是 `"continue"` 而非原始任务；`start_task`
  （runtime.py:749-766）不区分新建/续跑。`parse_intent` 有“用最近历史理解短消息”的兜底
  （gui/service.py:77-106），但 runtime 侧的任务理解与 survey/PREPARE 提示词吃不到原始任务。

## 5. 修复方案

原则：**沿用现有单写者 `ResearchState` + 每 agent JSONL rollout，不引入新持久化机制**。
分 P0（必修）与 P1（可选增强）。

### 5.1 `ResearchState` 新增字段（`research/supervisor/state.py`）

```python
    # 断点续传字段：全部可选，旧 state.json 以默认值加载，向后兼容
    task_text: str | None = None               # 首次完整任务文本（续跑时沿用）
    kaggle_download: bool | None = None        # configure_kaggle 的持久化决定
    task_research_ref: ArtifactRef | None = None       # general 调研结果 artifact
    task_research_agent_id: str | None = None          # general agent 稳定 id
    evaluator_ref: ArtifactRef | None = None           # 已冻结评估器 bundle
```

### 5.2 P0-1：任务理解跳过守卫 + 续跑任务文本（`research/runtime.py`）

- `start()` 条件加 `and self.state.task_understanding is None`；
  当 `task_understanding` 已存在时发布：
  `"断点续传：复用已持久化的任务理解，跳过任务理解回合。"`
- `start_task(task)`：
  ```python
  self._task_text = self.state.task_text or task   # 续跑沿用首次完整任务
  if self.state.task_text is None:
      self.state.task_text = task
      self.state.save(self._state_path)
  ```
  （同项目换新任务视为超出本次范围：需显式删除 `.athena/state.json` 重置。）
- 效果：survey 选题、PREPARE 提示词重新吃到原始任务，而不是 `"continue"`。

### 5.3 P0-2：失败回合留痕（`app_server/thread_runtime.py`）

失败路径在 rollback 前先持久化本轮新增消息（与取消路径一致）：

```python
    except Exception as exc:
        if runtime._ctx is not None and before_index is not None:
            new_items = runtime._ctx.items_since(before_index)
            runtime.record_items(new_items)      # 断点续传：失败/中断也要留痕
            runtime._ctx.rollback(before_index)
```

内存仍回滚（本次调用方看到失败），磁盘保留半成品对话供重启后
`resume_context_sync` 恢复。直接解决 `supervisor.jsonl` 0 字节。

### 5.4 P0-3：general 调研缓存与持久化

- `agent_turn_runner.run_general_turn` 返回 `(agent_id, result)`（新
  `GeneralTurnOutcome`），并在 `create_root` 后、等待前就允许上层拿到 agent id
  （避免 worker 超时/中断后连 id 都丢）。
- `Supervisor.dispatch_general`：
  ```python
  if self.state.task_research_ref is not None:      # 只缓存第一次（研究用）
      cached = json.loads(await self._store.get_text(self.state.task_research_ref))
      return {"cached": True, **cached}             # 不重新派发 general agent
  outcome = await self._run_general_turn(task)
  self.state.task_research_agent_id = outcome.agent_id
  self.state.task_research_ref = await self._store.put_text(
      json.dumps(outcome.result, ensure_ascii=False))
  await self._persist_state()
  return outcome.result
  ```
- 任务理解回合重跑时即使 LLM 再次想派调研，也直接命中缓存——**这是防重复调研的硬保证**。

### 5.5 P0-4：Kaggle 决定持久化（`research/supervisor/supervisor.py`）

- `set_kaggle_enabled`：写入 `state.kaggle_download` 并 `_persist_state()`。
- `Supervisor.__init__`：`self._kaggle_download = state.kaggle_download`（恢复决定）。

### 5.6 P0-5：PREPARE 断点

- `Supervisor` 新增 `checkpoint_evaluator(ref)`：写 `state.evaluator_ref` +
  `_evaluator_ref` 并持久化。
- `PhaseRunner.run_prepare_phase`：
  - 若 `rt._supervisor.evaluator_ref` 已存在且 artifact 可解析 → 发布
    `"PREPARE: 复用已冻结的评估器断点…"` 并跳过 `run_evaluator_plan`；
  - 否则跑 evaluator，成功后立即 `checkpoint_evaluator` 再进入 prepare。
  - prepare 中途崩溃的恢复：依赖现有固定 id `"prepare"` + 非空 `prepare.jsonl` 自动恢复
    线程记忆（机制已存在，见自查 #5），不额外加 turns_used 快照（P1）。
- `Supervisor._run_prepare` 顶部加 SOTA 守卫：
  ```python
  if self.tree.best_experiment_id() is not None:
      await self._publish("output", {... "PREPARE: 已有可信 baseline，跳过 PREPARE。"})
      await self._transition_phase("SEARCH")
      return
  ```
  覆盖“tree 已写、phase 未转”的崩溃窗口，并让 PREPARE 幂等。

### 5.7 P1（可选，不在首批）

- evaluator/prepare 的 `turns_used`/最近 feedback 写入 state，重启后从中断回合继续而非从
  原始任务重新提交。
- 前端 `continue` 分支改为调用后端 `start`/`resume`（不再把短消息当新 task 走
  `parse_intent → start_search`）；后端 P0 已能保证行为正确，此项只是省一次 LLM 预览。

## 6. 验收标准（Definition of Done）

1. 单测：新字段 round-trip + 旧 `state.json`（无新字段）可加载。
2. `start()` 在 `task_understanding` 已存在时：不发起 supervisor 回合，输出
   `"断点续传：…"` 行；不存在时行为与现在一致。
3. `_run_turn` 失败路径：rollout 文件非空且含失败前消息；内存上下文仍回滚。
4. `dispatch_general` 第二次调用返回 `cached=true`，且不新增 general agent 日志文件；
   第一次调用后 `state.json` 含 `task_research_ref`/`task_research_agent_id`。
5. `configure_kaggle` 后重启 runtime：`kaggle_enabled` 恢复原决定。
6. `evaluator_ref` 已冻结后重启 PREPARE：输出“复用评估器断点”，evaluator agent 不重跑；
   `tree` 已有 SOTA 且 phase=PREPARE 时重启：直接进 SEARCH。
7. **复盘验证**：在 `new_kaggle_test`（或其副本）上重启，session log 新行的第一句是
   “断点续传：…”，而不是“任务理解中…”。
8. 回归：改动模块相关既有测试无新增失败（参照 `draft.md` 记录的基线
   1278 passed / 84 failed / 27 errors，只比对受影响模块）。

## 7. 范围外

- SEARCH/VALIDATE 阶段的 recover（已实现，不在本次范围）。
- 同项目自动识别“换新任务”（本次约定：删除 `.athena/state.json` 重置）。
- named sessions / 多会话并发恢复。
- LLM 单次调用的精确续跑（超出 rollout 粒度）。
- athena-gui / athena_ts 前端改动（P1 可选）。

## 8. 实施步骤（确认后执行，每步先测试后实现）

1. `state.py`：加 5 个可选字段 + round-trip/兼容测试。
2. `thread_runtime.py`：失败留痕 + 失败路径单测。
3. `supervisor.py`：Kaggle 持久化、`dispatch_general` 缓存、`_run_prepare` SOTA 守卫、
   `checkpoint_evaluator` + 单测。
4. `agent_turn_runner.py`：`GeneralTurnOutcome`（agent_id 提前暴露）+ 单测。
5. `runtime.py`：任务理解守卫、`start_task` 任务文本持久化/沿用、续传输出行 + 单测。
6. `phase_runner.py`：evaluator 断点复用 + 单测。
7. 复盘验证：副本项目上做“杀进程→重启”演练，产出验收证据。

## 9. 仓库影响

- 修改：`src/athena/research/runtime.py`、`src/athena/research/agent_turn_runner.py`、
  `src/athena/research/supervisor/{state,supervisor}.py`、
  `src/athena/research/phase_runner.py`、`src/athena/app_server/thread_runtime.py`。
- 新增测试：对应模块的 pytest 用例。
- 注意：main 工作区现有未提交改动（`agent_turn_runner.py`、`web_search.py`、
  `test/unit/retrieval/test_web_search.py`、`draft.md`）；实施时只动上述文件、只 stage
  本次任务文件。

## 10. 风险与对策

- **失败留痕语义变化**：半截工具调用也会进 rollout；`resume_context_sync` 按消息粒度
  恢复、跳过损坏记录，可接受，但需回归线程恢复测试。
- **schema 兼容**：新代码读旧 state 没问题；旧代码读含新字段的 state 会因
  `extra="forbid"` 失败 → 回滚代码时需同时删除受影响项目的 `state.json`。
- **单写者约束**：所有新状态写入必须经 `Supervisor` 方法 + `_persist_state`，不绕过。
- **任务文本固化**：续跑沿用首次 `task_text`；同项目换新任务需显式重置（已列入范围外）。

## 11. 回滚/迁移

- 回滚：revert 本次 commit；删除测试项目 `.athena/state.json`；rollout JSONL 为
  append-only 且不含新 schema，旧代码可继续读。
- 迁移：无需迁移脚本；老项目在下次运行续传时自动获得新行为（旧字段缺省 → 任务理解仍会
  重跑一次，之后持久化新字段）。

## 12. 开放问题（需要你拍板）

1. **在哪个 checkout 实施？** main 有未提交改动；`.worktrees/headless-verify` 有 Codex
   `/root` 的活动计划（其 `codex_docs/CURRENT.md` 明确要求不并发实施）。建议新建专用
   worktree/branch，或由你确认直接在 main 改。
2. 是否把 P1 的前端 `continue` 分支改动一并纳入？
3. “同项目换新任务”是否需要顺带加一个显式 reset 命令？
