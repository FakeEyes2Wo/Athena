# Athena Research Runtime v2

Status: approved
Owner: Athena maintainers
Last verified: 2026-07-31
Source of truth: approved design decisions and the runtime paths named below

## Decision

ResearchTree v2 is the sole source of truth for hypotheses, experiments,
experiment relationships, evidence, and the selected SOTA experiment.
`ResearchTree.Experiment` owns the associated `GitWorkBranch`, because the
workspace is part of the reproducible execution record.

The unused `ExperimentStore` branch is not revived. Its useful child-index and
linear-serialization work is migrated into ResearchTree v2, after which its
types, store, v1 persistence code, and tests are removed.

Runtime code accepts only schema version 2. The repository contains no v1
loader, compatibility parser, or migration utility. Existing fixtures and
examples are rewritten as v2 data.

## Ownership

```text
GUI Gateway / WebSocket
      | protocol, serialization, and UI adaptation only
      v
athena.research.ResearchRuntime
      | phase, budget, cancellation, subscriptions, active task
      v
PREPARE -> SEARCH -> VALIDATE -> REPORT
      |
      v
ResearchTree v2
  |- Hypothesis
  |- Experiment
  |    `- GitWorkBranch
  |- parent / children
  `- SOTA
```

- `athena.core.research_tree.ResearchTree` owns mutable research facts.
- `athena.core.workspace.GitWorkspace` defines worktree operations, implemented by `athena.git_workspace.LocalGitWorkspace`.
- `athena.research.ResearchRuntime` owns research lifecycle state; app-server remains unchanged and owns only Thread/Turn concerns.
- `gui_gateway` forwards protocol requests and renders returned state.
- Workflow packages own PREPARE, SEARCH, VALIDATE, and REPORT orchestration.
- `athena.experiment.ranking` is the only ranking implementation.
- The repository contains no obsolete `athena.core` compatibility paths.

## Experiment Record

The canonical Experiment record contains:

- a `hypothesis_id` reference and an embedded experiment plan;
- the commit and `GitWorkBranch` used for execution;
- `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, or `CANCELLED` status;
- an optional real `EvalResult` and `ComparisonVerdict`;
- `dict[str, ArtifactRef]` for diff, logs, report, and other evidence;
- an optional error description for failed execution.

Hypotheses are stored once in the tree's hypothesis mapping. An Experiment does
not embed a second Hypothesis object. The key of the experiment mapping is the
experiment ID; the record does not carry a second copy of its own ID. Each
record stores only `parent_id`. Children are an in-memory index derived from
parent references and are not serialized as a second relationship source.

No successful record may use `"N/A"`, a fabricated zero metric, or a missing
validation result as a substitute for capability.

## State Flow

1. PREPARE creates the baseline worktree and a `PENDING` root experiment, then
   runs it through the same execution boundary. A real successful evaluation
   moves it through `RUNNING` to `SUCCEEDED` and establishes the initial SOTA.
   Failure blocks SEARCH.
2. SEARCH requires that successful baseline, selects a hypothesis with the
   deterministic ranking policy, creates a `PENDING` child, moves it to
   `RUNNING`, and then invokes CodeAgent.
3. Successful execution records the real evaluation, verdict, and artifacts
   before moving to `SUCCEEDED`.
4. Only a successful experiment accepted by Supervisor may become SOTA.
5. An execution exception moves the record to `FAILED` and preserves its error
   and log evidence. Cancellation moves it to `CANCELLED`.
6. VALIDATE records ablation and final-test runs as real experiments attached
   to the selected SOTA path.
7. REPORT reads completed evidence, writes a deterministic report artifact,
   and attaches that artifact to the SOTA experiment.

The existing deterministic ranking policy remains in force. The future Elo
policy remains only as `TODO(advanced-ranking)` until its rubric, pair
construction, cold start, and confidence calibration are approved.

## Persistence

ResearchTree v2 serialization contains one versioned graph with derived child
indexes and no second experiment model. Save operations write atomically.

The persisted shape is:

```text
version: 2
sota_id: string | null
hypotheses: {hypothesis_id: Hypothesis}
experiments:
  {experiment_id:
    parent_id: string | null
    hypothesis_id: string
    commit: CommitHash
    plan: ExperimentPlan
    gitwork: GitWorkBranch
    status: ExperimentStatus
    eval: EvalResult | null
    verdict: ComparisonVerdict | null
    artifacts: {kind: ArtifactRef}
    error: string | null
  }
