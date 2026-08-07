# Athena 本地可信工作流收口实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按已批准规格 `docs/superpowers/specs/2026-08-07-athena-local-workflow-closeout-design.md` 收口本地可信工作流，使 `src/main.py` 的 PREPARE → SEARCH → VALIDATE → REPORT 全流程在离线测试与 `examples/titanic` 真实数据上跑通，并修复 `main.py` 对已删除 `split_manifest_path` 的引用。

**Architecture:** 复用既有 `ResearchRuntime` 控制面与已批准的 `2026-08-07-athena-trusted-execution.md` 计划（任务 3–6）。Task 1（phase-scoped prepared data + trusted evaluation）与 Task 2（local + Docker runtimes）已提交；本计划自 Task 3 起，将 `CodeAgent` 重构为注入 `ExperimentRuntime` / `TrustedEvaluator` / `ArtifactStore` 的有界修复循环，把 `EvaluationInputs` 穿透 baseline / SEARCH / VALIDATE，冻结 final-test，补 CLI 预检与本地模式，重写离线 E2E 并在 titanic 真实试跑。

**Tech Stack:** Python 3.11/3.12, asyncio, pandas, scikit-learn, Pydantic, Git worktrees, Codex CLI, `qoder-agent-sdk==1.0.12`, pytest。

## Global Constraints

- 默认执行模式 `local`；本次保留已提交的 Docker 运行时，但 `main.py` 默认 `local`，本地模式记录 `strong_isolation: false` 并向 stderr 输出一次隔离性警告。
- 本地实验子进程环境允许列表：`PATH`、`SYSTEMROOT`、`WINDIR`、`TEMP`、`TMP` 及显式 `ATHENA_*`；不得含 `.env` 值与模型凭据。
- 生成代码可增改删 worktree 内任意常规辅助文件；最终必须存在 `run_experiment.py`。
- `.git/`、`.gitignore`、`eval.py`、`eval_spec.json`、`.athena/phase_manifest.json`、运行时输出为保护路径。
- 生成代码只写 `predictions.csv`（两列 `__athena_row_id,prediction`）；标签私有于 Athena 评估层。
- baseline / SEARCH / ablation 用 validation 数据；只有冻结 final-test 收到 test 特征。
- final-test 不调用任何代码后端，记录与 SOTA 完全相同的 commit。
- 每个生产行为变更走 RED → GREEN → 回归验证。
- 本计划不引入 Docker CLI 新行为；不新增 .env 凭据暴露。
- 现有未提交的 `git_workspace.py`（`--exclude-standard`）、`code_agent.py`（validation/test 提示词）、`test/unit/test_git_workspace.py`、`tests/test_code_agent_workflow.py`、`tests/test_e2e_ai4ml.py` 改动并入对应任务，不回退。

---

### Task 1: 现状基线核验

**Files:**
- Test: `tests/test_trusted_evaluation.py`, `tests/test_prepare_runtime.py`, `tests/test_experiment_execution.py`, `tests/test_code_runner.py`, `tests/test_e2e_ai4ml.py`

**Interfaces:**
- Consumes: 已提交的 `PreparedWorkflowData(validation_inputs, test_inputs, eval_spec_path, evaluator_path)`、`TrustedEvaluator`、`LocalExperimentRuntime` / `DockerExperimentRuntime`。
- Produces: 确认 `main.py` 当前失败点（`split_manifest_path`）与测试基线。

- [ ] **Step 1: 运行 Task 1–2 相关测试确认已提交工作健康**

Run: `uv run pytest -q tests/test_trusted_evaluation.py tests/test_prepare_runtime.py tests/test_experiment_execution.py tests/test_code_runner.py`
Expected: 全部 PASS（Task 1–2 已由提交 `a4aaf54`、`2efb300` 覆盖）。

- [ ] **Step 2: 运行离线 E2E 确认当前破坏点**

Run: `uv run pytest -q tests/test_e2e_ai4ml.py::test_main_composition_runs_prepare_search_validate_report_offline`
Expected: FAIL，`src/main.py` 抛出 `AttributeError: 'PreparedWorkflowData' object has no attribute 'split_manifest_path'`。此即本计划首要修复点。

- [ ] **Step 3: 记录现状**

确认工作区未提交改动与 Global Constraints 一致，不另提交。

---

### Task 2: 生成树策略与后端反馈

**Files:**
- Modify: `src/athena/code/types.py`
- Modify: `src/athena/code/backends/base.py`
- Modify: `src/athena/code/backends/codex.py`
- Modify: `src/athena/code/backends/qoder.py`
- Create: `src/athena/code/file_policy.py`
- Test: `tests/test_code_backend.py`
- Test: `tests/test_code_agent_workflow.py`

**Interfaces:**
- Consumes: `CodeBackend.generate(prompt, target_dir, previous_outputs, history) -> GenerationResult`、`snapshot_files`。
- Produces: `GenerationResult.files_deleted: list[str]`；`generation_result()` 报告删除路径；`render_backend_prompt(prompt, previous_outputs, history) -> str`；`GeneratedTreePolicy.validate(root: Path, changed_paths: set[str]) -> None`（违反时抛 `CodeExecutionError`）。

- [ ] **Step 1: 写失败测试（后端反馈与删除报告）**

在 `tests/test_code_backend.py` 追加：

```python
class _DeletingBackend(CodeBackend):
    def __init__(self, runner):
        self._runner = runner
        self.observed = {}

    async def generate(self, prompt, target_dir, previous_outputs, history):
        self.observed = {"prompt": prompt, "prev": list(previous_outputs), "hist": list(history)}
        root = Path(target_dir)
        (root / "old_model.py").unlink(missing_ok=True)
        (root / "run_experiment.py").write_text("print('ok')\n", encoding="utf-8")
        return generation_result(
            snapshot_files(root),
            snapshot_files(root),
            output="revised",
        )


@pytest.mark.asyncio
async def test_render_backend_prompt_includes_bounded_feedback():
    from athena.code.backends.base import render_backend_prompt

    previous = [ExecutionOutput(stdout="", stderr="model failed on validation", returncode=1)]
    history = [{"round": 1, "files": ["run_experiment.py"], "stdout": "", "stderr": "boom"}]
    prompt = render_backend_prompt("do it", previous, history)
    assert "Previous execution feedback" in prompt
    assert "model failed on validation" in prompt
    assert "boom" in prompt


def test_generation_result_reports_deleted_files(tmp_path):
    from athena.code.backends.base import generation_result, snapshot_files

    (tmp_path / "old_model.py").write_text("x", encoding="utf-8")
    before = snapshot_files(tmp_path)
    (tmp_path / "old_model.py").unlink()
    result = generation_result(before, snapshot_files(tmp_path), output="")
    assert result.files_deleted == ["old_model.py"]
```

