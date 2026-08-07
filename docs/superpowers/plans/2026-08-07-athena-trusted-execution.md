# Athena Trusted Experiment Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the runnable Athena workflow produce trusted, durable evaluation evidence with local execution by default, optional Docker isolation, phase-scoped data, and an unchanged SOTA final-test.

**Architecture:** PREPARE creates row-ID keyed train/features/labels artifacts and passes phase-specific `EvaluationInputs` objects through the workflow. `CodeAgent` owns one bounded generate-execute-evaluate loop using an injected execution runtime and trusted host evaluator; final-test uses a separate frozen execution path with no backend call. Complete Git diffs and all runtime evidence are persisted in `LocalArtifactStore` before ResearchTree records success.

**Tech Stack:** Python 3.11, asyncio, pandas, scikit-learn, Pydantic, Git worktrees, Codex CLI, `qoder-agent-sdk==1.0.12`, Docker CLI, pytest.

## Global Constraints

- Default execution is `local`; only `docker` may claim strong isolation.
- Local experiment subprocesses receive an explicit environment allowlist and no provider credentials or `.env` values.
- Generated code may create, modify, or delete auxiliary regular files inside the worktree; `run_experiment.py` must exist at the end.
- `.git/`, `.gitignore`, evaluator/config files, phase inputs, and runtime outputs are protected from generated changes.
- Generated code writes only `__athena_row_id,prediction`; labels remain private to Athena's evaluator.
- Baseline, SEARCH, and ablation use validation data. Only frozen final-test execution receives test features.
- Final-test never calls a code backend and records the exact SOTA commit.
- No experiment subprocess may install dependencies or use an agent credential.
- Every production behavior change follows RED -> GREEN -> regression verification.
- Preserve unrelated worktree changes. Fold the existing runner, Git ignore, prompt, documentation, and E2E edits into the matching tasks rather than reverting them.

---

### Task 1: Create Phase-Scoped Prepared Data And Trusted Evaluation

**Files:**
- Modify: `src/athena/evaluation/types.py`
- Create: `src/athena/evaluation/trusted.py`
- Modify: `src/athena/evaluation/factory.py`
- Modify: `src/athena/workflows/prepare/runtime.py`
- Modify: `src/athena/workflows/prepare/__init__.py`
- Modify: `tests/test_prepare_runtime.py`
- Create: `tests/test_trusted_evaluation.py`
- Modify: `tests/test_evaluation.py`

**Interfaces:**
- Produces `EvaluationInputs(phase, train_path, features_path, labels_path, target, row_id_column)` as a frozen Pydantic model in `athena.evaluation.types`.
- Produces `TrustedEvaluator(artifacts).evaluate(experiment_id, predictions_path, inputs, eval_spec) -> EvalResult`.
- Extends `PreparedWorkflowData` with `validation_inputs` and `test_inputs`; removes the all-splits manifest from the experiment-facing contract.
- Keeps `EvalSpec` and the catalog metric names compatible with existing ResearchTree and comparator callers.

- [ ] **Step 1: Write failing preparation tests for row IDs and private labels**

Add assertions that prepared train data contains `__athena_row_id` and the target, validation/test feature files contain the row ID but not the target, private label files contain exactly the row ID and target, IDs are disjoint and complete, and all three partitions are non-empty.

```python
validation = prepared.validation_inputs
validation_features = pd.read_csv(validation.features_path)
validation_labels = pd.read_csv(validation.labels_path)

assert list(validation_labels.columns) == ["__athena_row_id", "label"]
assert "label" not in validation_features.columns
assert validation.row_id_column == "__athena_row_id"
assert set(validation_features["__athena_row_id"]) == set(
    validation_labels["__athena_row_id"]
)
```

- [ ] **Step 2: Run preparation tests and verify RED**

Run: `uv run pytest -q tests/test_prepare_runtime.py`

Expected: FAIL because `PreparedWorkflowData` has no phase inputs and the split files still expose targets.

- [ ] **Step 3: Implement phase-scoped preparation**

Add the exact model shape:

```python
class EvaluationInputs(BaseModel):
    model_config = {"frozen": True}

    phase: Literal["validation", "test"]
    train_path: Path
    features_path: Path
    labels_path: Path
    target: str
    row_id_column: str = "__athena_row_id"
```

