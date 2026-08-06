# Core Structure Reorganization Design

Status: approved
Owner: Athena maintainers
Date: 2026-07-31

## Purpose

Reorganize every module currently owned by `src/athena/core` so that directory
depth reflects real functional boundaries, large mixed-responsibility modules
are split by responsibility, and domain implementations live with their
canonical owners. Preserve runtime behavior and the two supported public import
surfaces: `athena.core` and `athena.core.agent`.

`ResearchTree` remains in `athena.core` for this change. Its ownership may be
revisited separately, but this reorganization must not move it or change its v2
persistence contract.

## Current Problems

- `core/research/research_tree.py` and `core/gitutils/workspace.py` add package
  depth around a single implementation module.
- `core/agent/agent.py` repeats the package name and mixes public data models,
  orchestration, sampling, tool scheduling, factories, and runner adaptation.
- `core/schemas.py` mixes generic references, Thread/Turn protocol models,
  research facts, data facts, and evaluation protocol models.
- `core/evaluation.py` points from core back to the higher-level evaluation
  package.
- `core/ranking.py` and `core/budget.py` are domain policy implementations whose
  active consumers and compatibility modules already live under `experiment`
  and `research`.
- Git contracts and the local subprocess implementation are combined in one
  large module.
- Several library modules contain executable demonstrations that belong under
  `examples/`, making production files longer and obscuring their real scope.

## Chosen Approach

Use owner-driven, moderate flattening.

- Keep packages only where multiple stable units collaborate. The Agent package
  remains because it owns runtime, provider, control, models, and prompt assets.
- Flatten one-module wrapper packages.
- Move canonical business implementations to their established owners.
- Split shared schemas by domain and dependency direction.
- Do not create compatibility packages for obsolete deep imports.

Rejected alternatives:

- A flatten-only change would leave schema mixing and dependency inversion in
  place.
- Reducing core to only generic contracts would move Agent and Tool APIs without
  a demonstrated benefit and would create unnecessary migration risk.

## Target Structure

```text
src/athena/
|- core/
|  |- __init__.py
|  |- contracts.py
|  |- thread_models.py
|  |- research_models.py
|  |- research_tree.py
|  |- workspace.py
|  |- tool.py
|  |- tool_types.py
|  `- agent/
|     |- __init__.py
|     |- models.py
|     |- runtime.py
|     |- provider.py
|     |- control.py
|     `- prompts/
|- git_workspace.py
|- experiment/
|  `- ranking.py
|- research/
|  |- models.py
|  `- budget.py
|- evaluation/
|  `- types.py
`- data/
   `- types.py
```

The final `core` tree must not contain `research/`, `gitutils/`, `schemas.py`,
`ranking.py`, `budget.py`, or `evaluation.py`.

## Module Responsibilities

### Core contracts

`core/contracts.py` owns low-level aliases and helpers that can be imported by
any domain without pulling in runtime dependencies:

- `NonBlankText`, `ArtifactRef`, `CommitHash`, and `ExecutionId`
- `RunId`, `HypothesisId`, and `ExperimentId`
- `new_id`
- generic `EventEnvelope` and `ErrorRecord`

It imports no Athena business package.

`core/thread_models.py` owns `AgentTask`, `AthenaThread`, and `AthenaTurn`.
Keeping these lightweight models outside the Agent package prevents app-server
code from importing provider and runtime dependencies merely to describe a
thread.

### Research facts

`core/research_models.py` owns the facts persisted in or directly referenced by
ResearchTree:

- `HypothesisStatus`, `Hypothesis`, and `ExperimentPlan`
- `ExperimentOutcome`

`core/research_tree.py` owns `ExperimentStatus`, `Experiment`, `ResearchTree`,
the transition table, graph validation, v2 serialization, and atomic file
persistence. Moving the module must preserve:

- `SAVE_VERSION == 2`
- every serialized field name and value shape
- all allowed and rejected lifecycle transitions
- SOTA eligibility rules
- exception categories and validation behavior
- atomic save and strict load semantics

`MetricSpec` and `TaskMetaData`, which configure the research runtime but are
not ResearchTree graph records, move to `research/models.py`. `DataCard` moves
to `data/types.py`. `MetricDef`, `EvalSpec`, `EvalResult`, and
`ComparisonVerdict` move to `evaluation/types.py` as the single evaluation
protocol owner; ResearchTree depends directly on these lightweight contracts.

### Workspace boundary

`core/workspace.py` contains only the stable workspace boundary:

- `BinaryDiffWriter`
- `GitWorkspaceError`
- `GitWorkBranch`
- abstract `GitWorkspace`

`git_workspace.py` contains `LocalGitWorkspace` and all Git subprocess,
review-freeze, commit, worktree creation, and cleanup behavior. This keeps the
ResearchTree record dependent on a lightweight branch contract rather than a
local Git implementation.

### Agent runtime

`core/agent/models.py` owns `AgentConfig`, `AgentContext`, `AgentOutcome`,
`StepOutcome`, and `ToolCall`.

`core/agent/runtime.py` owns `BaseAgent`, `Agent`, the sampling loop, tool-call
scheduling, runner adaptation, factories, input loading, and result formatting.
The runtime may depend on Agent models, Tool contracts, Thread models, the
provider interface, and memory management.

`core/agent/provider.py` remains the OpenAI-compatible streaming adapter and
owns `ResponsesProvider`, `StreamEvent`, and message conversion.

`core/agent/control.py` owns `AgentControl`, `AgentHandle`, `AgentEvent`, and
`AgentResult`.

`core/agent/__init__.py` is the stable public Agent API. Prompt Markdown files
remain under `core/agent/prompts/` because they are runtime resources owned by
the Agent package.

### Tool runtime

`core/tool.py` and `core/tool_types.py` already separate behavior from shared
data contracts and remain root modules. Their public behavior, event names,
dispatch ordering, cancellation semantics, and result truncation remain
unchanged.

### Domain owners

`experiment/ranking.py` becomes the sole ranking implementation. It retains the
current deterministic Bradley-Terry, bootstrap uncertainty, UCB selection, and
proximity behavior, including the single `TODO(advanced-ranking)` marker.

`research/budget.py` becomes the sole owner of `BudgetSnapshot` and `RunMode`.

The evaluator and comparator are imported from `athena.evaluation`; the reverse
compatibility module `core/evaluation.py` is removed.

## Dependency Direction

The intended dependency direction is:

```text
core.contracts
  -> core.thread_models / core.workspace / core.tool_types
  -> core.research_models / research.models / data.types / evaluation.types
  -> core.research_tree / core.tool / core.agent.models
  -> core.agent.runtime / core.agent.provider / core.agent.control
  -> research runtime and workflows
  -> IDE and app-server adapters