- [ ] **Step 2: 运行测试验证 RED**

Run: `uv run pytest -q tests/test_code_backend.py -k "render or deleted"`
Expected: FAIL（`GenerationResult` 无 `files_deleted`，`render_backend_prompt` 不存在）。

- [ ] **Step 3: 实现类型与删除报告**

`src/athena/code/types.py` 的 `GenerationResult` 增加字段：

```python
@dataclass
class GenerationResult:
    files_created: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    files_deleted: list[str] = field(default_factory=list)
    output: str = ""
```

`src/athena/code/backends/base.py` 更新 `generation_result` 并新增 `render_backend_prompt`：

```python
import json


def generation_result(before, after, *, output):
    return GenerationResult(
        files_created=sorted(after.keys() - before.keys()),
        files_modified=sorted(
            path for path in after.keys() & before.keys() if after[path] != before[path]
        ),
        files_deleted=sorted(before.keys() - after.keys()),
        output=output,
    )


def render_backend_prompt(prompt, previous_outputs, history):
    parts = [prompt]
    recent_history = history[-3:]
    if recent_history:
        parts.append("Previous rounds (JSON):")
        parts.append(json.dumps(recent_history, ensure_ascii=False, default=str))
    for output in previous_outputs[-3:]:
        stdout = (output.stdout or "")[-4000:]
        stderr = (output.stderr or "")[-4000:]
        parts.append(
            f"Previous execution feedback:\nstdout:\n{stdout}\nstderr:\n{stderr}"
        )
    return "\n\n".join(parts)
```

- [ ] **Step 4: 后端改用渲染提示词**

`codex.py`：删除 `del previous_outputs, history`，将命令中的 `prompt` 改为
`render_backend_prompt(prompt, previous_outputs, history)`（在 import 区导入）。`qoder.py` 同改，`query(prompt=render_backend_prompt(...), options=options)`。

- [ ] **Step 5: 运行后端测试验证 GREEN**

Run: `uv run pytest -q tests/test_code_backend.py`
Expected: 全部 PASS。

- [ ] **Step 6: 写失败测试（生成树策略）**

在 `tests/test_code_agent_workflow.py` 追加：

```python
import os

import pytest


@pytest.mark.asyncio
async def test_generated_tree_policy_permits_auxiliary_files(tmp_path):
    from athena.code.file_policy import GeneratedTreePolicy
    from athena.workflows.search.code_agent import CodeExecutionError

    (tmp_path / "run_experiment.py").write_text("print(1)", encoding="utf-8")
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "stack.py").write_text("x", encoding="utf-8")
    GeneratedTreePolicy().validate(
        tmp_path, {"run_experiment.py", "models/stack.py", "config.json"}
    )


@pytest.mark.asyncio
async def test_generated_tree_policy_rejects_protected_and_missing_entrypoint(tmp_path):
    from athena.code.file_policy import GeneratedTreePolicy
    from athena.workflows.search.code_agent import CodeExecutionError

    (tmp_path / "run_experiment.py").write_text("print(1)", encoding="utf-8")
    policy = GeneratedTreePolicy()
    with pytest.raises(CodeExecutionError, match="protected path changed"):
        policy.validate(tmp_path, {"run_experiment.py", "eval.py"})
    (tmp_path / "run_experiment.py").unlink()
    with pytest.raises(CodeExecutionError, match="run_experiment.py"):
        policy.validate(tmp_path, set())


@pytest.mark.asyncio
async def test_generated_tree_policy_rejects_external_symlink(tmp_path):
    from athena.code.file_policy import GeneratedTreePolicy
    from athena.workflows.search.code_agent import CodeExecutionError

    (tmp_path / "run_experiment.py").write_text("print(1)", encoding="utf-8")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    try:
        os.symlink(outside, tmp_path / "leak.py")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation requires platform privileges")
    with pytest.raises(CodeExecutionError, match="symlink"):
        GeneratedTreePolicy().validate(tmp_path, {"leak.py"})
```

- [ ] **Step 7: 运行策略测试验证 RED**

Run: `uv run pytest -q tests/test_code_agent_workflow.py -k "policy"`
Expected: FAIL（`athena.code.file_policy` 不存在）。

- [ ] **Step 8: 实现 `file_policy.py`**

```python
"""Policy for generated experiment trees inside one worktree."""

from pathlib import Path


class GeneratedTreePolicy:
    PROTECTED = {".gitignore", "eval.py", "eval_spec.json"}
    _RUNTIME_NAMES = {"predictions.csv", "labels.csv", "eval_result.json"}

    def validate(self, root: Path, changed_paths: set[str]) -> None:
        from athena.workflows.search.code_agent import CodeExecutionError

        root = root.resolve()
        entrypoint = root / "run_experiment.py"
        if not entrypoint.is_file():
            raise CodeExecutionError("generated tree is missing run_experiment.py")
        for raw in sorted(changed_paths):
            posix = raw.replace("\\", "/")
            name = Path(raw).name
            if (
                posix in self.PROTECTED
                or posix == ".athena"
                or posix.startswith(".athena/")
                or name in self._RUNTIME_NAMES
                or name.startswith("athena_logs_")
            ):
                raise CodeExecutionError(f"protected path changed: {raw}")
            candidate = root / raw
            if candidate.is_symlink():
                raise CodeExecutionError(f"symlink rejected: {raw}")
            if not candidate.resolve().is_relative_to(root):
                raise CodeExecutionError(f"escape outside worktree: {raw}")
```

> 说明：`splits.json` 已由 `.athena/phase_manifest.json` 取代；`.athena/` 整树（含阶段清单与 phase inputs，可能携带标签 train.csv）为受保护运行时路径。`CodeExecutionError` 延迟导入以避循环依赖；抛出点无 `logs`，由 code_agent 包装补 `logs`（见 Task 3 Step 5）。

- [ ] **Step 9: 运行策略测试验证 GREEN**

Run: `uv run pytest -q tests/test_code_agent_workflow.py -k "policy"`
Expected: PASS。

