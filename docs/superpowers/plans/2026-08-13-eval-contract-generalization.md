# Eval 合同泛化 实现计划（predictions 目录 + evaluator handoff spec）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 eval 合同从「单文件 `predictions.csv`」泛化为「`predictions/` 目录 + `evaluator/HANDOFF.md` 自描述 spec」，支持任意 ML 任务的预测产物形态。

**Architecture:** predictions 产物统一为 `dict[str, bytes]`（workspace 相对路径 → 字节），复用冻结 bundle 的 `tree` 打包/解包模式（每个文件 `put_bytes`、清单 `put_text`）。系统仍只认 `metric.json` 的 `eval_script` + stdout 的 `{"primary": float}`，不解析 HANDOFF.md。

**Tech Stack:** Python ≥3.11、pydantic v2、uv、asyncio、现有 `ArtifactStore`/`DataScriptRunner`/`TrustedEvaluator`。

**Spec:** `docs/superpowers/specs/2026-08-13-eval-contract-generalization-design.md`

## Global Constraints

- 系统不解析 HANDOFF.md；它只是 agent 可读的 spec（Markdown）。
- predictions 目录强制（不再支持单文件路径）。
- 二进制安全：注入走 `write_bytes`/`get_bytes`，不经过 `write_text`。
- 保持最薄合同：`metric.json` 与 `eval_script`、stdout `{"primary": float}` 不变。
- 每任务 TDD：先写失败测试 → 跑红 → 最小实现 → 跑绿 → 提交。
- 分支在 `main`；提交只在各自任务的文件集上 `git add <具体文件>`，不要 `git add -A`。

---

## 核心接口（所有任务共享，先读）

```python
# 预测目录的内存表示：workspace 相对路径 -> 字节内容
PredictionsTree = dict[str, bytes]

# script_runner.py 新增两个模块级异步函数：
async def pack_directory(store: ArtifactStore, root: Path) -> ArtifactRef:
    """把 root 目录整树存为清单 artifact（复用 freeze 的 tree 模式）。"""
    # walk root.rglob("*")，每个文件 put_bytes，返回 {"<rel>": <bytes_ref>} 的 put_text ref

async def load_directory(store: ArtifactStore, ref: ArtifactRef) -> dict[str, bytes]:
    """从清单 ref 读回 {rel: bytes}。"""
```

```python
# DataScriptRunner.run 的 extra_files 升级为字节 + 嵌套目录：
# 原: extra_files: dict[str, str] | None   → 写 write_text
# 新: extra_files: dict[str, bytes] | None → 写 write_bytes（target.parent.mkdir(parents=True) 已有）
```

```python
# TrustedEvaluator.score 的签名升级：
async def score(
    self, *,
    eval_bundle: DataScriptBundle,
    predictions: dict[str, bytes],          # 原为 str（CSV 文本）
    candidate_id: str,
    direction: Literal["maximize", "minimize"],
    predictions_root: str,                  # 原 predictions_path（目录根，如 "predictions"）
) -> CandidateEvaluation:
    # extra_files = {f"{predictions_root}/{rel}": content for rel, content in predictions.items()}
```

---

## 任务分解与依赖

```
Task 1 (基础): script_runner.py  pack_directory/load_directory + extra_files 字节化
  └─> Task 2 (基础): evaluation.py  score 签名 dict[str,bytes]
        └─> Task 3: experiment.py  run_turn 打包目录
        └─> Task 4: validation.py _execute_predictions 打包目录
Task 5 (独立): prepare.py  _freeze_evaluator HANDOFF.md + labels 目录
Task 6 (独立): runtime.py  HANDOFF.md 透传给 SEARCH agent
Task 7 (独立): experiment.py manifest 校验 predictions 是目录
Task 8 (独立): prepare_agent.md 提示词更新
Task 9 (依赖 1-7): 单测/集成迁移
Task 10 (依赖 1-8): Titanic 例子迁移 + E2E
```

并行建议：Task 1→2 串行（定义接口）；Task 3/4/5/6/7/8 在 1-2 完成后并行（文件不相交）；Task 9/10 最后。

---

### Task 1: predictions 目录打包/解包 + 字节注入

**Files:**
- Modify: `src/athena/research/script_runner.py`（新增 `pack_directory`/`load_directory`；`run` 的 `extra_files` 字节化）
- Test: `test/unit/research/test_data_scripts.py`

**Interfaces:**
- Produces: `pack_directory(store, root) -> ArtifactRef`、`load_directory(store, ref) -> dict[str, bytes]`；`run(..., extra_files: dict[str, bytes] | None)` 写 `write_bytes`。