In `_prepare`, reject source data already containing `__athena_row_id`, assign stable IDs with `range(len(frame))`, split after assignment, keep target only in train, and write separate features/labels artifacts for validation and test. Raise `ValueError("dataset must produce non-empty train, validation, and test splits")` before writing config files when any partition is empty.

- [ ] **Step 4: Write failing trusted-evaluator contract tests**

Cover a correct shuffled prediction file, duplicate IDs, missing IDs, extra IDs, null IDs, unexpected columns, a generated `labels.csv` containing forged values, and non-finite primary metrics. The forged labels test must place `labels.csv` beside predictions and prove the score still comes from `EvaluationInputs.labels_path`.

```python
result = await evaluator.evaluate(
    "exp-1",
    tmp_path / "predictions.csv",
    prepared.validation_inputs,
    prepared.eval_spec,
)

assert result.primary < 1.0
assert result.per_sample.startswith("sha256:")
```

- [ ] **Step 5: Run trusted-evaluator tests and verify RED**

Run: `uv run pytest -q tests/test_trusted_evaluation.py`

Expected: FAIL with an import error for `athena.evaluation.trusted`.

- [ ] **Step 6: Implement host-owned metric evaluation**

Move metric dispatch into a reusable `metric_value(name, y_true, y_pred) -> float` function in `athena.evaluation.factory`. Implement `TrustedEvaluator` to load only the supplied predictions and private labels, enforce exact columns and ID-set equality, merge one-to-one by row ID, compute primary/secondary metrics, reject non-finite values, store the canonical aligned prediction CSV through `ArtifactStore.put_bytes`, and return its `sha256:` reference as `EvalResult.per_sample`.

- [ ] **Step 7: Run focused and preparation regressions**

Run: `uv run pytest -q tests/test_trusted_evaluation.py tests/test_prepare_runtime.py tests/test_evaluation.py tests/test_data_operations.py`

Expected: all selected tests pass.

- [ ] **Step 8: Commit trusted evaluation inputs**

```bash
git add src/athena/evaluation src/athena/workflows/prepare tests/test_prepare_runtime.py tests/test_trusted_evaluation.py tests/test_evaluation.py
git commit -m "feat: add trusted phase-scoped evaluation"
```

### Task 2: Add Local And Docker Experiment Execution Runtimes

**Files:**
- Create: `src/athena/code/execution.py`
- Modify: `src/athena/code/runner.py`
- Create: `tests/test_experiment_execution.py`
- Modify: `tests/test_code_runner.py`
- Create: `docker/experiment.Dockerfile`

**Interfaces:**
- Produces `ExecutionRequest(entrypoint, cwd, timeout_s, environment, readonly_inputs)`.
- Produces `ExperimentRuntime.run(request) -> ExecutionOutput` and `ExperimentRuntime.preflight() -> None`.
- Produces `LocalExperimentRuntime(base_environment=None)` and `DockerExperimentRuntime(image, executable="docker", memory="2g", cpus="2", pids_limit=256)`.

- [ ] **Step 1: Write failing local-runtime tests**

Test that the active `sys.executable` is used, only `PATH`, `SYSTEMROOT`, `WINDIR`, `TEMP`, `TMP`, and explicitly supplied `ATHENA_*` variables can reach the child, a sentinel `OPENAI_API_KEY` cannot reach it, cwd is fixed, and timeout returns a failed `ExecutionOutput`.

```python
result = await runtime.run(
    ExecutionRequest(
        entrypoint="run_experiment.py",
        cwd=tmp_path,
        timeout_s=10,
        environment={"ATHENA_SPLIT_MANIFEST": ".athena/phase_manifest.json"},
        readonly_inputs=(),
    )
)

assert result.returncode == 0
assert "SECRET_PRESENT" not in result.stdout
```

- [ ] **Step 2: Run local-runtime tests and verify RED**

Run: `uv run pytest -q tests/test_experiment_execution.py::test_local_runtime_uses_allowlisted_environment tests/test_code_runner.py`

Expected: FAIL because `athena.code.execution` does not exist.

- [ ] **Step 3: Implement the runtime protocol and local runtime**

Use `asyncio.create_subprocess_exec(sys.executable, entrypoint, cwd=cwd, env=allowlisted_env)` and the existing timeout/stream capture behavior. Resolve the entrypoint under cwd and reject traversal. Keep `run_script` as a compatibility wrapper around `LocalExperimentRuntime`, preserving the existing active-interpreter regression fix.