- [ ] **Step 10: 提交**

```bash
git add src/athena/code/types.py src/athena/code/backends/base.py src/athena/code/backends/codex.py src/athena/code/backends/qoder.py src/athena/code/file_policy.py tests/test_code_backend.py tests/test_code_agent_workflow.py
git commit -m "feat: enforce generated experiment tree policy"
```

---

### Task 3: CodeAgent 有界修复循环（注入运行时/评估器/存储）

**Files:**
- Modify: `src/athena/code/engine.py`
- Modify: `src/athena/workflows/search/code_agent.py`
- Modify: `tests/test_code_engine.py`
- Modify: `tests/test_code_agent_workflow.py`

**Interfaces:**
- Consumes: `EvaluationInputs`、`ExperimentRuntime`、`TrustedEvaluator`、`ArtifactStore`、`GeneratedTreePolicy`、`render_backend_prompt`。
- Produces:
  - `CodeAgent.__init__(..., runtime, artifacts, evaluator, max_rounds=3, monitor=None)`。
  - `CodeAgent.execute(experiment_id, hypothesis, plan, parent_commit, eval_spec, worktree, *, inputs: EvaluationInputs) -> CodegenResult`。
  - `CodeAgent._stage_inputs(wt_path, inputs) -> None`：写 `.athena/phase_manifest.json` 并复制 train/features 到 `.athena/inputs/`。
  - `CodeAgent._protected_hashes` 保护清单加入 `.athena/phase_manifest.json`。
  - `CodegenResult` 增加 `evaluation: ArtifactRef`。

- [ ] **Step 1: 写失败测试（阶段清单、修复循环、frozen 契约的 execute 分支）**

在 `tests/test_code_agent_workflow.py` 追加：

```python
from athena.evaluation.types import EvaluationInputs


def _inputs(tmp_path) -> EvaluationInputs:
    train = tmp_path / "train.csv"
    features = tmp_path / "features.csv"
    labels = tmp_path / "labels.csv"
    train.write_text("__athena_row_id,label\n0,0\n1,1\n", encoding="utf-8")
    features.write_text("__athena_row_id,feature\n0,1\n1,2\n", encoding="utf-8")
    labels.write_text("__athena_row_id,label\n0,0\n1,1\n", encoding="utf-8")
    return EvaluationInputs(
        phase="validation",
        train_path=train,
        features_path=features,
        labels_path=labels,
        target="label",
    )


@pytest.mark.asyncio
async def test_code_agent_stages_phase_manifest_and_inputs(tmp_path) -> None:
    inputs = _inputs(tmp_path)
    worktree = GitWorkBranch(path=str(tmp_path), branch="exp/t", base_commit="a" * 40)
    agent = CodeAgent()
    await agent._stage_inputs(tmp_path, inputs)
    manifest = json.loads((tmp_path / ".athena" / "phase_manifest.json").read_text())
    assert manifest["phase"] == "validation"
    assert manifest["row_id_column"] == "__athena_row_id"
    assert manifest["target"] == "label"
    assert (tmp_path / ".athena" / "inputs" / "train.csv").is_file()
    assert "labels" not in json.dumps(manifest)
```

> 注：此测试暂以 `CodeAgent()`（无注入）验证阶段清单写入。frozen 分支与注入循环在 Task 5（execute_frozen）落地；`execute` 的注入改造在 Step 5–7 完成。

- [ ] **Step 2: 运行测试验证 RED**

Run: `uv run pytest -q tests/test_code_agent_workflow.py -k "stages_phase_manifest"`
Expected: FAIL（`CodeAgent._stage_inputs` 不存在）。

- [ ] **Step 3: 实现 `_stage_inputs` 与保护清单扩展**

`code_agent.py` 增加导入 `EvaluationInputs`，新增：

```python
    @staticmethod
    async def _stage_inputs(wt_path: Path, inputs: EvaluationInputs) -> None:
        stage = wt_path / ".athena" / "inputs"
        stage.mkdir(parents=True, exist_ok=True)
        for name in ("train", "predict"):
            source = (
                inputs.train_path if name == "train" else inputs.features_path
            )
            await asyncio.to_thread(
                shutil.copyfile, source, stage / f"{name}.csv"
            )
        manifest = {
            "phase": inputs.phase,
            "row_id_column": inputs.row_id_column,
            "target": inputs.target,
            "train": ".athena/inputs/train.csv",
            "predict": ".athena/inputs/predict.csv",
        }
        await asyncio.to_thread(
            (wt_path / ".athena" / "phase_manifest.json").write_text,
            json.dumps(manifest, sort_keys=True),
            encoding="utf-8",
        )
```

`_protected_hashes` 保护清单改为
`(EVALUATION_ENTRYPOINT, "eval_spec.json", ".athena/phase_manifest.json")`。`import shutil` 与 `import json` 已在文件中。

- [ ] **Step 4: 运行阶段清单测试验证 GREEN**

Run: `uv run pytest -q tests/test_code_agent_workflow.py -k "stages_phase_manifest"`
Expected: PASS。

- [ ] **Step 5: 重构 `CodeAgent.execute` 为注入运行时/评估器的有界循环**

先扩展 `CodegenResult`，增加 `evaluation: ArtifactRef` 字段：

```python
class CodegenResult(BaseModel):
    experiment_id: str
    commit: str
    diff: ArtifactRef
    eval: EvalResult
    evaluation: ArtifactRef
    logs: ArtifactRef
    wall_time_s: float = 0.0
```

再改 `__init__` 增加参数 `runtime=None, artifacts=None, evaluator=None`，内部保存
`self._runtime = runtime or LocalExperimentRuntime()`、`self._artifacts = artifacts`、
`self._evaluator = evaluator`。`execute` 增加 `*, inputs: EvaluationInputs` 关键字参数，并把「生成 → 执行 → 评估 → 修复」迭代移入 `CodeAgent`：

