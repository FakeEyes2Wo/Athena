# Experiment Document Projection Redesign

Date: 2026-09-04
Status: design approved; written-spec review pending

## 1. Problem and objective

`src/athena/research/exp_docs.py` was restored to make the research runtime
importable, but it is still a monolithic recovery implementation. It mixes metric
discovery, untyped stage-record construction, Markdown rendering, path selection,
and persistence behind three free functions. Its current behavior has four concrete
correctness problems:

- it looks for `.athena/evaluator_spec.json`, which no producer writes, instead of
  reading the frozen evaluator under `workspaces/evaluator`;
- unsafe `run_id` and `stage` values silently collapse to `run` and `stage`, allowing
  unrelated records to overwrite one another;
- SEARCH, PREPARE, and VALIDATE can write derived documents before their canonical
  `ResearchTree` and `ResearchState` checkpoints are durable;
- JSON files are replaced atomically, but the two Markdown reports are not, and a
  multi-file projection has no completion marker.

The replacement will make experiment documents an explicit, recoverable projection
of canonical research state. It will preserve the useful disk contract while making
projection failures non-fatal to research execution.

## 2. Decisions and non-goals

The following decisions are fixed:

- `ResearchTree` plus Supervisor `ResearchState` remain the only canonical research
  state. Files below `exp_docs` are derived views and must never authorize a phase
  transition, SOTA decision, or recovery decision.
- The monolithic `src/athena/research/exp_docs.py` module will be deleted and replaced
  by a focused `athena.research.experiment_documents` package.
- The old Python free-function API (`task_metric_name`, `write_stage_doc`, and
  `write_reports`) will not receive a compatibility shim. The two production callers
  will migrate atomically in the same change.
- The existing human- and test-facing layout remains available:

  ```text
  .athena/exp_docs/
    runs/<run_id>.json
    baseline.json
    search.json
    final.json
    latest.json
    FINAL_REPORT.md
    OPTIMIZATION.md
  ```

- Existing run files will not be bulk-migrated or deleted. Normal projection and
  recovery rebuild repair derivable aliases and reports and create missing canonical
  run records, while preserving unknown historical records.
- No GUI protocol, `ResearchTree` schema, `ResearchState` schema, artifact-store
  contract, or phase-policy change is part of this work.
- The concurrency guarantee covers all projections made by one Supervisor runtime.
  Running multiple independent Athena processes against the same state root remains
  outside the supported single-writer model.

This is not a general document-generation framework. It is a narrow boundary for the
baseline, search, and final research views already produced by Athena.

## 3. Package structure and ownership

The new package has one responsibility per module:

```text
src/athena/research/experiment_documents/
  __init__.py
  models.py
  metric.py
  render.py
  store.py
  projector.py
```

### `models.py`

Defines strict, frozen Pydantic value objects for a stage event, its metric, reason,
provenance, persisted `StageRecord`, `LatestManifest`, and `ProjectionOutcome`.
Models use `strict=True` and `extra="forbid"`. They reject blank identifiers and
metric names, non-finite numeric values, malformed artifact references, and unknown
fields.

`run_id` must match `^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`, must not contain `..` or end
in `.`, and must not form a Windows reserved device name; it is rejected rather than
rewritten. `stage` is exactly `baseline`, `search`, or `final`, so it can safely select
one alias filename. `status` is a bounded uppercase token. A second resolved-path
containment check remains in the store as defense in depth.

Persisted new records include `schema_version: 1`. The record keeps the existing
conceptual fields: `run_id`, `stage`, `status`, `metric`, `artifacts`, `reason`, and
`provenance`. Optional metric values are represented by JSON `null`, never NaN or
infinity. `provenance` has explicit optional fields for the currently emitted
baseline, search, final, and phase-failure identities; it is not an arbitrary payload
bag.

The model shapes are:

