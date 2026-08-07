# Athena Main Workflow Design

**Status:** Approved
**Date:** 2026-08-07

## Goal

Provide a runnable `src/main.py` composition root for Athena's complete
`PREPARE -> SEARCH -> VALIDATE -> REPORT` workflow. The entry point must use
real agent-backed code generation, preserve experiment evidence in the
canonical `ResearchTree`, and fail explicitly when a required backend or input
is unavailable.

## Command-Line Contract

The entry point accepts a CSV dataset, target column, task type, primary
metric, metric direction, output directory, experiment budget, model name,
code backend, and optional HIL/debug flags. The supported backend values are:

- `codex`: invoke the locally installed Codex CLI in the experiment worktree.
- `qoder`: invoke the installed `qoder-agent-sdk` Python client.
- `auto`: use the existing `CodeRouter` for each hypothesis and require the
  selected backend to be available.

Credentials and provider configuration come from environment variables. The
program loads `.env` without printing secrets. Invalid paths, missing columns,
unsupported metric/task combinations, absent credentials, or unavailable
backends fail before starting an experiment where possible.

## Architecture

`src/main.py` is the application composition root. It creates the artifact
store, experiment Git repository, backend registry, Ideator, CodeAgent,
ResearchRuntime dependencies, Validator, and Reporter. Domain logic remains in
the existing `athena` modules rather than being reimplemented in the CLI.

The existing `ResearchRuntime` remains the workflow control plane. The main
entry point configures the task, starts PREPARE and SEARCH, awaits the runtime
task, then starts VALIDATE and REPORT. It persists the research tree after each
terminal phase and prints artifact locations and final status.

## PREPARE

PREPARE reads the CSV, validates the target, preserves an immutable raw copy,
and applies a deterministic minimum cleaning policy: rows with missing targets
are removed, numeric feature gaps use the training-safe median policy, and
categorical gaps use an explicit missing token. It then creates seeded,
disjoint train, validation, and final-test artifacts and records a
`DataProfile`, `ProcessingLog`, and frozen `EvalSpec`.

The experiment repository stores only small manifests and protected evaluator
code. Dataset contents remain artifacts and are referenced by absolute local
artifact paths, avoiding large data commits.

## Agent And Backend Flow

The existing Ideator remains the multi-agent hypothesis generator and uses the
configured PydanticAI model. Codex and Qoder implement the common `CodeBackend`
contract and receive the same experiment prompt, frozen task description,
allowed files, artifact references, prior execution output, and bounded round
history.

Codex runs through its CLI with the experiment worktree as its working
directory. Qoder runs through `qoder-agent-sdk`, installed as a project
dependency. Backend adapters report the files they actually changed and never
fabricate successful generation.

`CodeAgent` always requests or revises the fixed `run_experiment.py` entrypoint.
It executes the script under `AgentMonitor`, runs the protected evaluator, and
feeds execution failures back to the backend for a bounded number of repair
rounds. Generated code may read the frozen split manifest but may not modify
the evaluator or split definition.

## Experiment Evidence And Git

Each successful baseline, search candidate, ablation, and final-test run uses
this sequence:

1. Create an isolated worktree from the parent experiment commit.
2. Generate or revise `run_experiment.py` with the selected backend.
3. Execute it and the protected evaluator.
4. Build the complete binary Git diff.
5. Reject protected-file or scope violations.
6. Commit the approved tree and record the new commit in `ResearchTree`.
7. Attach real diff, log, prediction, and evaluation artifact references.

Failed and cancelled experiments retain their terminal state and available
logs. They do not become SOTA and do not consume a successful experiment
commit.

## Validation And Reporting

SEARCH uses the existing ranker, proximity graph, comparator, supervisor, and
budget. VALIDATE creates one isolated ablation for every hypothesis on the SOTA
path and exactly one final-test run. REPORT reads only successful tree evidence
and generates a deterministic Markdown report, optionally augmented by a
structured agent narrative grounded in the supplied evidence.

## Error Handling

The workflow never falls back to hard-coded hypotheses, metrics, code, or
success records. Backend launch errors, timeouts, malformed generated files,
metric failures, protected-file edits, and Git failures raise actionable
errors. The latest research tree and logs are saved before the CLI exits with a
non-zero status.

## Testing

Tests use fake Codex and Qoder clients to verify command construction, SDK
mapping, generation results, protected-file enforcement, and error propagation.
A deterministic fake code backend drives an end-to-end test through PREPARE,
SEARCH, VALIDATE, REPORT, Git commits, and tree persistence without external
network access. Existing unit tests remain regression gates.

Manual smoke checks cover `src/main.py --help`, prerequisite diagnostics, one
Codex-backed run, and one Qoder-backed run when credentials are configured.

## Scope Boundaries

This change does not implement advanced Elo policy, semantic proximity,
distributed execution, Kaggle MCP, Hugging Face model downloading, or a new
workflow checkpoint system. Those remain separate follow-up work and do not
block the minimum complete local workflow.