```python
    async def execute(
        self,
        experiment_id,
        hypothesis,
        plan,
        parent_commit,
        eval_spec,
        worktree,
        *,
        inputs: EvaluationInputs,
    ) -> CodegenResult:
        wt_path = Path(worktree.path)
        wt_path.mkdir(parents=True, exist_ok=True)
        started_at = time.time()
        log_path = wt_path / f"athena_logs_{experiment_id}.txt"
        logs_ref = f"artifact://{log_path}"

        await self._stage_inputs(wt_path, inputs)
        await self._ensure_evaluator(wt_path, eval_spec)
        protected_before = self._protected_hashes(wt_path)

        history: list[dict] = []
        previous_outputs: list[ExecutionOutput] = []
        log_results: list[tuple[str, ProcessResult]] = []
        evaluation: EvalResult | None = None
        if self._artifacts is None or self._evaluator is None:
            raise CodeExecutionError(
                "trusted evaluation is not configured", logs=logs_ref
            )
        if self._backends:
            for round_num in range(1, self._max_rounds + 1):
                backend_name = (
                    self._router.route(hypothesis)
                    if self.backend == "auto"
                    else self.backend
                )
                try:
                    selected_backend = self._backends[backend_name]
                except KeyError as exc:
                    raise CodeExecutionError(
                        f"code backend is not configured: {backend_name}",
                        logs=logs_ref,
                    ) from exc
                generation = await selected_backend.generate(
                    prompt=render_backend_prompt(
                        self._generation_prompt(
                            experiment_id, hypothesis, plan, eval_spec
                        ),
                        previous_outputs,
                        history,
                    ),
                    target_dir=str(wt_path),
                    previous_outputs=previous_outputs,
                    history=history,
                )
                changed = (
                    set(generation.files_created)
                    | set(generation.files_modified)
                    | set(generation.files_deleted)
                )
                self._verify_protected_files(wt_path, protected_before, logs_ref)
                if EXPERIMENT_ENTRYPOINT not in changed:
                    raise CodeExecutionError(
                        "code backend did not modify run_experiment.py",
                        logs=logs_ref,
                    )
                try:
                    GeneratedTreePolicy().validate(wt_path, changed)
                except CodeExecutionError as policy_error:
                    raise CodeExecutionError(str(policy_error), logs=logs_ref) from policy_error
                run_output = await self._runtime.run(
                    ExecutionRequest(
                        entrypoint=EXPERIMENT_ENTRYPOINT,
                        cwd=wt_path,
                        timeout_s=300,
                        environment={
                            "ATHENA_EXPERIMENT_ID": experiment_id,
                            "ATHENA_PHASE": inputs.phase,
                        },
                        readonly_inputs=(inputs.features_path,),
                    )
                )
                run_record = ("run_experiment.py", ProcessResult(
                    returncode=run_output.returncode,
                    output="\n".join(
                        part for part in (run_output.stdout, run_output.stderr) if part
                    ),
                ))
                log_results.append(run_record)
                await self._write_logs(log_path, log_results)
                previous_outputs.append(run_output)
                history.append({
                    "round": round_num,
                    "files": sorted(changed),
                    "stdout": (run_output.stdout or "")[-2000:],
                    "stderr": (run_output.stderr or "")[-2000:],
                })
                if run_output.returncode != 0:
                    continue
                try:
                    evaluation = await self._evaluator.evaluate(
                        experiment_id,
                        wt_path / "predictions.csv",
                        inputs,
                        eval_spec,
                    )
                except Exception as exc:
                    previous_outputs.append(ExecutionOutput(
                        stdout="",
                        stderr=f"trusted evaluation failed: {exc}",
                        returncode=-1,
                    ))
                    continue
                break
            else:
                raise CodeExecutionError(
                    "code generation failed after bounded revision rounds",
                    logs=logs_ref,
                )
        else:
            raise CodeExecutionError(
                "no code-generation backend configured for trusted execution",
                logs=logs_ref,
            )
        if evaluation is None:
            raise CodeExecutionError(
                "experiment did not produce a valid trusted evaluation",
                logs=logs_ref,
            )

        predictions_path = wt_path / "predictions.csv"
        canonical = await asyncio.to_thread(
            lambda: predictions_path.read_bytes()
        )
        evaluation_ref = await self._artifacts.put_bytes(canonical)
        diff_ref = f"artifact://diffs/{experiment_id}"
        commit = parent_commit
        if self._workspace is not None:
            diff_ref = await self._workspace.diff(worktree)
            commit = await self._workspace.commit(
                worktree, diff_ref, f"experiment: {experiment_id}"
            )
        return CodegenResult(
            experiment_id=experiment_id,
            commit=commit,
            diff=diff_ref,
            eval=evaluation,
            evaluation=evaluation_ref,
            logs=logs_ref,
            wall_time_s=time.time() - started_at,
        )
```

> 注：`render_backend_prompt` 从 `athena.code.backends.base` 导入；`ExecutionRequest`、`LocalExperimentRuntime` 从 `athena.code.execution` 导入；`GeneratedTreePolicy` 从 `athena.code.file_policy` 导入；`ProcessResult` 在本文件已定义。原「无后端时写失败 entrypoint」分支移除——受信执行路径必须有后端。`CodeEngine` 保留为兼容引擎。

- [ ] **Step 6: 更新 `CodegenResult` 与旧调用点**

`code_agent.py` 中 `CodegenResult` 增加 `evaluation: ArtifactRef`。更新 `baseline.py`、`search_loop.py`、`ablation.py` 的 `code_agent.execute(...)` 调用，补 `inputs=...`（validation），并把 `result.evaluation` 一并写入树 artifacts（`{"diff", "logs", "evaluation"}`）。

- [ ] **Step 7: 更新既有 CodeAgent 测试以适配注入循环**

`tests/test_code_agent_workflow.py` 中 `_fake_evaluation_run`、`WritingBackend` 等旧测试改为注入 `runtime`/`evaluator`/`artifacts`：构建真实 `LocalExperimentRuntime()`、`LocalArtifactStore(tmp_path / "store")`、`TrustedEvaluator(store)`，并传 `inputs=_inputs(tmp_path)`。`test_code_agent_generates_reviews_and_commits_real_diff` 与 `test_code_agent_feeds_execution_failure_to_revision_round` 须保持断言语义不变。

- [ ] **Step 8: 运行 CodeAgent 与引擎测试**

Run: `uv run pytest -q tests/test_code_agent_workflow.py tests/test_code_engine.py`
Expected: 全部 PASS（含新阶段清单/策略/修复循环测试）。

- [ ] **Step 9: 提交**

```bash
git add src/athena/code/engine.py src/athena/workflows/search/code_agent.py src/athena/workflows/prepare/baseline.py src/athena/workflows/search/search_loop.py src/athena/workflows/validate/ablation.py tests/test_code_engine.py tests/test_code_agent_workflow.py
git commit -m "feat: run bounded trusted generation loop"
```