- [ ] **Step 4: Write failing Docker command and preflight tests**

Inject a fake async runner and assert the execution command contains `run --rm --network none --read-only --user 65532:65532 --memory 2g --cpus 2 --pids-limit 256`, a writable worktree mount, explicit read-only input mounts, and no credential environment values. Test missing executable, unavailable daemon, and missing image diagnostics.

- [ ] **Step 5: Run Docker tests and verify RED**

Run: `uv run pytest -q tests/test_experiment_execution.py -k docker`

Expected: FAIL because `DockerExperimentRuntime` is missing.

- [ ] **Step 6: Implement Docker runtime and image definition**

Build commands as argument tuples without shell interpolation. `preflight()` runs `docker info` and `docker image inspect <image>`. Execution mounts cwd at `/workspace`, overlays each phase input read-only below `/workspace/.athena/inputs`, uses `/workspace` as cwd, and invokes the image's Python on `run_experiment.py`. The Dockerfile uses Python 3.11, installs the frozen project environment with `uv sync --frozen --no-dev`, creates UID/GID 65532, and sets the virtual-environment Python as entrypoint.

- [ ] **Step 7: Run runtime tests**

Run: `uv run pytest -q tests/test_experiment_execution.py tests/test_code_runner.py`

Expected: all runtime tests pass without requiring a live Docker daemon.

- [ ] **Step 8: Commit execution runtimes**

```bash
git add src/athena/code/execution.py src/athena/code/runner.py tests/test_experiment_execution.py tests/test_code_runner.py docker/experiment.Dockerfile
git commit -m "feat: add local and Docker experiment runtimes"
```

### Task 3: Rebuild Code Generation Around Feedback And A Generated-Tree Policy

**Files:**
- Modify: `src/athena/code/types.py`
- Modify: `src/athena/code/backends/base.py`
- Modify: `src/athena/code/backends/codex.py`
- Modify: `src/athena/code/backends/qoder.py`
- Create: `src/athena/code/file_policy.py`
- Modify: `src/athena/workflows/search/code_agent.py`
- Modify: `tests/test_code_backend.py`
- Modify: `tests/test_code_agent_workflow.py`
- Modify: `tests/test_code_engine.py`

**Interfaces:**
- Extends `GenerationResult` with `files_deleted: list[str]`.
- Produces `GeneratedTreePolicy.validate(root, changed_paths) -> None`.
- Changes `CodeAgent.execute(..., worktree, inputs: EvaluationInputs) -> CodegenResult` to use injected `ExperimentRuntime`, `TrustedEvaluator`, and `ArtifactStore`.
- Produces `CodeAgent.execute_frozen(..., worktree, inputs: EvaluationInputs) -> CodegenResult` for Task 5.

- [ ] **Step 1: Write failing backend feedback and deletion tests**

For Codex, assert a second-round command prompt contains bounded prior stderr and JSON history. For Qoder, assert the second `query()` prompt contains the same repair context. Delete an existing auxiliary file in each fake backend and assert `files_deleted` reports it.

```python
assert "Previous execution feedback" in observed_prompt
assert "model failed on validation" in observed_prompt
assert result.files_deleted == ["old_model.py"]
```

- [ ] **Step 2: Run backend tests and verify RED**

Run: `uv run pytest -q tests/test_code_backend.py`

Expected: FAIL because adapters discard feedback and snapshots do not report deletions.

- [ ] **Step 3: Implement bounded repair context and complete snapshots**

Add `render_backend_prompt(prompt, previous_outputs, history)` in `backends/base.py`. Include at most the last three rounds and the last 4000 characters of stdout/stderr per round. Make both adapters send the rendered prompt. Extend `generation_result` to report `before.keys() - after.keys()` as deleted paths.

- [ ] **Step 4: Write failing generated-tree policy tests**

Cover multiple auxiliary files, deletion of an auxiliary file, a missing `run_experiment.py`, edits/deletions of `.gitignore`, `eval.py`, `eval_spec.json`, `.athena/phase_manifest.json`, and runtime outputs, plus a symlink resolving outside the worktree.

```python
policy.validate(root, {"run_experiment.py", "models/stack.py", "config.json"})

with pytest.raises(CodeExecutionError, match="protected path changed"):
    policy.validate(root, {"run_experiment.py", "eval.py"})
```

