# Experiment Document Projection Completion Report

Date: 2026-09-05
Design: `docs/superpowers/specs/2026-09-04-experiment-document-projection-design.md`

## Delivered

Athena now projects experiment documents through the focused
`athena.research.experiment_documents` package. Strict contracts, metric resolution,
deterministic renderers, atomic multi-file storage, immutable run archives, a hashed
completion manifest, and a failure-containing projector replace the former monolithic
`athena.research.exp_docs` module.

The runtime constructs one required projector and injects it into the Supervisor.
PREPARE, SEARCH settlement, VALIDATE, skipped validation, and phase-failure paths save
canonical tree and state first, then refresh the derived documents. Projection and
warning-publication failures no longer change the canonical phase, SOTA, Plan
settlement, or research result. Recovery rebuilds derivable archives, aliases, and
reports while retaining unknown history and byte-preserving compatible archives.

## Acceptance evidence

| Criterion | Command or test | Result |
|---|---|---|
| Old module and imports are absent | `Test-Path src/athena/research/exp_docs.py`; legacy API `rg` audit | `False`; empty search, exit 1 |
| Projector is required and injected once | `rg -n "documents" src/athena/research/supervisor/deps.py`; `rg -n "ExperimentDocumentProjector\\(" src` | required `RuntimeDependencies.documents`; one production construction in `runtime/bootstrap.py` |
| Frozen evaluator priority and no nonexistent spec path | `test/unit/research/experiment_documents/test_metric.py`; `rg -n "evaluator_spec\\.json" src` | included in 84 package passes; empty search, exit 1 |
| Unsafe identifiers, non-finite metrics, and extra fields are rejected | `test/unit/research/experiment_documents/test_models.py` | included in 84 package passes |
| Semantic idempotency preserves bytes and conflicts preserve history | `test/unit/research/experiment_documents/test_store.py`; projector conflict tests | included in 84 package passes |
| Canonical tree and state are durable before every projection | `test/unit/research/supervisor/test_document_projection.py` | included in 350 Supervisor passes |
| Skipped validation and recovery survive projection failure | `test_projection_failure_does_not_block_skip_finalization` | included in 8 autonomous integration passes; real `Supervisor.recover()` rebuilt `final-skipped` |
| Warnings are sanitized and isolated from state, SOTA, and phase | Supervisor document-projection warning and settlement tests | included in 350 Supervisor passes |
| Atomic failure cleanup retains the previous manifest | `test_commit_cleans_temps_after_replace_failure` | included in 84 package passes |
| Concurrent projections do not interleave and latest identifies the last completion | `test_replaces_are_serialized_and_latest_is_last` | included in 84 package passes |
| Rebuild repairs derivable views and retains unknown history | projector rebuild tests, including normal archive compatibility | included in 84 package passes |
| GUI and disk reports are byte-identical | `test_all_phases_share_one_durable_state` through `GuiService.generate_report()` | included in 8 autonomous integration passes |
| CLI and Supervisor import successfully | Python import smoke; `Athena-cli --help` | both exited 0; CLI help rendered without `ModuleNotFoundError` |

Focused verification on 2026-09-05 also passed: 84 experiment-document tests, 350
Supervisor tests, and 35 autonomous/recovery/rolling/architecture tests. The legacy API
and evaluator-spec searches both returned empty output with exit 1, and
`git diff --check` exited 0.

## Full-suite result

`uv run pytest -q -p no:cacheprovider --basetemp=.ptf` completed with **2848 passed,
2 failed, 1 warning, and 53 subtests passed** in 353.87 seconds. Both failures are the
declared external-environment limitation in `test/unit/execution/test_encoding.py`:
`test_windows_shell_detection_finds_pwsh_on_path` found only Windows PowerShell 5.1,
and `test_chain_operator_works_under_powershell7` consequently rejected `&&` under
PowerShell 5.1. `Get-Command pwsh` and the three standard installation-path checks
confirmed that PowerShell 7 is not installed. No product test outside those two
environment assertions failed.

## Repository state

Implementation and evidence commits, in order:

- `eca19af` — activate the experiment-document plan.
- `30b7700` — define strict document contracts.
- `3b1650d` — resolve projected metric authority.
- `c8668ad` — record document foundations.
- `095e140` — render experiment-document batches.
- `f26a2f5` — retain legacy optimization guidance.
- `b7b9586` — add the safe projector facade.
- `16fd137` — atomically commit document batches.
- `449bfc2` — record the projection core.
- `dffdd38` — rebuild documents from canonical state.
- `ac4edc0` — record rebuild completion.
- `3aa2a28` — inject projection across Supervisor lifecycle paths.
- `0295906` — preserve normal projection archives during rebuild.
- `2dae16e` — verify the external projection contract.

The unrelated main-worktree changes in `src/athena/core/persistence.py`,
`src/athena/research/evaluation/trust.py`,
`test/unit/research/test_evaluator_trust.py`, and
`tmp_athena_tui_authorized.py` remain outside every task commit. The paused
transactional-resume worktree, branch, and WIP stash also remain untouched.

Merge, merged-tree verification, push, worktree removal, and temporary-branch deletion
remain the required delivery step. This section will be updated on `main` immediately
after those actions complete.