---

### Task 4: 结构化 GitDiff 与耐久证据

**Files:**
- Modify: `src/athena/core/workspace.py`
- Modify: `src/athena/git_workspace.py`
- Modify: `src/athena/code/review.py`
- Modify: `test/unit/test_git_workspace.py`
- Modify: `tests/test_code_review.py`
- Modify: `tests/test_code_agent_workflow.py`

**Interfaces:**
- Consumes: 既有 `GitWorkspace.diff/commit`。
- Produces: `GitDiff(ref: ArtifactRef, paths: tuple[str, ...])`；`GitWorkspace.diff(workspace) -> GitDiff`；`commit(workspace, approved_diff: GitDiff, message)`。

- [ ] **Step 1: 写失败测试（结构化 diff 含删除与忽略）**

在 `test/unit/test_git_workspace.py` 追加：

```python
@pytest.mark.asyncio
async def test_diff_reports_paths_including_deletions(tmp_path):
    from athena.git_workspace import LocalGitWorkspace

    repo = tmp_path / "repo"
    workspace = LocalGitWorkspace(
        repo, tmp_path / "wt", lambda b: f"artifact://d/{len(b)}"
    )
    base = await workspace.init(repo_path=repo)

    branch = await workspace.create(base, "exp/t4")
    p = Path(branch.path)
    (p / "run_experiment.py").write_text("print(1)", encoding="utf-8")
    (p / "old.py").write_text("x", encoding="utf-8")
    diff = await workspace.diff(branch)
    assert "run_experiment.py" in diff.paths
    assert "old.py" in diff.paths
    assert diff.ref.startswith("artifact://")

    (p / "old.py").unlink()
    diff2 = await workspace.diff(branch)
    assert "old.py" in diff2.paths
```

> 注：第二次 `diff()` 在同一 worktree 上可重复调用（`state["committed"]` 未置位）。第二次 diff 将 `git add -A` 暂存删除，`git diff --cached --binary base_commit` 呈现删除，paths 含 `old.py`。若实现限定 diff 只允许一次，则拆为两个分支各 diff 一次，并在第二次断言删除路径。关键断言是 `diff2.paths` 含删除路径。

- [ ] **Step 2: 运行测试验证 RED**

Run: `uv run pytest -q test/unit/test_git_workspace.py`
Expected: FAIL（`diff()` 返回 `ArtifactRef`，无 `.paths`）。

- [ ] **Step 3: 实现结构化 diff**

`core/workspace.py`：

```python
class GitDiff(BaseModel):
    ref: ArtifactRef
    paths: tuple[str, ...]
```

`GitWorkspace.diff` 返回类型改为 `GitDiff`；`commit(workspace, approved_diff: GitDiff, message)`。`git_workspace.py` 的 `diff()`：`git add -A` 后用 `git diff --cached --name-only -z <base_commit>` 解码 NUL 路径存 `paths`，审查记录存 `ref` 与 sha256/tree/head/empty，返回 `GitDiff(ref=artifact, paths=tuple(sorted(paths)))`。`commit()` 比较 `approved_diff.ref` 与 `state["review"]["artifact"]`。

- [ ] **Step 4: 更新语义审查测试**

`tests/test_code_review.py`：断言辅助路径（`models/stack.py`、`config.json`）获批准，保护路径被拒，删除头（`--- a/old.py`、`+++ /dev/null`）不被漏判。`review_diff` 的 `allowed_files` 语义保持；`declared_dependencies` 现按「已装环境包」传递，pandas/sklearn/scipy 不再误报为未声明依赖。`review.py` 的 `_PROTECTED_FILES` 改为 `{"eval.py", "eval_spec.json", ".gitignore"}`（`splits.json` 已由 `.athena/phase_manifest.json` 取代，该路径由 `GeneratedTreePolicy` 覆盖）。

- [ ] **Step 5: CodeAgent 集成 diff 审查与耐久证据**

在 `execute` 与 `execute_frozen` 中、`workspace.diff()` 返回 `GitDiff` 后：

```python
            diff = await self._workspace.diff(worktree)
            patch = await self._artifacts.get_bytes(diff.ref)
            verdict = review_diff(
                patch.decode("utf-8", errors="replace"),
                allowed_files=set(diff.paths),
                declared_dependencies=_installed_dependencies(),
            )
            if verdict.action == "reject":
                raise CodeExecutionError(
                    "generated diff rejected: " + "; ".join(verdict.reasons),
                    logs=logs_ref,
                )
            diff_ref = diff.ref
            commit = await self._workspace.commit(worktree, diff, f"experiment: {experiment_id}")
```

新增辅助函数（`code_agent.py` 模块级）：

```python
def _installed_dependencies() -> set[str]:
    """Top-level import names available in the frozen Athena environment."""
    try:
        from importlib.metadata import packages_distributions
    except ImportError:  # pragma: no cover - Python 3.11+
        return set()
    names = set()
    for top_level in packages_distributions().values():
        names.update(top_level)
    return names
```

> 注：生成的实验代码运行于 Athena 自身环境（`LocalExperimentRuntime` 用 `sys.executable`），故 `declared_dependencies` 取当前解释器的顶层包名，避免 pandas/sklearn 等合法依赖被 `review_diff` 判为未声明。`commit` 第二参现为 `GitDiff` 对象（Task 4 Step 3）。

日志与评估 JSON 用 `artifacts.put_text` 落 `sha256:` 引用：`logs` 在失败路径仍为 `artifact://<wt>/athena_logs_*.txt`，成功路径在 Task 3 已由 `put_bytes(predictions)` 得到 `evaluation` 引用；此处再以 `artifacts.put_text("\n".join(...))` 持久化 generation 日志并令 `logs` 指向该 `sha256:` 引用（供 worktree 清理后读取）。`CodegenResult.evaluation` 已在 Task 3 落地。

- [ ] **Step 6: 运行 Git 与审查回归**

Run: `uv run pytest -q test/unit/test_git_workspace.py tests/test_code_review.py tests/test_code_agent_workflow.py tests/test_search_workflow.py tests/test_validate_report.py`
Expected: 全部 PASS（旧断言若按字符串 `==` 比较 diff ref 的需改为 `.ref`）。

- [ ] **Step 7: 提交**