- [ ] **Step 5: Run policy tests and verify RED**

Run: `uv run pytest -q tests/test_code_agent_workflow.py -k "auxiliary or protected or symlink"`

Expected: FAIL because current CodeAgent permits only `run_experiment.py` and has no deletion/symlink policy.

- [ ] **Step 6: Implement phase staging and generated-tree policy**

Stage copied inputs under `.athena/inputs/train.csv` and `.athena/inputs/predict.csv`; write `.athena/phase_manifest.json` using relative paths and no labels. Hash all protected files before every backend round and verify them afterward. Permit regular auxiliary files anywhere below the worktree except protected/runtime paths. Require `run_experiment.py`, reject external symlinks, and update the generation prompt to describe the two-column prediction contract and allowed auxiliary tree.

- [ ] **Step 7: Write failing one-loop repair tests**

Use a fake backend that first writes predictions with a missing ID and then repairs them after receiving evaluation feedback. Assert the entrypoint runs once per round, trusted evaluation runs once per successful round, the backend receives prior output/history, and the old duplicate execution path is gone.

- [ ] **Step 8: Run repair-loop tests and verify RED**

Run: `uv run pytest -q tests/test_code_agent_workflow.py -k repair`

Expected: FAIL because trusted evaluation is currently outside `CodeEngine` and adapters receive no evaluator failure.

- [ ] **Step 9: Implement the bounded generate-execute-evaluate loop**

Move experiment-specific iteration into `CodeAgent`: call backend, validate the tree, call the injected runtime once, then call `TrustedEvaluator`. Convert execution and evaluation failures to `ExecutionOutput`, append bounded history, persist current logs, and retry up to `max_rounds`. Keep `CodeEngine` as the generic compatibility engine and update its tests only for the new `GenerationResult.files_deleted` field.

- [ ] **Step 10: Run backend and CodeAgent regressions**

Run: `uv run pytest -q tests/test_code_backend.py tests/test_code_agent_workflow.py tests/test_code_engine.py`

Expected: all selected tests pass.

- [ ] **Step 11: Commit the trusted generation loop**

```bash
git add src/athena/code src/athena/workflows/search/code_agent.py tests/test_code_backend.py tests/test_code_agent_workflow.py tests/test_code_engine.py
git commit -m "feat: enforce trusted generated experiment trees"
```

### Task 4: Review Complete Git Diffs And Persist Durable Evidence

**Files:**
- Modify: `src/athena/code/review.py`
- Modify: `src/athena/core/workspace.py`
- Modify: `src/athena/git_workspace.py`
- Modify: `src/athena/workflows/search/code_agent.py`
- Modify: `test/unit/test_git_workspace.py`
- Modify: `tests/test_code_review.py`
- Modify: `tests/test_code_agent_workflow.py`
- Modify: `src/athena/workflows/prepare/baseline.py`
- Modify: `src/athena/workflows/search/search_loop.py`

**Interfaces:**
- Produces `GitDiff(ref, paths)` from `GitWorkspace.diff(workspace)`.
- Changes `GitWorkspace.commit(workspace, approved_diff: GitDiff, message) -> CommitHash`.
- Extends `CodegenResult` with `evaluation: ArtifactRef`; `logs`, `diff`, `evaluation`, and `eval.per_sample` are all `sha256:` references.

- [ ] **Step 1: Write failing Git deletion and ignore tests**

Assert `GitDiff.paths` includes additions, modifications, and deletions, excludes `.gitignore`-matched runtime outputs, and the commit rejects any index/tree change after review. Retain the existing active fix that uses `git ls-files --others --exclude-standard`.

- [ ] **Step 2: Run Git tests and verify RED**

Run: `uv run pytest -q test/unit/test_git_workspace.py`

Expected: FAIL because `diff()` returns only an artifact reference and exposes no reviewed path set.

- [ ] **Step 3: Implement structured complete diffs**

After `git add -A`, obtain changed paths with `git diff --cached --name-only -z <base_commit>` and decode NUL-separated UTF-8 paths. Return a frozen `GitDiff` containing the content-addressed binary diff reference and sorted paths. Preserve reviewed staged hash, tree hash, and HEAD checks in `commit()`.

- [ ] **Step 4: Write failing semantic review tests**

Assert auxiliary paths are approved, protected/runtime paths and missing `run_experiment.py` are rejected, and deletion headers are not missed. Remove the old assumption that only `run_experiment.py` is allowed.

