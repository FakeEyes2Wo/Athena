# Skip-Validate Engineering Simplification Design

**Date:** 2026-09-05
**Status:** Approved for specification
**Scope:** Simplify the completed project-scoped `skip_validate` feature without
changing its user-visible behavior or scientific disclosure.

## Context

The completed feature is behaviorally sound: the GUI stores a project-scoped
`skip_validate` preference, disables but preserves the `auto_validate` choice,
and can complete a settled SEARCH directly with an explicitly unvalidated Final
report. Focused Python and frontend tests pass.

The implementation nevertheless spreads the same setting through too many
internal interfaces. The two wire booleans currently appear in runtime
construction, configuration, mutable runtime options, Supervisor dependencies,
settings mutation, gateway factory injection, phase control, state persistence,
and report generation. The gateway also uses signature reflection to support two
factory call shapes, and GUI state updates rebuild the whole state value in
multiple places.

The active transactional-search-resume backend plan already owns the phase,
settings, transaction, and report paths that this cleanup must change. Its Task
11 also introduces a durable validation policy. This design therefore refines
that active architecture; it must not create a competing state machine or a
temporary report subsystem.

## Goals

1. Keep the existing GUI RPC keys, persisted project preference, restart
   behavior, and honest unvalidated output.
2. Represent validation behavior with one internal policy boundary instead of
   threading independent booleans through each layer.
3. Give each mutable concern one owner: validation policy, GUI preferences, and
   transactional terminal state.
4. Remove runtime signature reflection and weak `Callable[..., ...]` factory
   typing.
5. Make invalid report states unrepresentable at the normal internal API.
6. Keep modules small, names explicit, and private interfaces narrow.
7. Integrate with the active transaction coordinator and the queued experiment
   document projection rather than duplicating either one.

## Non-goals

- No visual redesign of the settings panel.
- No new GUI RPC method or durable core `state.json` field.
- No broad redesign of the already-public `ResearchRuntime` constructor.
- No change to whether skipped validation runs the held-out evaluator: it must
  not run it.
- No deletion of the durable `validation_skipped` resume marker while older
  state remains supported.
- No general cleanup outside the skip-validation data and control flow.

## Compatibility Boundary

The stable boundary remains:

```json
{
  "auto_validate": true,
  "skip_validate": true
}
```

Both values remain necessary at the wire and preference boundary. When skipping
is enabled, `auto_validate` records the user's latent preference so disabling
skip can restore the prior automatic-validation choice.

`ResearchRuntime` is an exported composition root and existing scripts construct
it with keyword arguments. Its `auto_validate` and `skip_validate` keywords stay
as a compatibility adapter for this change. They are normalized immediately and
must not be forwarded independently after runtime composition.

Private gateway factory injection is not a public compatibility boundary. Its
old positional/optional-keyword call shapes may be replaced, and repository
tests must use the new typed shape.

## Validation Policy Model

Add one small policy module owned by the research runtime. It defines:

```python
class ValidationMode(StrEnum):
    MANUAL = "manual"
    AUTOMATIC = "automatic"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class ValidationPolicy:
    auto_validate: bool = False
    skip_validate: bool = False

    @property
    def mode(self) -> ValidationMode:
        if self.skip_validate:
            return ValidationMode.SKIP
        if self.auto_validate:
            return ValidationMode.AUTOMATIC
        return ValidationMode.MANUAL
```

`RuntimeOptions.validation_policy` is the single live policy owner. A settings
mutation constructs a new immutable policy, commits the named runtime-settings
transaction, and replaces that one field only after the commit succeeds.
`PhaseActions` receives one zero-argument `validation_mode` callback that reads
`runtime.session.options.validation_policy.mode`; it stores no policy booleans
and no policy copy. This keeps phase decisions current without introducing a
second mutable owner.

The policy accepts validated updates for either preference independently. Setting
`skip_validate=True` never rewrites `auto_validate`. Consequently all four wire
combinations are parseable, while only the derived three-state `mode` controls
workflow behavior.

Internal phase code branches only on `ValidationMode`. Raw `skip_validate`
checks are limited to compatibility parsing, GUI preference serialization, and
legacy state/report migration boundaries.

## Runtime Composition

Runtime construction performs these steps once:

1. Validate the two public compatibility keywords.
2. Construct the canonical `ValidationPolicy`.
3. Give runtime settings and phase control access to the same policy owner.
4. Project the policy back to the two existing keys only when returning a GUI
   settings snapshot.

`ResearchConfig` stores the initial immutable policy and retains read-only
`auto_validate` and `skip_validate` compatibility properties for callers that
inspect `runtime.config`. `RuntimeOptions` stores the live policy instead of
another pair of booleans. `PhaseActions` exposes only the narrow
`validation_mode` reader, not validation preferences. `Supervisor.configure_options`
no longer exposes separate validation keyword arguments. The settings coordinator
applies a validated policy patch through one named mutation.

## Gateway Construction and Preference Storage

Replace `_build_runtime` signature inspection with one private immutable request:

```python
@dataclass(frozen=True, slots=True)
class RuntimeLaunch:
    project_root: Path
    state_root: Path | None
    validation: ValidationPolicy


class RuntimeFactory(Protocol):
    def __call__(self, launch: RuntimeLaunch) -> ResearchRuntime: ...
```

