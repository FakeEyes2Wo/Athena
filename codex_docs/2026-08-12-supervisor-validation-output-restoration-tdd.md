# Validation Output Restoration Implementation Plan

> **For agentic workers:** Execute this packet directly with strict RED ->
> GREEN -> regression. The coordinator must record one Agent claim before any
> edit. Fresh evidence completes the task; no review Agent or review phase is
> required.
> **Required method:** Use the repository's `test-driven-development` skill;
> write and observe each behavioral RED before changing production code.
> **Design context:** Read technical-design sections 3, 11, and 17 only.

**Goal:** Simplify completed VALIDATE output cleanup by moving reviewed-tree
restoration to its existing Git owner, without changing accepted behavior.

**Architecture:** Extend the existing `GitWorkspace` owner with one path-scoped
restore operation. `LocalGitWorkspace` restores each declared output from the
last reviewed tree when tracked there and removes it when absent there. VALIDATE
then uses this public operation instead of maintaining a second file snapshot.

**Tech Stack:** Python 3.11+, asyncio, Git, pytest, existing
`GitWorkspace`/`LocalGitWorkspace`.

## Global Constraints

- Work on `main`; do not stage or commit because `/root` serializes both.
- Own only the files listed below. Stop on any ownership collision.
- Adopt existing uncommitted changes only after the named RED reproduces on a
  stable snapshot. Never manufacture a RED for behavior that already passes.
- Do not add a workspace manager, validation helper, repository wrapper, shell
  subprocess outside `LocalGitWorkspace`, or compatibility overload.
- `restore_paths` accepts workspace-relative output file paths only. Reject an
  absolute path, `..` traversal, an unregistered workspace, or a missing review.
- A valid RED is a behavioral assertion failure. Import, syntax, fixture, and
  concurrent file-replacement failures do not count.

## Ownership And Dependency

**Task ID:** `T11-I`

**Files:**

- Modify: `src/athena/core/workspace.py`
- Modify: `src/athena/core/git_workspace.py`
- Test: `test/unit/test_git_workspace.py`

This task is independent of SEARCH A.1 and may run concurrently with it. It
starts after the current T11 feature commit is archived and must commit before
the T11-S consumer task below. The T11-I Agent must not edit
`supervisor/validation.py` or VALIDATE tests.

## Existing Infrastructure Audit

Before editing, record this mapping in the completion package:

```text
workspace validation -> LocalGitWorkspace._get_state -> reuse
reviewed tree -> _WorkspaceState.review.tree -> reuse
Git execution/error mapping -> LocalGitWorkspace._git -> reuse
output cleanup -> GitWorkspace.restore_paths -> smallest interface extension
```

Read `GitWorkspace`, `LocalGitWorkspace.diff`, and the current reviewed-commit
tests. The reviewed tree, not filesystem existence and not HEAD, is the source
of truth. Determine whether a path exists in that tree with observable Git
output such as `git ls-tree -z --name-only <review-tree> -- <path>`. Do not infer
existence from `git cat-file -e` stdout because both outcomes may have empty
stdout when the helper is called with `check=False`.

## Interface

```python
class GitWorkspace(ABC):
    @abstractmethod
    async def restore_paths(
        self,
        workspace: GitWorkBranch,
        paths: tuple[str, ...],
    ) -> None:
        """Restore only these paths to the last reviewed tree."""
```

The method returns no snapshot and does not create a new review. It preserves
all changes outside `paths`, including a source mutation made during command
execution, so the caller's next `diff()` detects it.

## Task 1: Restore Reviewed And Generated Outputs

- [ ] **Step 1: Verify the stable baseline**

  Run:

  ```powershell
  git branch --show-current
  git diff --cached --name-only
  uv run pytest test/unit/test_git_workspace.py -q -p no:cacheprovider
  ```

  Expected before the new tests: `main`, empty index, and the existing suite
  passes. If files change during the command, wait for a stable snapshot and
  rerun.

