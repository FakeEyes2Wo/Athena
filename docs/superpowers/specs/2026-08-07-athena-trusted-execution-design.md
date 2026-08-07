# Athena Trusted Experiment Execution Design

**Status:** Approved
**Date:** 2026-08-07

## Goal

Make Athena's runnable Agent workflow produce trustworthy evaluation evidence
while keeping local execution as the default. Add optional Docker isolation,
remove generated labels from the scoring boundary, prevent final-test leakage,
execute the selected SOTA unchanged on final-test data, preserve complete Git
evidence, and feed actionable failures back to Codex CLI or Qoder SDK.

## Security And Trust Boundary

The default execution mode is `local`. It uses the active Athena Python
environment with a strict environment-variable allowlist, timeouts, a dedicated
worktree, and protected control files. It does not provide strong filesystem or
network isolation because a normal local subprocess can access resources allowed
to the current operating-system user. Athena must print this limitation and
record `strong_isolation: false` in the run summary.

The optional `docker` mode is the strong-isolation path. It runs as a non-root
user with networking disabled, explicit read-only data mounts, a writable
experiment worktree, and CPU, memory, and PID limits. It records
`strong_isolation: true`. Docker availability, daemon connectivity, and the
configured image are preflight requirements only when this mode is selected.

Both modes remove model-provider credentials and `.env` values from experiment
processes. Backend agents retain the credentials needed for code generation;
generated programs do not inherit them.

## Command-Line Contract

`src/main.py` adds:

- `--execution {local,docker}`, defaulting to `local`.
- `--docker-image IMAGE`, selecting the prebuilt experiment image for Docker
  execution.

The JSON summary records the execution mode and strong-isolation status. Local
mode emits a concise warning to stderr. Docker mode fails before PREPARE if its
runtime or image is unavailable.

## Prepared Data Contract

PREPARE creates a stable `__athena_row_id` after removing rows with missing
targets and before the deterministic split. The identifier is unique within the
run and remains unchanged through cleaning and evaluation.

Artifacts are separated by purpose:

- Train data contains the row ID, features, and target for model fitting.
- Validation and test feature artifacts contain row IDs and features but no
  target.
- Validation and test label artifacts contain row IDs and targets and are owned
  by the trusted evaluation layer.

No experiment receives a global manifest containing all split paths. Athena
creates a protected, Git-ignored phase manifest for each worktree:

- Baseline, SEARCH, and ablation receive train data and validation features.
- Final-test receives train data and test features.

The label artifact path is supplied only to the trusted evaluator after the
generated experiment process terminates. In local mode this prevents accidental
or prompt-driven label access but is not an operating-system security boundary;
Docker mode enforces the data mount boundary.

## Generated Code Contract

`run_experiment.py` is the required executable entrypoint, not the only file an
Agent may generate. An Agent may create, modify, or delete regular auxiliary
source files, model configuration, and resources inside the worktree. It may not
create symbolic links that resolve outside the worktree.

Athena-owned paths remain protected from creation, modification, and deletion:

- `.git/` and `.gitignore`
- `eval.py` and `eval_spec.json`
- the phase manifest
- predictions, logs, evaluation results, and other runtime evidence paths

The final generated tree must contain `run_experiment.py`. Runtime dependencies
are limited to the packages already installed in the Athena environment or the
experiment image. Generated code may not install packages or use network access
during execution.

The entrypoint writes `predictions.csv` with exactly two columns:

```text
__athena_row_id,prediction
```

Generated code never writes trusted labels. Creating `labels.csv` has no effect
on scoring and is treated as a reserved runtime-path violation.

## Trusted Evaluation

Athena evaluates predictions against its private phase label artifact. Before
calculating any metric it requires:

- both required columns with no unexpected columns;
- non-null and unique row IDs;
- exact row-ID set equality with the selected label artifact;
- deterministic alignment by row ID;
- a non-empty finite metric result.