- [ ] **Step 5: Run review tests and verify RED**

Run: `uv run pytest -q tests/test_code_review.py tests/test_code_agent_workflow.py -k review`

Expected: FAIL because CodeAgent does not invoke semantic diff review.

- [ ] **Step 6: Integrate review and durable evidence**

Read the binary diff from `ArtifactStore`, review `GitDiff.paths` plus the final tree policy, and commit only an approved unchanged diff. Store UTF-8 logs and canonical evaluation JSON using `put_text`; use the `TrustedEvaluator` prediction reference for per-sample evidence. Attach `evaluation` alongside `diff` and `logs` in baseline and SEARCH lifecycle calls.

- [ ] **Step 7: Prove evidence survives worktree removal**

Add an integration test that executes an experiment, records its four evidence references, removes the worktree, and reads diff/logs/evaluation/predictions back from `LocalArtifactStore`.

- [ ] **Step 8: Run Git and lifecycle regressions**

Run: `uv run pytest -q test/unit/test_git_workspace.py tests/test_code_review.py tests/test_code_agent_workflow.py tests/test_prepare_workflow.py tests/test_search_workflow.py`

Expected: all selected tests pass.

- [ ] **Step 9: Commit durable reviewed evidence**

```bash
git add src/athena/code/review.py src/athena/core/workspace.py src/athena/git_workspace.py src/athena/workflows test/unit/test_git_workspace.py tests/test_code_review.py tests/test_code_agent_workflow.py tests/test_prepare_workflow.py tests/test_search_workflow.py
git commit -m "feat: persist reviewed experiment evidence"
```

### Task 5: Execute Final-Test From The Exact SOTA Commit

**Files:**
- Modify: `src/athena/workflows/validate/ablation.py`
- Modify: `src/main.py`
- Modify: `tests/test_validate_report.py`
- Modify: `tests/test_main_workflow.py`
- Modify: `tests/test_code_agent_workflow.py`

**Interfaces:**
- `Validator` consumes both `validation_inputs` and `test_inputs`.
- Ablations call `CodeAgent.execute(..., inputs=validation_inputs)`.
- Final-test calls `CodeAgent.execute_frozen(..., inputs=test_inputs)` and never calls `CodeBackend.generate()`.

- [ ] **Step 1: Write failing final-test freeze tests**

Record backend calls and assert ablations invoke generation while final-test does not. Assert final-test worktree base, recorded commit, and post-execution HEAD all equal `sota.commit`; assert the final phase manifest names `test` and no validation label/path.

```python
assert agent.generated_kinds == ["ablation", "ablation"]
assert agent.frozen_kinds == ["final-test"]
assert tree.get_experiment(result.final_test_id).commit == tree.get_experiment(sota_id).commit
```

- [ ] **Step 2: Run validation tests and verify RED**

Run: `uv run pytest -q tests/test_validate_report.py -k final_test`

Expected: FAIL because final-test currently calls the same generating `execute()` path.

- [ ] **Step 3: Implement frozen execution**

`execute_frozen` stages protected test inputs, verifies the committed generated tree, runs the existing entrypoint once through `ExperimentRuntime`, performs trusted test evaluation, stores durable evidence, obtains an empty reviewed Git diff, and returns the unchanged parent commit. It never retries or calls a backend.

- [ ] **Step 4: Thread phase inputs through all workflow owners**

Pass `prepared.validation_inputs` into baseline and `SearchLoop`; construct the deferred Validator with both prepared input objects; update `Validator` to use validation inputs for ablation and test inputs only for final-test. Do not place test inputs in CodeAgent constructor or SEARCH prompts.

- [ ] **Step 5: Run validation, report, and runtime tests**

Run: `uv run pytest -q tests/test_validate_report.py tests/test_main_workflow.py tests/test_research_runtime.py tests/test_prepare_workflow.py tests/test_search_workflow.py`

Expected: all selected tests pass and REPORT still requires exact ablation coverage plus one successful final-test.

- [ ] **Step 6: Commit frozen final-test semantics**

```bash
git add src/main.py src/athena/workflows/validate/ablation.py src/athena/workflows/prepare/baseline.py src/athena/workflows/search/search_loop.py tests/test_validate_report.py tests/test_main_workflow.py tests/test_code_agent_workflow.py tests/test_prepare_workflow.py tests/test_search_workflow.py
git commit -m "feat: evaluate the frozen SOTA on final test"
```