- [ ] **Step 2: Write the failing tracked-output test**

  Add this behavior to `LocalGitWorkspaceTest` using its real temporary repo:

  ```python
  async def test_restore_paths_restores_reviewed_output_and_preserves_source_change(self):
      workspace = await self._create("experiment/restore-reviewed-output")
      root = Path(workspace.path)
      source = root / "solution.py"
      output = root / "predictions.csv"
      source.write_text("VERSION = 1\n", encoding="utf-8")
      output.write_text("id,prediction\n1,0\n", encoding="utf-8")
      reviewed = await self.manager.diff(workspace)

      source.write_text("VERSION = 2\n", encoding="utf-8")
      output.write_text("id,prediction\n1,1\n", encoding="utf-8")
      await self.manager.restore_paths(workspace, ("predictions.csv",))

      self.assertEqual("id,prediction\n1,0\n", output.read_text(encoding="utf-8"))
      self.assertEqual("VERSION = 2\n", source.read_text(encoding="utf-8"))
      self.assertNotEqual(reviewed, await self.manager.diff(workspace))
  ```

- [ ] **Step 3: Write the failing execution-only-output test**

  ```python
  async def test_restore_paths_removes_execution_only_output(self):
      workspace = await self._create("experiment/remove-generated-output")
      root = Path(workspace.path)
      source = root / "solution.py"
      output = root / "predictions.csv"
      source.write_text("VERSION = 1\n", encoding="utf-8")
      reviewed = await self.manager.diff(workspace)

      source.write_text("VERSION = 2\n", encoding="utf-8")
      output.write_text("id,prediction\n1,1\n", encoding="utf-8")
      await self.manager.restore_paths(workspace, ("predictions.csv",))

      self.assertFalse(output.exists())
      self.assertEqual("VERSION = 2\n", source.read_text(encoding="utf-8"))
      self.assertNotEqual(reviewed, await self.manager.diff(workspace))
  ```

- [ ] **Step 4: Write path-boundary RED cases**

  Add these cases to `LocalGitWorkspaceTest`:

  ```python
  async def test_restore_paths_rejects_paths_outside_workspace(self):
      workspace = await self._create("experiment/reject-output-escape")
      outside = Path(self._temp.name) / "outside.csv"
      outside.write_text("keep\n", encoding="utf-8")
      await self.manager.diff(workspace)

      for invalid in (str(outside), "../outside.csv"):
          with self.subTest(path=invalid):
              with self.assertRaises(GitWorkspaceError):
                  await self.manager.restore_paths(workspace, (invalid,))

      self.assertEqual("keep\n", outside.read_text(encoding="utf-8"))


  async def test_restore_paths_requires_reviewed_tree(self):
      workspace = await self._create("experiment/restore-before-review")

      with self.assertRaises(GitWorkspaceError):
          await self.manager.restore_paths(workspace, ("predictions.csv",))
  ```

  Run:

  ```powershell
  uv run pytest test/unit/test_git_workspace.py -q -p no:cacheprovider
  ```

  Expected RED: `LocalGitWorkspace` has no `restore_paths` behavior. The test's
  `AttributeError` at that method call is the valid missing-feature RED. Do not
  edit the abstract interface or implementation before observing it.

- [ ] **Step 5: Implement the minimum GREEN**

  In `GitWorkspace`, add the abstract signature above. In
  `LocalGitWorkspace.restore_paths`:

  1. acquire the existing lock and validate the registered workspace through
     `_get_state`;
  2. require `state.review`;
  3. normalize and validate every path as a relative path contained by the
     workspace;
  4. query the reviewed tree for the exact path;
  5. for a tracked path, restore that path in index and worktree from
     `review.tree`;
  6. for an absent path, remove it from the index if present and unlink only
     that workspace file;
  7. leave `state.review` and every non-output path unchanged.

  Use `_git` for all Git commands and raise `GitWorkspaceError` through the
  existing error boundary. Do not read and retain output bytes in memory.

- [ ] **Step 6: Verify GREEN and regressions**

  ```powershell
  uv run pytest test/unit/test_git_workspace.py -q -p no:cacheprovider
  uv run pytest test/unit -q -k "git_workspace or workspace" -p no:cacheprovider
  uv run black --check src/athena/core/workspace.py src/athena/core/git_workspace.py test/unit/test_git_workspace.py
  uv run python -m compileall -q src/athena/core/workspace.py src/athena/core/git_workspace.py
  git diff --check -- src/athena/core/workspace.py src/athena/core/git_workspace.py test/unit/test_git_workspace.py
  ```