```bash
git add src/athena/core/workspace.py src/athena/git_workspace.py src/athena/code/review.py src/athena/workflows/search/code_agent.py test/unit/test_git_workspace.py tests/test_code_review.py tests/test_code_agent_workflow.py
git commit -m "feat: persist reviewed experiment evidence"
```

---

### Task 5: 冻结 final-test 与 phase inputs 穿透

**Files:**
- Modify: `src/athena/workflows/validate/ablation.py`
- Modify: `src/athena/workflows/prepare/baseline.py`
- Modify: `src/athena/workflows/search/search_loop.py`
- Modify: `src/main.py`
- Modify: `tests/test_validate_report.py`
- Modify: `tests/test_main_workflow.py`

**Interfaces:**
- Consumes: `CodeAgent.execute(..., inputs=validation_inputs)`。
- Produces: `CodeAgent.execute_frozen(experiment_id, hypothesis, plan, parent_commit, eval_spec, worktree, *, inputs: EvaluationInputs) -> CodegenResult`；`Validator(workspace, code_agent, eval_spec, validation_inputs, test_inputs)`；`_DeferredValidator` 同时持有两套输入。

- [ ] **Step 1: 写失败测试（final-test 冻结）**

在 `tests/test_validate_report.py` 顶部确保已有 import：`EvaluationInputs`、`CodegenResult`、`EvalResult`、`Path`、`pytest`。追加：

```python
def _dummy_result(experiment_id: str = "exp-x", commit: str = "c" * 40) -> CodegenResult:
    return CodegenResult(
        experiment_id=experiment_id,
        commit=commit,
        diff=f"sha256:{'0' * 64}",
        eval=EvalResult(
            experiment_id=experiment_id,
            primary=0.8,
            per_sample=f"sha256:{'1' * 64}",
        ),
        evaluation=f"sha256:{'2' * 64}",
        logs=f"sha256:{'3' * 64}",
    )


class RecordingTrustedAgent:
    def __init__(self, *, commit: str) -> None:
        self.commit = commit
        self.generated: list[str] = []
        self.frozen: list[str] = []

    async def execute(self, experiment_id, hypothesis, plan, parent_commit, eval_spec, worktree, *, inputs):
        self.generated.append(inputs.phase)
        return _dummy_result(experiment_id=experiment_id, commit=self.commit)

    async def execute_frozen(self, experiment_id, hypothesis, plan, parent_commit, eval_spec, worktree, *, inputs):
        self.frozen.append(inputs.phase)
        return _dummy_result(experiment_id=experiment_id, commit=parent_commit)


@pytest.mark.asyncio
async def test_validator_runs_ablation_on_validation_and_final_test_frozen(tmp_path) -> None:
    from athena.workflows.validate.ablation import Validator

    agent = RecordingTrustedAgent(commit="c" * 40)
    eval_spec = create_eval_spec("classification")
    validation_inputs = _inputs(tmp_path / "v")
    test_inputs = _inputs(tmp_path / "t", phase="test")
    validator = Validator(
        None, agent, eval_spec, validation_inputs=validation_inputs,
        test_inputs=test_inputs,
    )
    tree = _tree_with_sota(validation_inputs)  # 见下方辅助，构造含 SOTA 的树
    result = await validator.run("exp-sota", tree)

    assert agent.generated == ["validation", "validation"]
    assert agent.frozen == ["test"]
    assert tree.get_experiment(result.final_test_id).commit == "c" * 40
```

> 注：`_inputs(tmp_path, phase=...)` 与 `_tree_with_sota(...)` 为测试辅助：`_inputs` 按 Task 3 的 `_inputs()` 复制一份并接受 `phase` 参数；`_tree_with_sota` 构造含一个成功 SOTA 实验（`plan.kind="search"`、`eval` 非空、`status=SUCCEEDED`）及其两条 hypothesis 的 `ResearchTree`。若 `tests/test_validate_report.py` 已有等价辅助，则复用之。断言即规格要求：ablation 走 validation、final-test 走 frozen 且不改 commit。

- [ ] **Step 2: 运行测试验证 RED**

Run: `uv run pytest -q tests/test_validate_report.py -k final_test`
Expected: FAIL（`execute_frozen` 不存在）。

- [ ] **Step 3: 实现 `execute_frozen`**

`code_agent.py` 新增：

```python
    async def execute_frozen(
        self,
        experiment_id,
        hypothesis,
        plan,
        parent_commit,
        eval_spec,
        worktree,
        *,
        inputs: EvaluationInputs,
    ) -> CodegenResult:
        wt_path = Path(worktree.path)
        wt_path.mkdir(parents=True, exist_ok=True)
        started_at = time.time()
        log_path = wt_path / f"athena_logs_{experiment_id}.txt"
        logs_ref = f"artifact://{log_path}"

        await self._stage_inputs(wt_path, inputs)
        await self._ensure_evaluator(wt_path, eval_spec)
        if self._artifacts is None or self._evaluator is None:
            raise CodeExecutionError("trusted evaluation is not configured", logs=logs_ref)

        run_output = await self._runtime.run(
            ExecutionRequest(
                entrypoint=EXPERIMENT_ENTRYPOINT,
                cwd=wt_path,
                timeout_s=300,
                environment={
                    "ATHENA_EXPERIMENT_ID": experiment_id,
                    "ATHENA_PHASE": inputs.phase,
                },
                readonly_inputs=(inputs.features_path,),
            )
        )
        await self._write_logs(
            log_path,
            [("run_experiment.py", ProcessResult(
                returncode=run_output.returncode,
                output="\n".join(part for part in (run_output.stdout, run_output.stderr) if part),
            ))],
        )
        if run_output.returncode != 0:
            raise CodeExecutionError(
                "frozen final-test execution failed", logs=logs_ref
            )

        evaluation = await self._evaluator.evaluate(
            experiment_id, wt_path / "predictions.csv", inputs, eval_spec
        )
        evaluation_ref = await self._artifacts.put_bytes(
            await asyncio.to_thread((wt_path / "predictions.csv").read_bytes)
        )
        diff_ref = f"artifact://diffs/{experiment_id}"
        commit = parent_commit
        if self._workspace is not None:
            diff = await self._workspace.diff(worktree)
            diff_ref = diff.ref
            commit = await self._workspace.commit(worktree, diff, f"experiment: {experiment_id}")
        return CodegenResult(
            experiment_id=experiment_id,
            commit=commit,
            diff=diff_ref,
            eval=evaluation,
            evaluation=evaluation_ref,
            logs=logs_ref,
            wall_time_s=time.time() - started_at,
        )
```