```python
class MetricObservation(BaseModel):
    primary: float | None = None
    reference: float | None = None
    generalization_gap: float | None = None
    secondary: dict[str, float] = {}

class StageEvent(BaseModel):
    run_id: str
    stage: Literal["baseline", "search", "final"]
    status: str
    metric: MetricObservation
    artifacts: dict[str, str] = {}
    reason: ReasonRecord
    provenance: ProvenanceRecord

class StageRecord(StageEvent):
    schema_version: Literal[1] = 1
    metric: MetricRecord  # observation plus resolved name and direction
```

The displayed `{}` defaults above describe serialized empty mappings; implementation
uses factories, not mutable class defaults. `ReasonRecord` contains bounded nonblank
`kind` and `summary`. `ProvenanceRecord` contains optional `phase`, `experiment_id`,
`hypothesis_id`, `commit`, `sota_experiment_id`, `sota_commit`, and
`validation_commit` strings. Artifact keys and values are bounded, nonblank,
single-line strings.

The normal-stage event can enrich an archive with non-authoritative settlement
context, such as the comparison reference and human-readable reason. Those values are
audit context, not additional canonical state. Recovery reproduces only the subset
derivable from the tree/state snapshot and labels unavailable context explicitly.

### `metric.py`

Owns `MetricResolver`. It receives the configured evaluator workspace roots from the
composition root and resolves the display name in this order:

1. a valid, nonblank `primary_metric` in the frozen SEARCH evaluator's
   `evaluate/metric.json`;
2. a valid, nonblank `primary_metric` in the legacy flat evaluator
   `metric.json`;
3. the confirmed task-understanding `primary_metric`;
4. the literal fallback `primary`.

The resolver reuses evaluator parsing where possible, treats unreadable or invalid
metadata as unavailable with a diagnostic warning, and never reads
`.athena/evaluator_spec.json`.

### `render.py`

Contains pure functions that turn validated records and an immutable canonical
snapshot into UTF-8 bytes. All output is rendered before persistence starts.

- Stage JSON uses deterministic key ordering, two-space indentation, LF newlines, a
  final newline, and JSON serialization with non-finite values forbidden.
- `FINAL_REPORT.md` delegates to `athena.research.report.build_final_report`. Given the
  same tree and validation snapshot, the disk report and GUI report must therefore be
  byte-identical.
- `OPTIMIZATION.md` retains the current direction-aware score ordering, SOTA marker,
  unscored-experiment section, and VALIDATE summary. Dynamic table text is escaped so
  it cannot break Markdown structure.
- `LatestManifest` is rendered from the effective bytes selected during store
  preflight, including the existing bytes of an idempotent immutable run record.

### `store.py`

Owns path validation, immutable run archives, the per-root projection lock, unique
temporary files, flush/fsync, atomic replace, manifest hashing, and temp cleanup. It
does not know about Supervisor phases or metric policy.

### `projector.py`

Owns `ExperimentDocumentProjector`, the only production facade. It validates a stage
event, captures the tree/validation/task-understanding inputs by value, resolves the
metric name, asks the renderer for the complete batch, and asks the store to commit
it. It also implements best-effort rebuild from canonical state.

`__init__.py` exports the projector protocol, concrete projector, typed stage event,
and outcome. It does not re-export the deleted free functions.

## 4. Injected interface

Supervisor collaborators depend on a small protocol rather than constructing paths or
calling persistence helpers:

```python
class DocumentProjector(Protocol):
    def project_stage(
        self,
        event: Mapping[str, object],
        *,
        tree: ResearchTree,
        validation: Mapping[str, object] | None,
        task_understanding: Mapping[str, object] | None,
        direction: Literal["maximize", "minimize"],
    ) -> ProjectionOutcome: ...

    def rebuild(
        self,
        *,
        tree: ResearchTree,
        validation: Mapping[str, object] | None,
        task_understanding: Mapping[str, object] | None,
        direction: Literal["maximize", "minimize"],
    ) -> ProjectionOutcome: ...
```

The mapping enters the safe facade before strict model construction so validation
errors cannot escape the projection boundary. Both public methods catch ordinary
projection exceptions, log the detailed traceback, and return a fixed failure outcome;
they do not catch process-control exceptions such as `KeyboardInterrupt` or
`SystemExit`.

