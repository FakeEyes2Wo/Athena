# Athena Main Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a runnable `src/main.py` that composes Athena's real PREPARE, SEARCH, VALIDATE, and REPORT workflow with Codex CLI and Qoder SDK code-generation backends.

**Architecture:** Keep `ResearchRuntime` as the control plane and inject one application-owned workflow context containing prepared data, frozen evaluation configuration, Git workspace, agent backends, validator, and reporter. Code generation is performed through the existing `CodeBackend` contract; every successful experiment is reviewed, committed, and recorded with real artifact references before the next phase starts.

**Tech Stack:** Python 3.11, asyncio, Pydantic/PydanticAI, pandas, Git worktrees, Codex CLI, `qoder-agent-sdk==1.0.12`, pytest.

## Global Constraints

- All workflow operations are asynchronous.
- Prompts and Pydantic descriptions remain English.
- Scoring and acceptance decisions use frozen deterministic rules and evidence.
- Missing backend configuration must fail explicitly; no fixed-code or fixed-hypothesis fallback is allowed.
- Dataset contents remain artifacts and are not committed to experiment Git history.
- Only `run_experiment.py` may be changed by code-generation backends.
- Qoder may use Read/Edit tools but not Bash.
- Failed and cancelled experiments retain logs and terminal state.

---

### Task 1: Install Qoder SDK And Implement Real Backend Adapters

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/athena/code/backends/codex.py`
- Modify: `src/athena/code/backends/qoder.py`
- Modify: `src/athena/code/backends/__init__.py`
- Test: `tests/test_code_backend.py`

**Interfaces:**
- Consumes: `CodeBackend.generate(prompt, target_dir, previous_outputs, history)`.
- Produces: `CodexBackend.from_config(config)`, `QoderBackend.from_config(config)`, and `load_backend(name, **config)` returning real adapters.

- [ ] **Step 1: Write failing adapter tests**

Add tests that inject an async process runner into `CodexBackend`, assert the command contains `codex exec --sandbox workspace-write --ephemeral -C <target>`, and verify non-zero exit codes raise `BackendUnavailableError`. Add a fake Qoder module exposing `query`, `QoderAgentOptions`, `qodercli_auth`, `AssistantMessage`, and `TextBlock`; verify `cwd`, `allowed_tools=["Read", "Edit"]`, `disallowed_tools=["Bash"]`, and `permission_mode="acceptEdits"`.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest -q tests/test_code_backend.py`

Expected: failures because Codex still imports a Python `codex.Client` and Qoder still expects `Client.generate()`.

- [ ] **Step 3: Add the dependency**

Run: `uv add "qoder-agent-sdk==1.0.12"`

Expected: `pyproject.toml` and `uv.lock` include the Windows-compatible Qoder SDK package.

- [ ] **Step 4: Implement Codex CLI generation**

Make `CodexBackend` launch:

```python
(
    "codex", "exec",
    "--sandbox", "workspace-write",
    "--ephemeral",
    "--color", "never",
    "-C", target_dir,
    prompt,
)
```

Snapshot files before and after the command and return the actual created and modified relative paths. Convert executable-not-found, timeout, and non-zero exit conditions into `BackendUnavailableError` with stderr context.

- [ ] **Step 5: Implement Qoder SDK generation**

Use `QoderAgentOptions(auth=..., cwd=Path(target_dir), allowed_tools=["Read", "Edit"], disallowed_tools=["Bash"], permission_mode="acceptEdits")` and consume `query(prompt=prompt, options=options)`. Select PAT authentication when `QODER_PERSONAL_ACCESS_TOKEN` exists; otherwise use `qodercli_auth()`. Collect assistant text and return actual created/modified paths from the same snapshot helper.

- [ ] **Step 6: Run adapter tests and dependency check**

Run: `uv run pytest -q tests/test_code_backend.py`

Run: `uv pip check`

Expected: all adapter tests pass and dependency resolution reports no conflicts.

- [ ] **Step 7: Commit backend integration**

Run: `git add pyproject.toml uv.lock src/athena/code/backends tests/test_code_backend.py && git commit -m "feat: connect Codex CLI and Qoder SDK backends"`

### Task 2: Make CodeAgent Generate, Review, Commit, And Return Real Evidence

**Files:**
- Modify: `src/athena/code/engine.py`
- Modify: `src/athena/workflows/search/code_agent.py`
- Modify: `src/athena/core/research_tree.py`
- Modify: `src/athena/workflows/prepare/baseline.py`
- Modify: `src/athena/workflows/search/search_loop.py`
- Modify: `src/athena/workflows/validate/ablation.py`
- Test: `tests/test_code_engine.py`
- Test: `tests/test_code_agent_assets.py`
- Test: `tests/test_prepare_workflow.py`
- Test: `tests/test_search_workflow.py`
- Test: `tests/test_validate_report.py`