- [ ] **Step 1: 写失败测试** —— `test_pack_load_directory_roundtrip`（建含子目录 + 二进制文件的目录，pack→load 断言 bytes 一致）；`test_run_injects_bytes_and_nested`（`extra_files={"sub/b.bin": b"\x00\x01"}`，断言 run 后文件存在且 bytes 一致）。

- [ ] **Step 2: 跑红** `pytest test/unit/research/test_data_scripts.py -q`，断言新测试 FAIL。

- [ ] **Step 3: 实现** `pack_directory`（`root.rglob("*")` → `put_bytes` → 清单 `put_text`）、`load_directory`（`get_text` 清单 → `get_bytes`）；`run` 的 `extra_files` 从 `write_text` 改 `write_bytes`。

- [ ] **Step 4: 跑绿** 同上测试 PASS。

- [ ] **Step 5: 提交** `git add src/athena/research/script_runner.py test/unit/research/test_data_scripts.py && git commit -m "feat: predictions directory pack/load + byte injection"`

---

### Task 2: TrustedEvaluator.score 目录签名

**Files:**
- Modify: `src/athena/research/evaluation.py`
- Test: `test/unit/research/test_evaluation.py`

**Interfaces:**
- Consumes: Task 1 的 `extra_files: dict[str, bytes]`。
- Produces: `score(..., predictions: dict[str, bytes], predictions_root: str)` 构造 `extra_files={f"{predictions_root}/{rel}": content}`。

- [ ] **Step 1: 失败测试** —— 用 `dict[str, bytes]` 调 `score`，断言 runner 收到的 `extra_files` 键为 `predictions_root/rel`。

- [ ] **Step 2: 跑红**。

- [ ] **Step 3: 实现** 改 `score` 签名与 `extra_files` 构造；同步更新 `test_evaluation.py` 与 `test_experiment.py`/`test_validate_agent_contract.py` 里的 `_FakeEvaluator`/`_Evaluator` 的 `score` 签名（加 `predictions: dict[str, bytes]`、`predictions_root: str`）。

- [ ] **Step 4: 跑绿** `pytest test/unit/research/test_evaluation.py test/unit/research/supervisor/test_experiment.py test/integration/research/test_validate_agent_contract.py -q`。

- [ ] **Step 5: 提交**。

---

### Task 3: run_turn 打包 predictions 目录

**Files:**
- Modify: `src/athena/research/supervisor/experiment.py`（`run_turn` 的 predictions 读取段）
- Test: `test/unit/research/supervisor/test_experiment.py`

**Interfaces:**
- Consumes: `pack_directory`/`load_directory`（Task 1）、`score(dict[str, bytes], predictions_root)`（Task 2）。
- Produces: `predictions_ref` 存目录清单；`predictions` 以 `dict[str, bytes]` 传 `score`。

- [ ] **Step 1: 失败测试** —— fixture 里 `outputs.predictions` 指向目录，断言 `run_turn` 产出 scored + `predictions_ref` 为目录清单。

- [ ] **Step 2-4: 红→实现→绿**（把 `predictions_path.read_text` 换成 `pack_directory`+`load_directory`，`score` 传 `dict[str, bytes]` + `manifest.outputs["predictions"]`）。

- [ ] **Step 5: 提交**。

---

### Task 4: _execute_predictions 打包目录

**Files:**
- Modify: `src/athena/research/supervisor/validation.py`
- Test: `test/unit/research/supervisor/test_validation.py`（或现有 validate 测试）

**Interfaces:**
- Consumes: `pack_directory`（Task 1）。
- Produces: `_execute_predictions -> tuple[ArtifactRef, str]`（目录清单 ref + 目录相对路径）。

- [ ] **Step 1: 失败测试** —— 目录 predictions 断言 `predictions_ref` 是清单 + `predictions_path` 是目录路径。

- [ ] **Step 2-4: 红→实现→绿**。

- [ ] **Step 5: 提交**。

---

### Task 5: _freeze_evaluator 冻结 HANDOFF.md + labels 目录

**Files:**
- Modify: `src/athena/research/supervisor/prepare.py`
- Test: `test/unit/research/supervisor/test_prepare_plan.py`

**Interfaces:**
- Produces: `_freeze_evaluator` 冻结的 bundle tree 里包含 `HANDOFF.md` 与 labels（目录或单文件均可）。

- [ ] **Step 1: 失败测试** —— evaluator 目录含 `HANDOFF.md` + `labels/`，断言冻结 bundle 的 tree 含两者。