### Task 6: Add CLI Preflight, Docker Configuration, Documentation, And Full E2E Proof

**Files:**
- Modify: `src/main.py`
- Modify: `src/athena/workflows/prepare/evaluator_factory.py`
- Modify: `tests/test_main_workflow.py`
- Modify: `tests/test_e2e_ai4ml.py`
- Modify: `README.md`
- Modify: `docs/README.md`
- Modify: `docs/superpowers/specs/2026-08-07-athena-main-workflow-design.md`

**Interfaces:**
- Extends `RunConfig` with `execution: Literal["local", "docker"] = "local"` and `docker_image: str = "athena-experiment:local"`.
- Exposes the supported task/metric/direction catalog from `EvaluatorFactory` for preflight.
- Adds `execution` and `strong_isolation` to `run_summary.json`.

- [ ] **Step 1: Write failing CLI/preflight tests**

Cover parser defaults and Docker overrides; unsupported task/metric/direction; target missing from the CSV header; fewer than five non-null target rows with 0.2 validation/test ratios; local isolation warning; Docker preflight before output/repository creation; and summary isolation fields.

```python
assert config.execution == "local"
assert config.docker_image == "athena-experiment:local"

with pytest.raises(ValueError, match="non-empty train, validation, and test"):
    validate_config(config)
```

- [ ] **Step 2: Run CLI tests and verify RED**

Run: `uv run pytest -q tests/test_main_workflow.py`

Expected: FAIL because execution settings and complete preflight do not exist.

- [ ] **Step 3: Implement CLI runtime selection and preflight**

Add both options to argparse. Validate CSV header/usable target count and supported metric/direction before `build_application()` creates output directories. Construct `LocalExperimentRuntime` by default or `DockerExperimentRuntime` after successful preflight. Print one local-mode warning and include exact isolation fields in the summary.

- [ ] **Step 4: Rewrite the offline full-workflow test to obey the trusted contract**

The fake backend may create `run_experiment.py` plus an auxiliary `model.py`; it must read `.athena/phase_manifest.json` and write row-ID keyed predictions without labels. Assert four successful experiments, durable `sha256:` diff/log/evaluation/prediction references, final-test backend call absence, unchanged final commit, no test path in generated SEARCH prompts, and readable evidence after worktree cleanup.

- [ ] **Step 5: Run the E2E test and verify GREEN only after full wiring**

Run: `uv run pytest -q tests/test_e2e_ai4ml.py::test_main_composition_runs_prepare_search_validate_report_offline`

Expected: PASS through PREPARE -> SEARCH -> VALIDATE -> REPORT with trusted phase evidence.

- [ ] **Step 6: Update runnable documentation**

Document local as the default non-strong-isolation mode, Docker image build and `--execution docker`, the exact prediction schema, auxiliary-file support, phase-scoped inputs, unchanged SOTA final-test, Qoder/Codex authentication, output artifacts, and failure codes. Update the original main-workflow design status with a link to the trusted-execution design rather than rewriting historical decisions.

- [ ] **Step 7: Run complete verification**

Run: `uv run pytest -q tests test/unit`

Run: `uv run black --check src tests test/unit`

Run: `uv pip check`

Run: `uv run python src/main.py --help`

Run the invalid configuration command and capture `$LASTEXITCODE`; expected Athena exit code is 2.

Run: `git diff --check`

Expected: all tests pass, changed Python files are Black-clean, dependencies are compatible, help exits 0, invalid configuration exits 2 without a traceback, and no whitespace errors remain. If full Black still reports unrelated baseline files, record the exact pre-existing paths and run `black --check` on every changed Python file.

- [ ] **Step 8: Review the complete implementation against the trusted design**

Verify every requirement in `docs/superpowers/specs/2026-08-07-athena-trusted-execution-design.md`, request an independent code review, fix all Critical/Important findings, and rerun Step 7 after the final change.

- [ ] **Step 9: Commit workflow closeout**

```bash
git add src/main.py src/athena/workflows/prepare/evaluator_factory.py tests/test_main_workflow.py tests/test_e2e_ai4ml.py README.md docs/README.md docs/superpowers/specs/2026-08-07-athena-main-workflow-design.md
git commit -m "feat: close the trusted Athena workflow loop"
```