Missing, duplicate, unknown, or extra IDs fail evaluation. The frozen metric
catalog remains deterministic and supports the task/metric combinations exposed
by CLI preflight. Unsupported combinations and split sizes that cannot produce
non-empty train, validation, and test sets fail before the experiment repository
is initialized.

## Generation And Repair Loop

For baseline, SEARCH, and ablation, each bounded round performs:

1. Invoke the selected Codex or Qoder backend.
2. Snapshot the complete generated tree, including additions, modifications,
   and deletions.
3. Require `run_experiment.py` and verify protected paths and symlink rules.
4. Execute the entrypoint through the configured execution runtime.
5. Run trusted evaluation.
6. On failure, provide bounded stdout, stderr, evaluation diagnostics, changed
   paths, and prior round history to the next backend call.

Successful evaluation proceeds to Git review. Backend adapters must include
`previous_outputs` and `history` in repair prompts rather than discard them.

## Git And Artifact Evidence

Git review uses the complete staged binary diff from the parent commit. It
allows generated auxiliary files but rejects changes to protected or runtime
paths, external symlinks, and a missing final entrypoint. Deletions are reviewed
the same way as additions and modifications. The reviewed tree hash and diff
hash must still match at commit time.

Logs, predictions, evaluation results, and diffs are copied to the
content-addressed artifact store before worktree cleanup. ResearchTree stores
those durable references plus the real generated commit. Evidence must remain
readable after the worktree is removed.

## Validation Semantics

Ablation experiments branch from the selected SOTA and may generate a complete
revised code tree under the normal protected-file rules. They use validation
features and trusted validation labels.

Final-test branches from the selected SOTA commit but never invokes Codex,
Qoder, or any code-repair loop. It executes the exact committed
`run_experiment.py` and all committed auxiliary files with the final-test phase
manifest. Its ResearchTree commit equals the SOTA commit. Any execution or
evaluation failure is recorded as a failed final-test; test feedback is never
used to alter code.

## Error Handling

Configuration errors return exit code 2 before agent work where possible.
Workflow, backend, execution, evaluation, Git, or artifact failures return exit
code 1. Interruptions return 130. Athena saves the latest ResearchTree on every
phase boundary and in the top-level failure path.

Failed experiments retain all durable evidence available at the failure point.
Athena never emits a report or success summary unless validation evidence is
complete. Local mode's isolation warning is part of normal startup and does not
change the exit code.

## Docker Executor Image

The repository provides a reproducible experiment executor image definition and
documents its build command. The image contains Python 3.11 plus the locked
scientific runtime required by generated experiments. Docker execution uses no
network, runs as a non-root user, mounts only the worktree and phase-authorized
data, and applies explicit resource limits. Image building is a separate setup
operation and may use network access to resolve the locked packages.

## Verification

Automated tests must cover:

- generated `labels.csv` cannot influence trusted scoring;
- SEARCH and ablation manifests do not contain test paths;
- missing, duplicate, or extra prediction IDs fail evaluation;
- auxiliary generated files can be committed;
- protected files and runtime paths cannot be added, changed, or deleted;
- external symlinks are rejected;
- final-test does not invoke a backend and retains the exact SOTA commit;
- local experiment processes cannot read injected test API tokens;
- Docker commands include network, user, mount, and resource restrictions;
- repair prompts contain prior execution and evaluation diagnostics;
- evidence remains readable after worktree removal;
- the offline workflow reaches PREPARE, SEARCH, VALIDATE, and REPORT with real
  Git commits and durable evidence.

Full Python tests, targeted formatting checks, dependency checks, CLI help,
invalid-configuration exit codes, and diff whitespace checks remain release
gates. Real Codex and Qoder smoke runs are reported separately because they
require external authentication and network availability.

## Scope Boundaries

This design does not attempt to make local subprocess execution a strong
sandbox, permit runtime dependency installation, add distributed execution, or
change Ideator, ranking, HIL, report narrative, or advanced evaluation policy.
Strong isolation is provided only by the optional Docker execution mode.