```

No compatibility module may point from a lower layer to a higher layer merely
to preserve an obsolete deep import.

## Import Compatibility

The following public surfaces remain compatible:

- `from athena.core import ...` for every symbol currently exported there
- `from athena.core.agent import ...` for every symbol currently exported there

All repository-owned callers are updated to canonical module paths. The
following deep paths are intentionally removed rather than recreated as empty
wrappers:

- `athena.core.agent.agent`
- `athena.core.agent.subagent`
- `athena.core.gitutils.workspace`
- `athena.core.research.research_tree`
- `athena.core.schemas`
- `athena.core.ranking`
- `athena.core.budget`
- `athena.core.evaluation`

No Pydantic field, dataclass field, default value, event kind, exception type,
or callable signature changes as part of this work unless an import-only type
annotation must name its new canonical module.

## Migration Sequence

1. Run the current Python suite and record the baseline.
2. Add focused import and serialization regression tests before moving code.
3. Split generic, Thread/Turn, research, evaluation, data, and runtime models;
   update imports while leaving behavior in place.
4. Flatten ResearchTree and split the workspace contract from its local Git
   implementation.
5. Split the Agent package into models, runtime, provider, and control modules;
   preserve its package exports and prompt resource lookup.
6. Move Ranking and Budget to their canonical domain owners and remove the
   reverse evaluation compatibility module.
7. Update every production, test, script, example, and documentation reference.
8. Remove obsolete files and empty directories, then run the complete gates.

Existing uncommitted user changes, including the note in
`core/agent/agent.py`, must be incorporated into the split and must not be
reverted or overwritten.

## Error Handling And Behavioral Safety

This is a structural refactor, so existing errors remain observable at the same
behavioral boundaries. Import failures during migration are fixed at their
callers rather than hidden behind new compatibility packages. ResearchTree
validation continues to reject invalid versions, references, cycles, state
transitions, result payloads, and SOTA assignments. Git workspace operations
continue to reject invalid branches, unknown worktrees, unreviewed commits,
post-review mutations, and unsafe cleanup.

If a test fails outside the recorded baseline, implementation stops at that
migration step until the regression is explained and corrected.

## Testing And Acceptance

Focused coverage must verify:

- every symbol currently exported by `athena.core` and `athena.core.agent`
  remains importable;
- ResearchTree v2 produces and accepts the same payload shape;
- graph validation, transitions, SOTA selection, and atomic persistence retain
  their current behavior;
- Agent streaming, tool scheduling, serial barriers, cancellation, runner
  adaptation, and control lifecycle retain their current behavior;
- Tool invocation, events, dispatch, validation, and truncation retain their
  current behavior;
- LocalGitWorkspace preserves review-freeze and post-review tamper protection;
- Ranking, Budget, and Evaluation tests pass from their new canonical owners.

Final verification commands:

```powershell
uv run pytest -q tests test/unit
uv run black --check src/athena tests test/unit examples scripts
```

Repository searches must find no production imports of:

```text
athena.core.schemas
athena.core.gitutils
athena.core.research
athena.core.ranking
athena.core.budget
athena.core.evaluation
athena.core.agent.agent
athena.core.agent.subagent
```

Acceptance also requires the obsolete files and directories to be absent, no
new circular imports, no change to ResearchTree v2 fixtures, and no unrelated
worktree changes.