```

Allowed status transitions are `PENDING -> RUNNING`, `PENDING -> CANCELLED`,
and `RUNNING -> SUCCEEDED | FAILED | CANCELLED`. Terminal states cannot change.
SOTA must name a `SUCCEEDED` baseline or search experiment, never an ablation or
final-test record.

Load performs these steps before replacing live state:

1. Require `version: 2`.
2. Parse every hypothesis, experiment, status, worktree, and artifact.
3. Validate parent references and reject cycles or missing parents.
4. Validate that SOTA names one eligible successful experiment.
5. Build the child index.
6. Replace live state only after all checks pass.

Version 1, a missing version, and unknown versions fail with an explicit schema
error. They are not converted or partially loaded.

## App Server And GUI Gateway

`ResearchRuntime` owns the active ResearchTree, phase, budget, running task,
cancellation, and workflow dependencies. It exposes application commands for
configuration, search control, validation, reporting, snapshots, save, and
load.

GUI gateway transport delegates to those commands. It does not mutate the tree or
manufacture domain responses. The existing `tree_get`, `tree_save`, and
`tree_load` commands remain as adapters because both frontends use them.
`tree_add_node` is removed because no frontend calls it and direct UI writes
would bypass the application owner.

App-server protocol errors are returned for invalid phase transitions,
unsupported persistence versions, missing SOTA state, missing validation
backends, incomplete report evidence, and unknown commands.

## Compatibility And Removal

- `athena.experiment.search_loop` re-exports the canonical workflow SearchLoop.
- `athena.experiment.ranking` is the canonical ranking implementation.
- `athena.experiment.validate` re-exports the canonical Validator.
- `athena.experiment.report` continues to re-export the canonical Reporter.
- Duplicate implementations are removed after their imports are redirected.
- `ExperimentStore`, `InMemoryExperimentStore`, the duplicate Experiment type,
  and v1 persistence are removed after their useful indexing and serialization
  behavior has been represented by ResearchTree v2 tests.

## Delivery Batches

Each batch is independently verified and committed.

1. **ResearchTree v2**: add the canonical record and state rules, migrate the
   child index and linear persistence, rewrite fixtures and examples, then
   remove the old Store/types/v1 persistence branch and tests.
2. **PREPARE and SEARCH**: record baseline and search execution honestly,
   consolidate Ranking, and reduce duplicate paths to re-exports.
3. **VALIDATE and REPORT**: record validation runs and generate reports only
   from complete v2 evidence; remove duplicate implementations.
4. **Research runtime and GUI gateway**: keep research lifecycle ownership in
   `athena.research`, delegate GUI commands, and keep app-server unchanged.
5. **Documentation**: update current architecture, testing guidance, examples,
   and ownership references to the verified implementation.

## Verification

Every implementation batch must run its targeted tests and the full applicable
gate. Final acceptance requires:

```powershell
uv run pytest -q tests test/unit
Set-Location athena-gui
npm test
npm run build
```

Repository searches must find:

- no v1 parser, v1 fixture, or v1 migration utility;
- no production `ExperimentStore` or `InMemoryExperimentStore` reference;
- no copied SearchLoop, Ranking, or Validator business logic;
- no placeholder success, `"N/A"` success, or fabricated zero metric;
- exactly one explicit `TODO(advanced-ranking)` policy gap in the canonical
  ranking path.
- no obsolete core imports, verified with:

```powershell
rg -n -P "athena\.core\.(?:schemas|gitutils|ranking|budget|evaluation)(?:\.|\b)|athena\.core\.research(?:\.|\b)|athena\.core\.agent\.(?:agent|subagent)(?:\.|\b)" src tests test examples scripts agent_tool_example.py
```

All unrelated user worktree changes and the three safety stashes remain
untouched.