- [ ] **Step 2-4: 红→实现→绿**（放宽 `labels.csv` 单文件假设，改为目录或 `labels.csv`/`labels/` 任一）。

- [ ] **Step 5: 提交**。

---

### Task 6: HANDOFF.md 透传给 SEARCH agent

**Files:**
- Modify: `src/athena/research/runtime.py`（SEARCH plan 的 context 构造处）
- Test: `test/unit/research/test_runtime*.py`（或 supervisor 测试）

**Interfaces:**
- Produces: SEARCH plan 的 `context_ref` 里携带 evaluator HANDOFF.md 文本，使 ideator/worker 可读。

- [ ] **Step 1: 失败测试** —— 断言 plan 上下文中包含 HANDOFF.md 内容。

- [ ] **Step 2-4: 红→实现→绿**。

- [ ] **Step 5: 提交**。

---

### Task 7: manifest 校验 predictions 是目录

**Files:**
- Modify: `src/athena/research/supervisor/experiment.py`（`ExperimentManifest._validate_outputs`）
- Test: `test/unit/research/supervisor/test_experiment.py`

**Interfaces:**
- Produces: `outputs.predictions` 语义为目录（相对路径无 `..`/绝对，仍由现有 `_validate_relative_path` 保证；目录存在性在 run 时校验，不在 manifest 校验）。

- [ ] **Step 1: 失败测试** —— 断言目录路径合法（现有校验已覆盖逃逸/绝对，本任务主要补「predictions 必须是存在的目录」的 run 时错误文案）。

- [ ] **Step 2-4: 红→实现→绿**。

- [ ] **Step 5: 提交**。

---

### Task 8: prepare_agent.md 提示词更新

**Files:**
- Modify: `src/athena/core/agent/prompts/prepare_agent.md`

**Interfaces:**
- Produces: 提示词要求 agent 产出 `predictions/` 目录 + `evaluator/HANDOFF.md`（描述格式 + 判定标准）+ `evaluator/evaluate.py` 读目录打印 `{"primary": float}`。

- [ ] **Step 1: 改提示词**（替换单文件 `predictions.csv` 措辞为目录 + HANDOFF.md）。

- [ ] **Step 2: 提交**（无测试，提示词变更，用 `docs`/`fix(prompts)` 提交）。

---

### Task 9: 单测/集成迁移

**Files:**
- Modify: `test/unit/research/test_data_scripts.py`、`test/unit/research/supervisor/test_experiment.py`、`test_prepare_plan.py`、`test/unit/research/test_evaluation.py`、`test/integration/research/test_prepare_agent_contract.py`、`test_validate_agent_contract.py`

**Interfaces:**
- Consumes: Task 1-7 的最终签名。

- [ ] **Step 1: 全量单测** `.venv/Scripts/python.exe -m pytest test/unit/research/ -q`，把 `outputs.predictions` 单文件 fixture 改成目录。

- [ ] **Step 2: 集成** `.venv/Scripts/python.exe -m pytest test/integration/research/ -q`。

- [ ] **Step 3: 全绿后提交**。

---

### Task 10: Titanic 例子迁移 + E2E

**Files:**
- Modify: `examples/titanic/...`（如需）、`examples/e2e-verify*`（迁移脚本或 README）
- Modify: `README.md`（headless 命令不变，补 predictions 目录说明）

**Interfaces:**
- Consumes: 全部。

- [ ] **Step 1: 迁移** Titanic 例子 `predictions.csv` → `predictions/predictions.csv`，`evaluate.py` 读目录，加 `evaluator/HANDOFF.md`。

- [ ] **Step 2: E2E** `.venv/Scripts/python.exe scripts/run_headless.py --project examples/e2e-verify-dir --task "..." --data "$(pwd)/examples/titanic" --search-limit 2`，断言 `TERMINAL: phase=COMPLETED status=COMPLETED`。

- [ ] **Step 3: 提交**。

---

## 自检

- Spec 覆盖：predictions 目录化（Task 1/3/4）、HANDOFF 进 bundle + 透传（Task 5/6）、字节注入（Task 1）、manifest 目录语义（Task 7）、提示词（Task 8）、迁移 + E2E（Task 9/10）——全覆盖。
- 类型一致性：`PredictionsTree = dict[str, bytes]`、`pack_directory -> ArtifactRef`、`load_directory -> dict[str, bytes]`、`score(predictions: dict[str, bytes], predictions_root: str)` 贯穿 Task 1-4、9。
- 无占位符。