`SupervisorRuntime` gains one required `documents: DocumentProjector` field. The
runtime composition root constructs exactly one concrete projector with:

- document root `project_root/.athena/exp_docs`, preserving the current disk path;
- evaluator candidates derived from the configured `ResearchPaths.workspaces`, so
  named GUI sessions still find their own frozen evaluator workspace.

Tests inject recording or failing protocol implementations. `PhaseMachine` and
`PlanSettlement` no longer import any experiment-document implementation module.

## 5. Projection batch and commit marker

For a normal stage event, the projector prepares the four non-manifest candidates in
memory:

1. `runs/<run_id>.json`;
2. the matching stage alias (`baseline.json`, `search.json`, or `final.json`);
3. `FINAL_REPORT.md`;
4. `OPTIMIZATION.md`.

`latest.json` contains `schema_version`, projection kind, `stage`, `run_id`, and a
sorted map from every non-manifest path in that batch to its SHA-256 digest. Its
`projection_id` is the SHA-256 digest of that canonical manifest content, not a clock
or random UUID. Repeating the same semantic projection therefore produces identical
bytes and the same projection identity. For `kind="stage"`, `stage` and `run_id` are
required. For `kind="rebuild"`, both are JSON `null` because the batch can cover
several stages.

The store acquires its one projection lock before inspecting or writing targets. It
preflights every path and immutable-run conflict. When an immutable run record already
has the same semantics, its existing bytes become the effective bytes and the archive
is omitted from replacement. The store then hashes the effective batch, asks the pure
manifest renderer for `latest.json`, creates and fsyncs a unique temporary file beside
each target, and only then begins replacement. Targets are replaced in the order shown
above, with `latest.json` always last.

Individual files are atomic. A filesystem cannot atomically rename several unrelated
paths as one transaction, so `latest.json` is the batch commit marker: if any earlier
replace fails, the previous manifest remains authoritative. Some fixed aliases or
reports can then contain newer bytes, but their digests will not match the committed
manifest; recovery treats that as an incomplete projection and rebuilds it. Temporary
files are removed on every ordinary failure path.

All stage projections and rebuilds share the same lock. If concurrent SEARCH
settlements arrive, the later lock holder observes the earlier completed state and
commits after it. Consequently `latest.json` always names the last successfully
completed projection, and batches cannot interleave.

## 6. Immutable run and compatibility rules

`runs/<run_id>.json` is append-only by identity:

- if the path does not exist, the new record is created;
- if it contains the same semantic record, projection is idempotent and leaves the
  archive bytes unchanged;
- if it contains different semantic content, the store reports
  `RunDocumentConflict` and does not overwrite it or commit `latest.json`.

Semantic comparison parses both sides into normalized records rather than comparing
formatting. A narrow compatibility parser accepts the exact unversioned record shape
written by the restored `exp_docs.py`, supplies `schema_version: 1` in memory, and
compares its known fields. Unsupported or malformed legacy content remains untouched
and conflicts with a new record of the same identity. It is never silently repaired,
renamed, or discarded.

Stage aliases and Markdown reports are replaceable projections. They are rewritten
after every successful stage event or rebuild. Unknown files in `runs/` are never part
of cleanup and are not listed in a newly generated manifest unless canonical state
selects them.

## 7. Canonical-first Supervisor flows

Every call site follows one invariant: complete the relevant canonical mutation and
required durable checkpoint before attempting document projection.

### PREPARE baseline

The Supervisor records the baseline hypothesis/experiment/SOTA, records
`evaluator_ref` in `ResearchState`, saves the tree and state, and only then projects
the baseline event and reports. Publishing completion and transitioning to SEARCH
remain after that checkpoint. A document failure cannot undo the trusted baseline.

### SEARCH settlement

Settlement updates the experiment, hypothesis verdict/priority, and SOTA pointer,
removes the settled active Plan, then saves the tree and state. Only after both saves
succeed does it project the search event. Lease release and Agent reaping retain their
current lifecycle semantics. A projection failure cannot restore the Plan, old SOTA,
or old hypothesis status.