**Interfaces:**
- Consumes: backend mapping `Mapping[str, CodeBackend]`, `GitWorkspace`, `GitWorkBranch`, `ExperimentPlan`, and frozen `EvalSpec`.
- Produces: `CodegenResult.commit` and `CodegenResult.diff` backed by a real approved Git commit/diff; `ResearchTree.complete_experiment(..., commit=...)`.

- [ ] **Step 1: Write failing CodeAgent lifecycle tests**

Create a fake backend that writes `run_experiment.py`, a recording Git workspace whose `diff()` and `commit()` return stable references, and assertions that generation precedes execution, protected file changes fail, execution failure triggers a bounded revision, and success returns the committed hash.

- [ ] **Step 2: Run targeted tests and verify RED**

Run: `uv run pytest -q tests/test_code_agent_assets.py tests/test_code_engine.py`

Expected: failures because `CodeAgent` does not accept backends/workspace, does not generate code, and returns placeholder diff/commit values.

- [ ] **Step 3: Add a fixed-entrypoint mode to CodeEngine**

Extend `CodeEngine.run()` with `entrypoint: str | None`. When supplied, execute exactly that relative path after each generation round and reject backend-reported paths outside the target directory. Preserve current first-generated-Python behavior when omitted.

- [ ] **Step 4: Integrate CodeAgent with backend routing**

Construct `CodeAgent(backends, workspace, backend="auto", max_rounds=3)`. For every experiment, select the named backend or use `CodeRouter`, render an English prompt containing hypothesis, plan, evaluation spec, split manifest, allowed file list, and previous failures, then run `CodeEngine` with `entrypoint="run_experiment.py"`.

- [ ] **Step 5: Enforce protected files and commit evidence**

Hash `eval.py`, `eval_spec.json`, and `splits.json` before generation and verify them afterward. Ignore runtime outputs through the base repository `.gitignore`. Call `workspace.diff()`, pass the textual changed paths through `review_diff(allowed_files={"run_experiment.py"}, ...)`, then call `workspace.commit()`. Return the approved diff artifact and commit hash.

- [ ] **Step 6: Record the committed hash in ResearchTree**

Add optional `commit` to `ResearchTree.complete_experiment`; default to the existing hash for compatibility. Pass `result.commit` from baseline, search, and validation workflows.

- [ ] **Step 7: Run workflow regression tests**

Run: `uv run pytest -q tests/test_code_engine.py tests/test_code_agent_assets.py tests/test_prepare_workflow.py tests/test_search_workflow.py tests/test_validate_report.py`

Expected: all targeted tests pass.

- [ ] **Step 8: Commit experiment evidence lifecycle**

Run: `git add src/athena/code src/athena/core/research_tree.py src/athena/workflows tests && git commit -m "feat: commit generated experiment evidence"`

### Task 3: Add Deterministic Dataset Preparation And Frozen Evaluator Assets

**Files:**
- Create: `src/athena/workflows/prepare/runtime.py`
- Modify: `src/athena/workflows/prepare/__init__.py`
- Test: `tests/test_prepare_runtime.py`

**Interfaces:**
- Consumes: `Path` to CSV, `TaskMetaData`, target column, artifact directory, split seed and ratios.
- Produces: immutable `PreparedWorkflowData(profile, processing_log, eval_spec, split_manifest_path, evaluator_path)`.

- [ ] **Step 1: Write failing preparation tests**

Cover missing CSV, missing target, empty data after dropping missing targets, deterministic split identity, disjoint split membership, numeric median fill, categorical missing-token fill, raw-copy preservation, and stable `DataProfile`/`ProcessingLog` artifacts.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest -q tests/test_prepare_runtime.py`

Expected: import failure because `athena.workflows.prepare.runtime` does not exist.

- [ ] **Step 3: Implement artifact-backed preparation**

Use `pandas.read_csv`, preserve original bytes, drop missing targets, fill numeric features with medians and categorical features with `"__MISSING__"`, call existing `deterministic_split`, and write content-addressed CSV files below `<output>/artifacts/data`.

- [ ] **Step 4: Generate frozen manifests**

Write `splits.json` with absolute train/validation/test paths and target name, plus `eval_spec.json` from `EvaluatorFactory.build()`. Generate a deterministic protected `eval.py` supporting `f1_macro`, `accuracy`, `rmse`, `mae`, and `mse` from `labels.csv` and `predictions.csv` without model-generated evaluation logic.

- [ ] **Step 5: Run preparation tests**

Run: `uv run pytest -q tests/test_prepare_runtime.py tests/test_data_operations.py tests/test_prepare_workflow.py`

Expected: all preparation tests pass.

- [ ] **Step 6: Commit deterministic preparation**

Run: `git add src/athena/workflows/prepare tests/test_prepare_runtime.py && git commit -m "feat: prepare frozen workflow data"`

### Task 4: Compose The Full Runtime In src/main.py

**Files:**
- Create: `src/main.py`
- Test: `tests/test_main_workflow.py`

**Interfaces:**
- Consumes CLI arguments: `--data`, `--target`, `--task-type`, `--metric`, `--direction`, `--model`, `--backend`, `--output-dir`, `--max-experiments`, `--max-no-improve`, `--hil`, `--debug`.
- Produces saved research tree, Git experiment repository/worktrees, artifacts, final Markdown report, JSON summary, and process exit status.

- [ ] **Step 1: Write failing CLI and composition tests**

Test parser defaults and validation, backend selection, prerequisite diagnostics, phase event rendering, tree save on success/failure, and an injected fake workflow that reaches `COMPLETED` in the order PREPARE, SEARCH, VALIDATE, REPORT.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest -q tests/test_main_workflow.py`