- [ ] **Step 7: Return completion evidence**

  ```text
  Task: T11-I
  Agent: <recorded claim>
  Branch: main
  Infrastructure: <the four audit mappings above>
  Files: <exact owned changed paths>
  RED: <command and expected behavioral failure>
  GREEN: <focused command and pass count>
  Regression: <command and pass count>
  Static/format: <commands and results>
  Open blockers: none
  ```

`/root` reruns the focused command, commits only the three owned files as
`fix: restore reviewed workspace outputs`, and authorizes T11 to consume the
interface. This evidence reconciliation is not an implementation review.

## Task 2: Consume The Workspace Owner

**Task ID:** `T11-S`

**Files:**

- Modify: `src/athena/research/supervisor/validation.py`
- Test: `test/integration/research/test_validate_agent_contract.py`

Any assigned implementation Agent may own this task. It starts only after
T11-I commits, requires no original-Agent context, and must not edit Git
workspace files.

- [ ] **Step 1: Add the consumer RED**

  Import `AsyncMock` from `unittest.mock`. Add this integration test beside the
  existing stable-workspace case. It keeps the real `LocalGitWorkspace`, makes
  `predictions.csv` part of the candidate diff before independent review, then
  observes the one public restore call:

  ```python
  @pytest.mark.asyncio
  async def test_validation_delegates_all_output_restoration_to_git_workspace(
      tmp_path,
  ) -> None:
      harness = _Harness(tmp_path)
      await harness.start()
      root = Path(harness.branch.path)
      predictions = root / "predictions.csv"
      predictions.write_text("id,pred\n1,reviewed\n", encoding="utf-8")
      restore = AsyncMock(wraps=harness.git.restore_paths)
      harness.git.restore_paths = restore

      await harness.run()

      restore.assert_awaited_once_with(harness.branch, ("predictions.csv",))
      assert predictions.read_text(encoding="utf-8") == "id,pred\n1,reviewed\n"
      await harness.close()
  ```

  Keep `test_command_source_mutation_is_rejected_before_score`; together the
  tests prove only declared outputs are restored and source mutation remains
  visible. Do not assert Git commands or provider stream-call counts.

  Run:

  ```powershell
  uv run pytest test/integration/research/test_validate_agent_contract.py -q -p no:cacheprovider
  ```

  Expected RED: VALIDATE restores private bytes and never delegates the
  declared output set to `GitWorkspace.restore_paths`.

- [ ] **Step 2: Implement the minimum GREEN**

  Pass the injected `GitWorkspace` into `_execute_predictions` and replace
  `original_outputs` plus its byte-read/write loop with exactly one `finally`
  call:

  ```python
  await git.restore_paths(workspace, tuple(manifest.outputs.values()))
  ```

  Do not branch on file existence in VALIDATE. `ExperimentManifest` already
  validates workspace-relative output paths; GitWorkspace owns reviewed-tree
  tracked/untracked decisions.

- [ ] **Step 3: Verify GREEN and full VALIDATE regression**

  ```powershell
  uv run pytest test/integration/research/test_validate_agent_contract.py -q -p no:cacheprovider
  uv run pytest test/unit/agent/test_validate_agent.py test/unit/research/supervisor/test_validation_plan.py test/integration/research/test_validate_agent_contract.py test/unit/research/test_validation.py test/unit/research/test_services.py -q -p no:cacheprovider
  uv run black --check src/athena/research/supervisor/validation.py test/integration/research/test_validate_agent_contract.py
  uv run python -m compileall -q src/athena/research/supervisor/validation.py
  git diff --check -- src/athena/research/supervisor/validation.py test/integration/research/test_validate_agent_contract.py
  ```

- [ ] **Step 4: Return the same completion package for `T11-S`**

  Include the exact RED, focused GREEN, full pass count, and proof that the only
  changed files are the two owned paths. `/root` commits them as
  `refactor: delegate validation output restoration`. No later review follows.