### VALIDATE completion

The report artifact is stored as today. The Supervisor then records validation,
`phase=COMPLETED`, and `status=COMPLETED`, saves state, and projects the final event.
The session result is published independently of projection success.

### Phase failure

The original phase exception still determines `FAILED`. The Supervisor saves that
canonical status first and then asks the projector for the appropriate baseline,
search, or final failure record. If this derived write also fails, it cannot replace
the original exception, change the phase/status, or escape the existing failure
handler. A failure archive ID is
`<stage>-phase-failure-<digest>`, where `digest` is the first 12 hexadecimal characters
of SHA-256 over the normalized exception class and full message. Repeating the same
failure is idempotent; a later, different failure receives a distinct immutable run
record while the stage alias advances.

If a canonical save itself fails, no document projection is attempted. Canonical
persistence errors retain the Supervisor's existing failure behavior; this design
does not disguise them as projection warnings.

## 8. Failure semantics and observability

`ProjectionOutcome` exposes only success or the stable warning code
`experiment_documents_stale` and this fixed message:

> Experiment documents could not be refreshed; canonical research state is safe and
> the documents will be rebuilt on recovery.

Its exact fields are `ok: bool`, `projection_id: str | None`,
`warning_code: Literal["experiment_documents_stale"] | None`, and
`warning_message: str | None`. Success requires a projection ID and no warning fields;
failure requires both fixed warning fields and no projection ID. It carries no
exception, filesystem path, or partially written file list.

The projector logs the concrete exception, operation, stage, and run identity with a
traceback for operators. Raw exception text is not copied into user-facing events.
Each Supervisor caller emits the fixed message once through the existing `output`
event with `channel="error"`; using that existing transport avoids a GUI protocol
change, while the canonical state remains non-failed. Failure to publish this warning
is itself caught and logged so it cannot replace the research result or original phase
exception. Callers must not set the research phase to `FAILED`, roll back
tree/state/SOTA, or raise the projection exception.

Expected failures include invalid event data, unsafe paths, immutable-run conflicts,
rendering errors, permission errors, short writes, fsync failures, and replace
failures. They all have the same research-level semantics: canonical progress stands,
the last valid `latest.json` stands when one exists, and recovery repairs the derived
view when canonical inputs are sufficient.

## 9. Recovery and rebuild

At lifecycle start, after `ResearchTree` and `ResearchState` have been loaded and
before new phase work starts, Athena performs one best-effort rebuild when canonical
state or an existing document root is present. For a resumed non-PREPARE run,
`Supervisor.recover()` first completes Plan/tree recovery and then rebuilds documents.
For a PREPARE run, `PhaseMachine.start()` rebuilds before `_run_prepare()`. Both paths
can emit the fixed warning through the existing asynchronous event publisher. A
brand-new empty project creates no document tree until it has a stage result.

Rebuild runs under the same store lock and follows these rules:

- regenerate `FINAL_REPORT.md` and `OPTIMIZATION.md` from the loaded canonical
  snapshot;
- regenerate baseline, search, and final aliases when their source is derivable;
- create a missing run record for each derivable terminal experiment or final
  validation result;
- use `exp_baseline` for the baseline; select the most recently inserted terminal
  non-baseline experiment in `ResearchTree` order for the search alias; and use the
  persisted validation `result_id` (or `final` when absent) for final validation;
- when SEARCH reference values or historical reasons are not recoverable, use `null`
  and the explicit reason `reconstructed_from_canonical_state` rather than inventing
  evidence;
- do not reconstruct phase-failure exception text, because it is not canonical;
- preserve every existing run record. If a selected run exists, use its compatible
  validated content; if it conflicts with derivable canonical facts, abort that
  rebuild batch, report the conflict, retain the prior manifest, and leave the run
  untouched;
- commit a fresh content-derived `latest.json` only after the complete rebuild batch
  succeeds.

The rebuild is repair, not migration. It does not rewrite compatible legacy archives
just to add `schema_version`, and it never deletes records that the current tree can no
longer explain.