Expected: import failure because `src/main.py` does not exist.

- [ ] **Step 3: Implement CLI configuration and preflight**

Load `.env`, parse arguments with `argparse`, validate positive budgets and input paths, check Codex with `shutil.which("codex")`, import Qoder only when selected, and require `--model`/`ATHENA_IDEATOR_MODEL` for Ideator.

- [ ] **Step 4: Build application dependencies**

Prepare data, create `LocalArtifactStore`, initialize `LocalGitWorkspace` with protected evaluator/manifests and `.gitignore`, create PydanticAI agents for Ideator and Reporter, instantiate backend registry, `CodeAgent`, `Validator`, `Reporter`, and `ResearchWorkflowDependencies` closures.

- [ ] **Step 5: Drive the canonical phases**

Dispatch `TASK_CONFIGURE`, `SEARCH_START`, await `runtime.run_task`, require successful SEARCH completion, dispatch `VALIDATE_START`, then `REPORT_GENERATE`. Save the tree after SEARCH, VALIDATE, REPORT, and in the top-level exception handler.

- [ ] **Step 6: Emit machine- and human-readable results**

Print concise phase transitions to stderr and write `<output>/run_summary.json` containing phase, SOTA ID, tree path, report reference, budget, and backend. Return `0` only after REPORT completes; return `1` for workflow failures and `2` for invalid configuration.

- [ ] **Step 7: Run main workflow tests**

Run: `uv run pytest -q tests/test_main_workflow.py tests/test_research_runtime.py`

Expected: all entrypoint and runtime tests pass.

- [ ] **Step 8: Commit the application entrypoint**

Run: `git add src/main.py tests/test_main_workflow.py && git commit -m "feat: add Athena main workflow entrypoint"`

### Task 5: End-To-End Regression, Documentation, And Smoke Checks

**Files:**
- Modify: `tests/test_e2e_ai4ml.py`
- Modify: `README.md`
- Modify: `docs/README.md`

**Interfaces:**
- Consumes: injectable fake structured agent and fake code backend.
- Produces: offline evidence that the complete workflow composes correctly, plus runnable user instructions for real backends.

- [ ] **Step 1: Replace the partial smoke test with a full offline workflow test**

Use a temporary CSV and deterministic fake agents/backends to execute `main.run()` through baseline, one search candidate, all ablations, final test, report generation, Git commits, and tree persistence. Assert every successful experiment has a real commit, diff, logs, eval, and per-sample reference.

- [ ] **Step 2: Run the end-to-end test and verify RED before final wiring**

Run: `uv run pytest -q tests/test_e2e_ai4ml.py`

Expected: fail until every composition dependency from Tasks 1-4 is connected.

- [ ] **Step 3: Document real execution**

Document Qoder PAT/local-session authentication, Codex CLI authentication, required model configuration, command examples, output layout, HIL behavior, and explicit failure semantics. Mark `docs/design.md` historical and link the runnable entrypoint documentation from `docs/README.md`.

- [ ] **Step 4: Run formatting and full Python tests**

Run: `uv run black --check src tests test/unit`

Run: `uv run pytest -q tests test/unit`

Expected: Black exits 0 and the complete Python suite has zero failures.

- [ ] **Step 5: Run local smoke checks**

Run: `uv run python src/main.py --help`

Run: `uv run python src/main.py --data missing.csv --target label --model test --backend codex`

Expected: help exits 0; invalid input exits 2 with a concise diagnostic and no traceback.

- [ ] **Step 6: Review the final diff**

Run: `git diff --check`

Run: `git status --short`

Expected: no whitespace errors; only planned implementation, tests, dependency lock, and documentation files are modified.

- [ ] **Step 7: Commit end-to-end coverage and documentation**

Run: `git add tests/test_e2e_ai4ml.py README.md docs/README.md && git commit -m "docs: document runnable Athena workflow"`