- [ ] **Step 4: 线程穿透 phase inputs**

- `ablation.py`：`Validator.__init__(..., validation_inputs, test_inputs)`；`_execute` 对 ablation 调 `self._code_agent.execute(..., inputs=self._validation_inputs)`，对 final-test 调 `self._code_agent.execute_frozen(..., inputs=self._test_inputs)`；`run()` 依据 `branch_kind` 分流。
- `baseline.py`：`create_baseline(..., validation_inputs)` 传入 `code_agent.execute(..., inputs=validation_inputs)`。
- `search_loop.py`：构造时收 `validation_inputs`，`execute(..., inputs=self._validation_inputs)`。
- `main.py`：`prepare_baseline` 传 `prepared.validation_inputs`；`search_factory` 传 `prepared.validation_inputs`；`_DeferredValidator` 构造收 `validation_inputs` 与 `test_inputs`，`run()` 传给 `Validator`。

- [ ] **Step 5: 修复 `main.py` 破坏点（`_seed_repository` 与 `build_application`）**

`main.py` 三处修复：

(a) `_seed_repository` 的 ignore 清单加入 `.athena/`（防 phase inputs 携标签提交），并删除 `splits.json` 复制行：

```python
    ignore = "\n".join(
        (
            "predictions.csv",
            "labels.csv",
            "fold_ids.csv",
            "eval_result.json",
            "athena_logs_*.txt",
            ".athena/",
            "__pycache__/",
            "",
        )
    )
    await workspace.init(
        repo_path=repo,
        initial_file=".gitignore",
        initial_content=ignore,
    )
    shutil.copyfile(prepared.evaluator_path, repo / "eval.py")
    shutil.copyfile(prepared.eval_spec_path, repo / "eval_spec.json")
    await _git(repo, "add", "eval.py", "eval_spec.json")
    await _git(repo, "commit", "-m", "freeze evaluation protocol")
    return await _git(repo, "rev-parse", "HEAD")
```

(b) `build_application` 注入本地运行时与可信评估器，构造 `CodeAgent` 时传入：

```python
    from athena.code.execution import LocalExperimentRuntime
    from athena.evaluation.trusted import TrustedEvaluator

    runtime = LocalExperimentRuntime()
    evaluator = TrustedEvaluator(artifacts)
    code_agent = CodeAgent(
        backend=config.backend,
        backends=selected_backends,
        workspace=workspace,
        runtime=runtime,
        artifacts=artifacts,
        evaluator=evaluator,
    )
```

(c) `prepare_baseline`、`search_factory` 与 `_DeferredValidator` 按 Step 4 传递 phase inputs；`_DeferredValidator.__init__` 增 `validation_inputs`、`test_inputs` 两参数并在 `run()` 传给 `Validator(workspace, code_agent, eval_spec, validation_inputs, test_inputs)`。

- [ ] **Step 6: 运行 validation/report/runtime/main 测试**

Run: `uv run pytest -q tests/test_validate_report.py tests/test_main_workflow.py tests/test_research_runtime.py tests/test_prepare_workflow.py tests/test_search_workflow.py tests/test_e2e_ai4ml.py`
Expected: 除离线 E2E（见 Task 6）外全部 PASS。

- [ ] **Step 7: 提交**

```bash
git add src/athena/workflows/validate/ablation.py src/athena/workflows/prepare/baseline.py src/athena/workflows/search/search_loop.py src/athena/workflows/search/code_agent.py src/main.py tests/test_validate_report.py tests/test_main_workflow.py
git commit -m "feat: evaluate the frozen SOTA on final test"
```

---

### Task 6: CLI 预检、本地模式、离线 E2E 重写与文档

**Files:**
- Modify: `src/main.py`
- Modify: `src/athena/workflows/prepare/evaluator_factory.py`
- Modify: `tests/test_main_workflow.py`
- Modify: `tests/test_e2e_ai4ml.py`
- Modify: `README.md`
- Modify: `docs/README.md`

**Interfaces:**
- Consumes: `EvaluatorFactory` 支持的 task/metric/direction 目录。
- Produces: `RunConfig.execution: Literal["local"] = "local"`；摘要含 `execution` 与 `strong_isolation: false`；本地隔离警告；预检函数 `validate_config` 增强。

- [ ] **Step 1: 写失败测试（预检与本地模式）**

`tests/test_main_workflow.py` 追加：

```python
def test_parse_args_defaults_local_execution(tmp_path: Path) -> None:
    data = tmp_path / "data.csv"
    data.write_text("feature,label\n0,0\n1,1\n", encoding="utf-8")
    config = parse_args(["--data", str(data), "--target", "label", "--model", "m"])
    assert config.execution == "local"


def test_validate_config_rejects_unsupported_metric(tmp_path: Path) -> None:
    data = tmp_path / "data.csv"
    data.write_text("feature,label\n0,0\n1,1\n", encoding="utf-8")
    config = RunConfig(
        data=data, target="label", model="m", backend="qoder",
        output_dir=tmp_path / "run", metric="not_a_metric",
    )
    with pytest.raises(ValueError, match="unsupported"):
        validate_config(config)


def test_validate_config_rejects_target_missing_from_header(tmp_path: Path) -> None:
    data = tmp_path / "data.csv"
    data.write_text("feature,other\n0,0\n1,1\n", encoding="utf-8")
    config = RunConfig(
        data=data, target="label", model="m", backend="qoder",
        output_dir=tmp_path / "run",
    )
    with pytest.raises(ValueError, match="target column not found"):
        validate_config(config)
```

- [ ] **Step 2: 运行测试验证 RED**

Run: `uv run pytest -q tests/test_main_workflow.py -k "preflight or local or unsupported or target_missing"`
Expected: FAIL。

- [ ] **Step 3: 实现预检与本地模式**

- `RunConfig` 加 `execution: Literal["local"] = "local"`（不暴露 `--execution` 选择，仅记录本地模式；为兼容保留 argparse 可选 `--execution`，choices=("local",)，默认 "local"）。
- `evaluator_factory.py` 增加 `SUPPORTED_METRICS`（来自 `_DEFAULT_METRICS` 名称并集）与 `SUPPORTED_TASK_TYPES`，导出 `supported_metric(name)`。
- `validate_config` 在现有检查后追加：读 CSV 头确认 target 在列；`metric` 若给定则 `supported_metric` 校验，否则按 `_metric()` 默认名校验；对 target 非空计数 ≥5（0.2/0.2 比例下非空 train/validation/test）。
- `run()` 摘要增加 `"execution": config.execution, "strong_isolation": False`；`main()` 在 `execution == "local"` 时向 stderr 打印一次隔离警告。