## 10. Migration sequence

Implementation will be one behavior-complete migration:

1. Add the package, strict models, resolver, pure renderers, atomic store, projector,
   and focused unit tests.
2. Add the injected projector protocol to `SupervisorRuntime`; update the production
   composition root and all test dependency builders.
3. Move PREPARE, SEARCH, VALIDATE, and phase-failure call sites to canonical-first
   ordering and the injected projector.
4. Add startup rebuild after canonical state loading.
5. Delete `src/athena/research/exp_docs.py` and prove no old imports remain.
6. Run focused, integration, import-smoke, and full-suite verification before closing
   the implementation plan.

`test/__init__.py`, restored with the original module, remains unchanged. No old
module shim or deprecation period is needed because repository search shows only the
two internal production consumers.

## 11. Test strategy

Focused model tests cover valid records plus invalid stage names, unsafe/blank run
identities, blank metric names, NaN/infinity at every numeric position, malformed
artifact references, invalid provenance, and extra fields.

Store tests cover:

- creation of the preserved directory layout;
- semantic idempotency across different JSON formatting and the supported legacy
  shape;
- same-run/different-content conflict without overwrite;
- containment checks and unique temporary names;
- injected failures before replacement, midway through replacement, and while
  replacing `latest.json`;
- cleanup of temporary files and preservation of the old manifest;
- two concurrent projections producing non-interleaved batches whose manifest names
  the last completed one.

Renderer and resolver tests cover both evaluator locations, confirmed-task fallback,
the literal fallback, invalid evaluator metadata, direction-aware ordering, Markdown
escaping, and exact equality between GUI and disk final-report bytes for the same
snapshot.

Supervisor tests use recording and failing projector doubles to prove:

- tree/state saves happen before baseline, search, final, and failure projection;
- a projection exception outcome never changes phase, status, Plan removal,
  hypothesis status, or SOTA;
- the original phase exception remains the reported cause when failure-document
  projection also fails;
- recovery rebuild preserves unknown history and repairs aliases/reports from
  canonical state.

Existing autonomous-research integration tests continue to assert `runs/*.json`,
`baseline.json`, `search.json`, `final.json`, `FINAL_REPORT.md`, and
`OPTIMIZATION.md`, and gain assertions for a valid `latest.json`. Import smoke tests
cover `athena.research.supervisor` and the CLI entry point. A static repository check
must find no import of `athena.research.exp_docs` and no definition or call of its
three deleted free functions.

Final verification consists of the focused experiment-document tests, affected
Supervisor unit tests, research integration tests, CLI/Supervisor import smoke, and
the full pytest suite. Environment-only failures, such as a missing PowerShell 7
binary in an optional integration test, must be recorded separately and must not be
reported as product failures or silently ignored.

## 12. Acceptance criteria

- `src/athena/research/exp_docs.py` is gone and production uses the injected
  `ExperimentDocumentProjector` exclusively.
- The existing experiment-document paths and meanings remain available, with
  `latest.json` identifying the last complete, digest-verifiable batch.
- Frozen evaluator metadata is the first metric-name authority; the nonexistent
  `.athena/evaluator_spec.json` path is unused.
- Invalid identities and records are rejected without fallback filenames or archive
  overwrite.
- Same-identity/same-content projection is idempotent; same-identity/different-content
  projection is a visible conflict and preserves the archive.
- Canonical tree/state checkpoints precede every derived document projection.
- Any ordinary projection failure leaves canonical progress and phase semantics
  intact, preserves the previous commit marker, emits only the sanitized warning at
  the user boundary, and retains detailed operator diagnostics.
- All JSON and Markdown targets use prepared bytes and atomic per-file replacement;
  concurrent projections from one Supervisor runtime cannot interleave.
- Recovery can rebuild all derivable current views without deleting or rewriting
  unknown historical run files.
- Disk and GUI final reports are byte-identical for the same canonical snapshot.
- Focused, Supervisor, integration, import-smoke, and full-suite verification provide
  fresh passing evidence, with any environment limitation called out explicitly.
