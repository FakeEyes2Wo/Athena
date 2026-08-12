# Supervisor VALIDATE TDD

> The original T11 feature is implemented. This packet now records its stable
> reference contract; the remaining output-cleanup simplification is executed
> by T11-I/T11-S in the dedicated restoration packet. Do not reopen completed
> behavior or modify `supervisor.py`/`research/runtime.py`.
> Read technical-design sections 3, 11, 16, and 17 only.

**Goal:** Add one stable `validate` Plan/Agent/workspace that evaluates frozen
SOTA, permits runtime-only repairs under independent review, and recovers a
trusted score logically exactly once.

## Ownership

- Create: `src/athena/agents/validate_agent.py`
- Create: `src/athena/core/agent/prompts/validate_agent.md`
- Create: `src/athena/research/supervisor/validation.py`
- Modify narrowly: `src/athena/research/validation.py`
- Modify narrowly: `src/athena/research/contracts.py`
- Create: `test/unit/agent/test_validate_agent.py`
- Create: `test/unit/research/supervisor/test_validation_plan.py`
- Create: `test/integration/research/test_validate_agent_contract.py`

`src/athena/core/workspace.py`, `src/athena/core/git_workspace.py`, and
`test/unit/test_git_workspace.py` belong exclusively to T11-I. Stop if its
public interface is absent after the coordinator declares T11-I complete.

## Existing Owners And Contracts

Use `Agent`, `load_prompt`, `AgentTypeRegistry`, `BaseAgentRunner`, generic
workspace tools, `ExecutionRuntime`, `LocalGitWorkspace`, `ArtifactStore`,
`TrustedEvaluator`, `ValidationService`, and its existing gap/tolerance.
Extend the existing `athena.research.contracts.ValidationResult`; do not define
another result model.

```python
def register_validate_agent(
    registry: AgentTypeRegistry,
    *, provider: object, artifacts: ArtifactStore, workspace: Path,
    runtime: ExecutionRuntime,
) -> None: ...


class ValidationInput(BaseModel):
    sota_commit: CommitHash
    reference_metric: float
    direction: Literal["maximize", "minimize"]
    final_evaluator_ref: ArtifactRef
    validation_key: str


class ValidationResult(BaseModel):  # existing model, narrowly extended
    # existing result_id/status/test/final/gap/warning remain
    sota_commit: CommitHash | None = None
    validation_commit: CommitHash | None = None
    predictions_ref: ArtifactRef | None = None
    evidence_ref: ArtifactRef | None = None


def validation_key(
    sota_commit: CommitHash,
    reference_metric: float,
    direction: Literal["maximize", "minimize"],
    final_evaluator_ref: ArtifactRef,
) -> str: ...


async def recovery_action(
    result_ref: ArtifactRef | None,
    store: ArtifactStore,
) -> Literal["run", "score", "commit"]: ...


class ValidationDiffReview(BaseModel):
    accepted: bool
    reason: str


async def review_validation_diff(
    *, workspace: GitWorkBranch, diff: GitDiff, explanation: str,
    independent_review: Callable[[str], Awaitable[ValidationDiffReview]],
) -> ValidationDiffReview: ...


async def run_validation_plan(
    *,
    input: ValidationInput,
    agents: AgentRuntime,
    git: GitWorkspace,
    workspace: GitWorkBranch,
    execution: ExecutionRuntime,
    evaluator: TrustedEvaluator,
    store: ArtifactStore,
    independent_review: Callable[[str], Awaitable[ValidationDiffReview]],
    result_ref: ArtifactRef | None,
    checkpoint: Callable[[ArtifactRef], Awaitable[None]],
    publish: EmitEvent | None = None,
) -> ValidationResult: ...
```

`validation_key` is SHA-256 over canonical JSON containing exactly the four
frozen inputs: SOTA commit, reference metric, direction, and final evaluator
ref. The final evaluator bundle is the sole frozen evaluation artifact consumed
by this phase. The validation commit is distinct from and never becomes SOTA.

## TDD

- [ ] **RED 1: stable key and artifact recovery**

  ```python
  def test_validation_key_is_stable_and_input_sensitive():
      first = validation_key("sota-a", 0.82, "maximize", "final-eval-a")
      assert first == validation_key("sota-a", 0.82, "maximize", "final-eval-a")
      assert first != validation_key("sota-b", 0.82, "maximize", "final-eval-a")


  @pytest.mark.asyncio
  async def test_complete_result_commits_without_rescoring(tmp_path):
      store = LocalArtifactStore(tmp_path / "artifacts")
      result_ref = await store.put_text(complete_result_json())
      assert await recovery_action(result_ref, store) == "commit"
  ```

  `complete_result_json()` is a test-local valid existing `ValidationResult`.

  Run: `uv run pytest test/unit/research/supervisor/test_validation_plan.py -q -p no:cacheprovider`

  Expected RED: target module absent.