- [ ] **Step 4: 重写离线 E2E 至可信契约**

`tests/test_e2e_ai4ml.py::test_main_composition_runs_prepare_search_validate_report_offline`：假后端改为读 `.athena/phase_manifest.json`、写 `run_experiment.py` 与辅助 `model.py`、写行 ID 键控 `predictions.csv`（两列 `__athena_row_id,prediction`，不含标签）；`build_application` 注入真 `LocalArtifactStore` + `TrustedEvaluator` + `LocalExperimentRuntime`。断言：4 个成功实验；`diff`/`logs`/`evaluation` 均 `sha256:` 或 `artifact://` 引用；SEARCH 提示词不含 test 路径；final-test 不调用后端且 commit 等于 SOTA commit；worktree 清理后证据从 store 可读。

- [ ] **Step 5: 运行 E2E 验证 GREEN**

Run: `uv run pytest -q tests/test_e2e_ai4ml.py::test_main_composition_runs_prepare_search_validate_report_offline`
Expected: PASS。

- [ ] **Step 6: 更新文档**

`README.md`：默认 local（非强隔离），预测 schema（`__athena_row_id,prediction`），辅助文件支持，phase-scoped inputs，冻结 SOTA final-test，退出码。`docs/README.md`：把收口设计链接加入索引，标注 main-workflow 设计状态为 implemented、trusted-execution 为 implemented。

- [ ] **Step 7: 全量验证**

Run: `uv run pytest -q tests test/unit`
Run: `uv run black --check src tests test/unit`
Run: `uv pip check`
Run: `uv run python src/main.py --help`
Run: 无效配置命令并确认退出码 2（无 traceback）
Run: `git diff --check`

- [ ] **Step 8: 审查并对齐规格**

对照 `2026-08-07-athena-local-workflow-closeout-design.md` 逐项核对；独立审查；修复 Critical/Important 后重跑 Step 7。

- [ ] **Step 9: 提交**

```bash
git add src/main.py src/athena/workflows/prepare/evaluator_factory.py tests/test_main_workflow.py tests/test_e2e_ai4ml.py README.md docs/README.md
git commit -m "feat: close the trusted Athena workflow loop"
```

---

### Task 7: Titanic 真实试跑

**Files:**
- Test: `examples/titanic/`
- Modify: `README.md`（示例命令）

**Interfaces:**
- Consumes: `src/main.py` CLI（local 模式、codex/qoder 后端、PydanticAI ideator 模型）。

- [ ] **Step 1: 确认示例数据就绪**

确认 `examples/titanic/train.csv`（含 `Survived` 列）、`test.csv`、`gender_submission.csv` 存在。`--data` 使用 `train.csv`（单文件契约，target=`Survived`）。

- [ ] **Step 2: 运行真实工作流**

```bash
uv run python src/main.py --data examples/titanic/train.csv --target Survived --model "$ATHENA_IDEATOR_MODEL" --backend codex --output-dir .athena/titanic-run --max-experiments 1 --max-no-improve 1
```

若 `ATHENA_IDEATOR_MODEL` 未设置，先提示用户配置（PydanticAI 模型标识，如 `openai:gpt-5` / deepseek 兼容端点）。预期：PREPARE → SEARCH（1 候选）→ VALIDATE（ablations + final-test）→ REPORT 全绿，`run_summary.json` 含 `execution: local` 与 `strong_isolation: false`，`reports/` 生成报告。

- [ ] **Step 3: 核验产物**

确认 `.athena/titanic-run/` 含 `research_tree.json`、`artifacts/objects`（`sha256:` diff/log/evaluation/predictions）、`experiment_repo` 提交链、`run_summary.json`、`reports/athena_report_*.md`。

- [ ] **Step 4: 记录试跑命令与结果到 README 示例节**

在 `README.md` 增加 Titanic 一行示例命令与产物清单。

- [ ] **Step 5: 提交**

```bash
git add examples/titanic README.md
git commit -m "docs: run and document the Titanic example"
```

---

### 自审检查

- **规格覆盖**：本地运行时（Task 1 基线 / Task 3 注入）✓；生成树策略与修复循环（Task 2–3）✓；完整 Git diff 与耐久证据（Task 4）✓；冻结 final-test（Task 5）✓；CLI 预检与本地模式（Task 6）✓；Titanic 验收（Task 7）✓；文档（Task 6/7）✓。
- **占位扫描**：无 TBD/TODO；每步含具体代码或命令。
- **类型一致**：`CodeAgent.execute` 与 `execute_frozen` 的 `inputs: EvaluationInputs` 关键字贯穿 baseline/search/ablation；`GitDiff(ref, paths)` 贯穿 workspace/git_workspace/code_agent；`CodegenResult.evaluation` 在 Task 3 定义、Task 4 复用；`GeneratedTreePolicy` 抛 `CodeExecutionError` 与既有 `CodeExecutionError(logs=...)` 签名相容（策略校验处用无 logs 调用，由 code_agent 包装补 logs，符合既有用法）。
- **Task 3 移除旧路径**：`execute` 不再有「无后端写失败 entrypoint」回退（`_ensure_experiment_entrypoint`）与「解析 eval_result.json」路径（`_load_evaluation`）——受信执行必须有后端并走 `TrustedEvaluator`。既有测试 `test_code_agent_runs_the_frozen_evaluator_source` 等须改写为注入后端的形态（Task 3 Step 7）。
- **Task 3 新增导入**：`code_agent.py` 需导入 `render_backend_prompt`（`athena.code.backends.base`）、`ExecutionRequest`/`LocalExperimentRuntime`（`athena.code.execution`）、`GeneratedTreePolicy`（`athena.code.file_policy`）、`EvaluationInputs`/`EvalResult`（`athena.evaluation.types`）、`shutil`。
- **Task 4 依赖审查**：`declared_dependencies` 传 `_installed_dependencies()`（当前解释器顶层包名），`review.py::_PROTECTED_FILES` 更新为 `{"eval.py", "eval_spec.json", ".gitignore"}`，避免 pandas/sklearn 误报。