The production factory translates `RuntimeLaunch` into the public
`ResearchRuntime` compatibility call. `GuiRequestHandler` always calls exactly
one factory shape. It does not import `inspect`, guess callable capabilities, or
silently omit the project preference.

`GuiStateStore` owns updates to GUI preference state. Provide narrow named
operations for the mutations the handler needs:

- set the active project;
- remember the last session for one project;
- remember the skip-validation preference for one project.

Each operation performs one load-modify-atomic-save cycle and preserves unrelated
fields. The handler no longer constructs full `GuiState` values. A same-process
lock protects the update cycle; the existing atomic replacement continues to
protect readers from partial files.

The authoritative value returned by `settings_set` remains the value persisted
by the gateway. Rejected or normalized settings are never persisted from the
untrusted request patch.

## Transactional Skip Finalization

The active transaction coordinator becomes the sole owner of terminal state
mutation. The skip path is one named mutation, not a manual snapshot/restore block
inside `PhaseRunner`.

The flow is:

1. Read a stable SEARCH/SOTA snapshot and ensure all SEARCH plans are settled.
2. Render the skipped-validation report projection outside the transaction lock.
3. Submit one coordinator mutation containing the expected context version,
   selected SOTA identity, derived report bytes, skipped Final-stage document,
   and terminal state changes.
4. Revalidate SEARCH/RUNNING, the SOTA evaluation, absence of validation, and
   absence of live plans inside the transaction.
5. Atomically commit `phase=COMPLETED`, `status=COMPLETED`,
   `validation_skipped=True`, and the derived artifacts.
6. Publish output and state only after commit. Publication failure is logged and
   does not roll back a durable success.

No held-out evaluator, validation checkpoint, or fabricated final score is
created. A failed commit leaves the prior state and report projection intact.

## Report Input Model

Replace parallel `validation` and `validation_skipped` parameters with one
validated value:

```python
class ValidationReportStatus(StrEnum):
    ABSENT = "absent"
    SKIPPED = "skipped"
    VALIDATED = "validated"


@dataclass(frozen=True, slots=True)
class ValidationReportInput:
    status: ValidationReportStatus
    result: Mapping[str, object] | None = None
```

Construction rejects a result for `ABSENT` or `SKIPPED` and requires a result for
`VALIDATED`. Report builders receive only this value, so callers cannot express
"validation is present and skipped".

The durable state-to-report boundary performs the one conversion from
`state.validation` and the legacy `state.validation_skipped` marker. The queued
experiment-document projection consumes the same value; this cleanup must avoid
deepening `exp_docs.py`, which that queued plan replaces.

## UI

The current two switches remain. The automatic-validation switch stays disabled
while skip is active, but its checked value is preserved. The explanatory text
remains associated through `aria-describedby`.

Frontend code may derive `validationMode` locally for display, but it does not
replace the two persisted preference fields with a single mode because that would
discard the latent automatic-validation choice.

## Error Handling

- Non-boolean wire values fail at the settings boundary before policy mutation.
- A factory always receives an explicit policy; there is no fallback construction
  path that drops the preference.
- GUI preference update failure returns an RPC error and keeps the prior readable
  state file.
- Transaction precondition failure returns a typed rejection and performs no
  state/report mutation.
- Report input inconsistency fails at construction, not in multiple render
  functions.
- Post-commit publication failures are observable warnings, never false rollback
  claims.

## Verification

Implementation is complete only with fresh evidence for all of the following:

1. Policy unit tests cover all four boolean inputs and all three derived modes.
2. Toggling skip on and off preserves the original `auto_validate` preference.
3. Runtime construction normalizes once; internal phase dependencies expose no
   raw `skip_validate` or `auto_validate` fields.
4. Gateway factory tests prove one typed call shape and project/session restart
   restoration without signature reflection.
5. GUI state update tests prove each named mutation preserves every unrelated
   field and rejects malformed persisted preferences safely.
6. Transaction tests prove successful atomic finalization, rollback on state or
   report write failure, stale-context rejection, and post-commit publication
   behavior.
7. Evaluator contract tests prove the held-out evaluator is never invoked on the
   skip path.
8. Report tests prove skipped disclosure, validated disclosure, and rejection of
   invalid report inputs.
9. Existing GUI component, bridge, gateway, breakpoint-resume, Supervisor, report,
   and autonomous-research tests pass.
10. Ruff/format checks pass for every changed Python file; TypeScript compilation,
    focused frontend tests, GUI build, Rust tests, and `cargo check` pass.
11. An architecture test or equivalent structural assertion prevents reintroducing
    factory reflection and raw validation booleans into internal phase interfaces.

## Delivery Sequence

1. Incorporate this design into the active transactional backend plan before its
   validation-policy task is implemented.
2. Land the runtime policy, gateway factory, and GUI state ownership changes only
   after the active branch contains the completed skip-validation commits.
3. Implement skip finalization through the transaction coordinator in the active
   plan's serialization/final-validation tasks.
4. Keep the existing GUI wire shape through the dependent frontend plan.
5. Make the queued experiment-document projection consume
   `ValidationReportInput` and remove obsolete compatibility adapters there.

This order yields one final architecture and avoids a temporary second
transaction, report, or settings subsystem.