- [ ] **GREEN 1:** Implement strict `ValidationInput`, canonical key,
  artifact-state recovery, and the narrow existing result extension. Reuse
  `ValidationService`; do not copy generalization math.

- [ ] **RED 2: deterministic diff policy**

  ```python
  @pytest.mark.asyncio
  @pytest.mark.parametrize("path", ["solution/model.py", "solution/features.py"])
  async def test_validation_rejects_semantic_changes(path, diff_for, reviewer):
      result = await review_validation_diff(
          workspace=diff_for.workspace,
          diff=diff_for(path),
          explanation="improve score",
          independent_review=reviewer,
      )
      assert result.accepted is False


  ```

  Add these named unit cases to `test_validation_plan.py`:

  ```text
  test_validation_allows_dependency_path_device_seed_and_serialization_repairs
  test_validation_rejects_architecture_feature_preprocessing_parameter_and_training_changes
  test_validation_rejects_final_label_access_before_llm_review
  test_validation_requires_agent_explanation
  test_validation_requires_independent_acceptance
  ```

  The deterministic checks reject known forbidden paths/content before calling
  the reviewer. Ambiguous diffs go to the injected independent LLM reviewer.

  Run:
  `uv run pytest test/unit/research/supervisor/test_validation_plan.py -q -p no:cacheprovider`

- [ ] **GREEN 2:** Implement the narrow diff/key/recovery functions and reuse
  `ValidationService` for the final gap and warning. Do not introduce another
  validation result or generic review service.

- [ ] **RED 3: Agent repair and exactly-once recovery**

  ```python
  @pytest.mark.asyncio
  async def test_crash_after_score_does_not_score_twice(harness):
      await harness.crash_after_score()
      await harness.resume()
      assert harness.evaluator_calls == 1
      assert harness.result.validation_commit != harness.result.sota_commit
  ```

  Add named integration cases:

  ```text
  test_validate_uses_stable_validate_agent_plan_and_workspace
  test_rejected_diff_returns_feedback_to_same_agent
  test_predictions_without_score_continue_at_scoring
  test_partial_or_invalid_output_reruns_under_same_key
  test_complete_result_commits_without_execution_or_scoring
  test_validation_never_changes_research_tree_sota
  ```

  Run: `uv run pytest test/unit/agent/test_validate_agent.py test/integration/research/test_validate_agent_contract.py -q -p no:cacheprovider`

- [ ] **GREEN 3:** Follow `register_supervisor_agent`: production passes
  `ResponsesProvider`; tests inject a provider. Require Agent explanation,
  independent LLM review, deterministic leakage checks, and reviewed Git commit.
  Human may supply environment repair information but may not tune semantics.

  `run_validation_plan` uses the injected `GitWorkspace` for diff and commit.
  It passes every `ExperimentManifest.outputs` path to
  `GitWorkspace.restore_paths` in a `finally` block after execution. It must not
  keep an in-memory byte snapshot or implement tracked/untracked Git decisions.
  It owns the Agent repair loop and artifact-key recovery but never receives or
  mutates `ResearchState` or `ResearchTree`. The Supervisor alone records phase
  completion and validation metadata.

- [ ] **Regression**

  ```powershell
  uv run pytest test/unit/agent/test_validate_agent.py test/unit/research/supervisor/test_validation_plan.py test/integration/research/test_validate_agent_contract.py test/unit/research/test_validation.py test/unit/research/test_services.py -q -p no:cacheprovider
  ```

  The integration slice must include these observable cases:

  ```text
  normal generated predictions do not alter the reviewed diff
  a pre-existing reviewed predictions file is restored after execution
  an execution-only predictions file is removed after execution
  a source mutation remains visible and is rejected before scoring
  recovery does not execute, score, or commit a second time
  ```

- [ ] Tests use real ArtifactStore/temp Git/workspace and AgentRuntime. Fake only
  provider/reviewer/evaluator external outcomes. Crash recovery reconstructs
  production objects over the same `tmp_path`.

- [ ] Return exact RED/GREEN output, reused interfaces, and files to `/root`.
  Proposed commit: `feat: add independent validation plan`.

Fresh RED, GREEN, and the regression command complete Task 11. Provider call
count is not a domain contract; assert stable identity and externally visible
execution/review/evaluator/commit effects instead. The independent
LLM diff decision is runtime product behavior; it is not a post-implementation
review gate.
