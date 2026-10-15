# Research and Agent review ledger

Scope: `src/athena/research/`, `src/athena/agents/`, and dedicated research/agent
tests. This records the older module-simplification inventory's remaining
Pending rows and current dirty-boundary rechecks. The older ledger remains the
source for files it already marked Reviewed; those decisions were retained
unless a current diff or caller required the scoped edits listed below.

## Pending research modules

| File | Decision and production caller evidence | Verification |
| --- | --- | --- |
| `research/runtime/facade.py` | Retain as the public composition facade. `ResearchRuntime` constructs config/services/workflow once; runtime bootstrap, CLI and GUI access its properties and lifecycle/settings methods. Its apparent forwarding methods are consumed by turn runners, supervisor phases, GUI and gateway callers; merging them would couple those public boundaries to storage records. | `tests/unit/research/test_runtime_settings.py`, `test_runtime_ideators.py`, `test_runtime_survey.py`, `test_breakpoint_resume.py`; `tests/integration/research/test_continue_resume_surfaces.py`. |
| `research/script_runner.py` | Retain `DataScriptRunner` as owner of bundle freeze/run and live evaluator-copy execution. PREPARE and SEARCH use frozen bundles; evaluator and VALIDATE paths use `run_dir`. Persistent frozen-run directories are intentional: `test_run_frozen_bundle_uses_configured_workdir` asserts retention for inspection/recovery, while live evaluator copies are removed in `finally`. Kept explicit `check=False` because the wrapper maps return codes to `ScriptCommandError` itself. | `tests/unit/research/test_data_scripts.py`; evaluator/property tests under `tests/unit/research/supervisor/`. |
| `research/splitter.py` | Retain the deterministic split primitive. PREPARE owns its call; split features/labels, row IDs and manifest hashes are consumed by prediction-contract validation and frozen evaluator coverage. Group-level assignment and stable ordering are separate correctness policy, not duplicate CSV convenience. | `tests/unit/research/test_splitter.py`, `tests/unit/research/test_prediction_contract.py`, `tests/unit/research/test_data_contract_reaches_candidates.py`. |
| `research/supervisor/run_state.py` | Retain process-local task/worktree/wake/guidance state separately from durable `ResearchState`; `Supervisor` and `SearchLoop` consume snapshots and wake operations. Moving these fields into persisted state would serialize asyncio objects or conflate live resources with recovery data. | `tests/unit/research/supervisor/test_running_items.py`, `test_phase_suspend.py`; `tests/integration/research/test_search_recovery.py`, `test_human_plan_boundary.py`. |
| `research/supervisor/scheduling.py` | Retain policy/ranker/action projection as one deterministic scheduling boundary. `SearchLoop._fill_slots` consumes actions; `PlanSettlement` consumes the policy outcome; this is shared policy, not a one-caller wrapper. | `tests/unit/research/supervisor/test_ranker.py`, `test_supervisor.py`; `tests/integration/research/test_rolling_search.py`, `test_human_plan_boundary.py`. |
| `research/supervisor/search_loop.py` | Retain the long-lived SEARCH owner for scheduling, pause/wake/cancel races, bounded diagnostics, infra parking and settlement dispatch. `Supervisor` delegates lifecycle/control calls here; the task set and durable state have separate owners. | `tests/unit/research/supervisor/test_supervisor.py`, `test_running_items.py`, `test_phase_suspend.py`; `tests/integration/research/test_rolling_search.py`, `test_search_recovery.py`, `test_human_plan_boundary.py`, `test_hypothesis_repair_cycle.py`. |
| `research/supervisor/settlement.py` | Retain `PlanSettlement` as the single trusted-result projection path. Search delegates completion here; it reads only the stored best result, applies uncertainty/reference policy, saves canonical tree/state before derived document/event projection, then reaps the Agent. Ordering guards durable scores from projection failures. | `tests/unit/research/supervisor/test_supervisor.py`, `test_document_projection.py`; `tests/integration/research/test_rolling_search.py`, `test_search_recovery.py`. |
| `research/supervisor/state.py` | Retain strict durable state and digest-bound resume-file migration in one persistence owner. `ResearchRuntime` loads/saves it and `SupervisorRunState` reads it through a callback. The compatibility migration and backup/quarantine behavior are explicitly covered and are not generic schema machinery. | `tests/unit/research/test_breakpoint_resume.py`, `test_running_items.py`, `test_runtime_settings.py`; `tests/integration/research/test_task_seeding.py`. |
| `research/supervisor/statistics.py` | Retain pure statistical verdict helpers separate from scheduling; `settlement.py` is their production caller. The file owns confidence intervals and uncorrected evidence p-values; Bonferroni is applied once at the decision boundary. | `tests/unit/research/supervisor/test_supervisor.py` statistical-settlement cases. |
| `research/supervisor/supervisor.py` | Retain the public lifecycle facade. Runtime composition constructs it; app/runtime callers use stable methods while `PlanRuntime`, `SearchLoop`, and `PhaseMachine` own internal policies. Removing the facade would expose three private lifecycle owners to application callers. | `tests/unit/research/supervisor/test_supervisor.py`, `test_phase_suspend.py`; integration research lifecycle/recovery suites. |
| `research/supervisor/validation.py` | Retain `ValidationSession`'s recoverable run/score/commit sequence. `PhaseMachine` creates `ValidationDeps/Input/Options`; FINAL prediction contract is derived from the frozen evaluator, output freshness is checked, reviewed diff is bound to commit evidence, and results are checkpointed by stable key. Error handling is scoped to corrupt durable artifacts, output freshness, review rejection, agent reap, and commit evidence respectively. | `tests/unit/research/supervisor/test_validate_predicts_heldout.py`; `tests/integration/research/test_validate_agent_contract.py`, `test_human_plan_boundary.py`, `test_autonomous_research.py`. |
| `research/supervisor/validation_contracts.py` | Retain typed logical input, injected collaborators and per-attempt options as distinct records. `validation.py` consumes them; logical identity cannot accidentally absorb execution-only timeout/feature-path settings. | Same VALIDATE unit/integration suites above. |
| `research/turns/ideator.py` | Retain lane orchestration. `AgentTurnRunner` composes the mixin and runtime bootstrap registers `run_ideator_turn`; each lane owns a unique workspace and one-shot Agent, while the separate source/evidence verifier remains in `turns/support.py`. Bounded gate retries, peer-lane success, cancellation propagation and reaping are intentional. | `tests/unit/research/test_runtime_ideators.py`, `test_runtime_eval_handoff.py`, `test_runtime_survey.py`; `tests/unit/research/supervisor/test_ideator_wiring.py`. |
| `research/supervisor/evaluator_plan.py` | Retain one evaluator freeze workflow. Removed `_validate_frozen_evaluator`'s optional `layout=None` test-compatibility branch: no production caller needed that fallback, while `run_evaluator_plan` already resolves the layout once and passes it through validation/README/descriptor. Tests now explicitly pass `_evaluator_layout(root)`, proving one frozen contract per submission. | `tests/unit/research/supervisor/test_evaluator_plan_column.py`, `test_prepare_plan.py`. |

## Pending Agent modules and prompt files

| File | Decision and caller evidence | Verification |
| --- | --- | --- |
| `agents/__init__.py` | Retain minimal package exports; `research/turns/ideator.py` imports `Ideator` through this package. | `tests/unit/test_agent.py`; `tests/unit/research/test_runtime_ideators.py`. |
| `agents/ideator/__init__.py` | Retain focused debate-Ideator API export, distinct from prompt-agent registrations in `ideator_agent.py`. | `tests/unit/test_agent.py`; debate branch in `tests/unit/research/test_runtime_survey.py`. |
| `agents/ideator/ideator.py` | Retain proposal/review/revision/judge state machine; its injected agent factory and structured stage contracts are consumed by the explicit `--ideation debate` branch. Cancellation is re-raised and provider/judge validation errors retry within bounded stage policy; changed `BaseException` to `Exception` so process-level exits are not swallowed. | `tests/unit/test_agent.py` structured-output cases; `tests/unit/research/test_runtime_survey.py` debate branch. |
| `agents/ideator/types.py` | Retain public `DebateResult` response shape used by the debate orchestration and artifact result path; the nested stage schemas remain implementation-private. | `tests/unit/test_agent.py`; `tests/unit/research/test_runtime_survey.py`. |
| `agents/ideator_agent.py` | Retain profile-to-prompt/output registration boundary. `research/turns/ideator.py` binds baseline, exploit, bold and moonshot profiles; profile keeps workspace, gated output schema and prompt selection consistent. | `tests/unit/research/test_runtime_ideators.py`, `test_runtime_settings.py`; baseline prompt contract tests. |
| `agents/kaggle_handoff_agent.py` | Retain isolated evidence contract and registration adapter; `turns/general.py` and ideator handoff flow consume the registered type, while evidence records only items actually read. | `tests/unit/research/test_kaggle_handoff_agent.py`, `test_runtime_survey.py`. |
| `agents/prepare_agent.py` | Retain small registration entrypoints because PREPARE, evaluator, EDA orchestrator and EDA worker bind different schemas/tool restrictions; shared construction remains in `register_prompt_agent`. | `tests/unit/agent/test_prepare_agent.py`; `tests/unit/research/test_prepare_evaluator_isolation.py`. |
| `agents/prompt_agent.py` | Retain the shared prompt/factory owner; runtime/bootstrap and task-specific registration call it. Dynamic workspace resolution, runtime shell binding and lazy tools are required by per-Plan isolation and late-built corpus/Kaggle tools. | `tests/unit/agent/test_generic_tools.py`, `test_plan_agent.py`, `test_prepare_agent.py`; `tests/unit/idea_generation/test_ideator_profiles.py`. |
| `agents/supervisor_agent.py` | Retain long-lived serialized SupervisorAgent and typed decision tools; the runtime registers it once and exposes lifecycle/search/hypothesis operations via the `SupervisorActions` protocol. | `tests/unit/agent/test_supervisor_agent.py`; `tests/unit/research/supervisor/test_supervisor.py`. |
| `agents/task_agents.py` | Retain related data/general/validate/plan registrations and their distinct result contracts; callers are turn runners, `PlanRuntime` and VALIDATE phase bootstrap. | `tests/unit/agent/test_plan_agent.py`, `test_validate_agent.py`; `tests/unit/research/supervisor/test_plan_turn_streaming.py`, `tests/unit/research/test_breakpoint_resume.py`. |
| `agents/tools/__init__.py` | Retain package marker; tools are consumed from the concrete `generic_tools` module. | `tests/unit/agent/test_generic_tools.py`. |
| `agents/tools/generic_tools.py` | Retain workspace-bounded read/write/append and execution-runtime shell adapter. Registrations bind per-agent workspaces; `.athena` remains runtime-owned and read-only. | `tests/unit/agent/test_generic_tools.py`, `test_plan_agent.py`, `test_framework_ownership.py`; `tests/unit/research/test_runtime_ideators.py`. |
| `agents/prompts/baseline_ideator_agent.md` | Retain source-grounded baseline research instructions; registered by `BASELINE_IDEATOR_PROFILE`, called from PREPARE baseline orchestration. | `tests/unit/research/prepare/test_baseline_prompt_contract.py`; `tests/integration/research/test_authoritative_baseline_gate.py`. |
| `agents/prompts/data_agent.md` | Retain Data Agent EDA/report contract; registered by `register_data_agent` for `AgentTurnRunner.run_data_turn`. | `tests/unit/agent/test_prepare_agent.py`; `tests/unit/research/test_eda_todo.py`. |
| `agents/prompts/eda_worker_agent.md` | Retain subordinate worker contract, distinct from EDA orchestrator; `register_prepare_eda_agent` binds worker output and workspace. | `tests/unit/agent/test_prepare_agent.py`; `tests/unit/research/test_eda_todo.py`. |
| `agents/prompts/evaluator_agent.md` | Retain authoritative metric/schema instructions; registered by PREPARE evaluator pipeline and enforced again by frozen evaluator validation. | `tests/unit/research/supervisor/test_prepare_prompt_contract.py`, `test_evaluator_plan_column.py`, `tests/unit/research/test_evaluator_spec.py`. |
| `agents/prompts/general_agent.md` | Retain general task research/handoff prompt; `run_general_turn` registers it and consumes `GeneralResult`. | `tests/unit/test_agent.py`; `tests/unit/research/test_runtime_survey.py`. |
| `agents/prompts/ideator_agent.md` | Retain legacy ungated Ideator prompt for baseline ideation mode; profile registration pairs it with `HypothesisBatch`. | `tests/unit/research/test_runtime_ideators.py`; prompt contract tests. |
| `agents/prompts/ideator_gated_agent.md` | Retain gated profile prompt shared by exploit/bold/moonshot profiles; `run_light_pipeline` validates proposals before graph registration. | `tests/unit/research/test_runtime_ideators.py`, `test_ideator_wiring.py`. |
| `agents/prompts/kaggle_handoff_agent.md` | Retain Kaggle evidence collection prompt, registered through `register_kaggle_handoff_agent`; result contract is `KaggleHandoffResult`. | `tests/unit/research/test_kaggle_handoff_agent.py`. |
| `agents/prompts/plan_agent.md` | Retain hypothesis execution prompt; `register_plan_agent` binds one isolated branch and plan-scoped tools, and `PlanRuntime` owns scoring/settlement. | `tests/unit/agent/test_plan_agent.py`; `tests/integration/research/test_hypothesis_repair_cycle.py`, `tests/unit/research/test_data_contract_reaches_candidates.py`. |
| `agents/prompts/prepare_agent.md` | Retain PREPARE task/evidence workflow; `run_prepare_plan` registers it and applies deterministic baseline/source gates. | `tests/unit/research/supervisor/test_prepare_plan.py`, `test_prepare_prompt_contract.py`; baseline gate integration. |
| `agents/prompts/prepare_eda_agent.md` | Retain EDA orchestrator instructions; dedicated registration applies restricted shell workdir and typed `HandoffResult`. | `tests/unit/agent/test_prepare_agent.py`; `tests/unit/research/test_eda_todo.py`. |
| `agents/prompts/supervisor_agent.md` | Retain conversational/decision prompt; registered by `register_supervisor_agent`, paired with typed tool schemas. | `tests/unit/agent/test_supervisor_agent.py`. |
| `agents/prompts/validate_agent.md` | Retain runtime-only frozen-SOTA repair instructions; the VALIDATE loop independently preflights and reviews every proposed diff. | `tests/integration/research/test_validate_agent_contract.py`. |

## Scoped implementation changes and review notes

- Public meaningful API/validator documentation was added on changed boundaries
  in `agents/prompt_agent.py`; `research/clarification/llm_generator.py`;
  `research/experiment_documents/projector.py`; literature bench, paper
  markdown, paper scout and survey modules; `research/prepare/baseline_research.py`;
  `research/runtime/bootstrap.py`, `resume_contract.py`; and
  `research/supervisor/recovery.py`. Projection properties remain because they
  are used by concrete projector implementations and test support.
- Scoped Ruff autofixes/Black formatting reconciled imports, explicit
  concatenation and whitespace in the already-owned research/agent sources and
  dedicated research tests. No assertion was deleted to satisfy style. Existing
  `ValueError` contracts in `evaluation/evaluator.py` and `supervisor/state.py`
  were preserved with narrow Ruff rationale because tests and callers use them
  for invalid values.
- Other changed research files (`clarification/controller.py`, evaluation
  trust/spec, prepare/data/evaluator/source verification, supervisor plan and
  search paths, and their regression tests) contain the active data-contract,
  trust, and repair-loop repairs from the preceding task. This review retained
  their behavior and rechecked their callers rather than rewriting those
  policies during simplification.
- `_validate_frozen_evaluator` now requires the resolved `layout` tuple. The
  production freeze attempt resolves `_evaluator_layout(root)` once and shares
  the result with property checks, README rendering and descriptor creation;
  13 direct test calls now explicitly resolve the layout. This removes the only
  alternate parse path and avoids test-only private API compatibility.
- `turns/common.py` timeout cleanup logs the safe exception class if interrupt
  cleanup itself fails, while retaining the original timeout as the turn result.
  `BLE001` is locally justified because cleanup failures from injected Agent
  runtimes must not mask the primary timeout. The event callback binds its bus
  at creation rather than relying on a later loop variable.
- `script_runner.py` uses explicit `subprocess.run(check=False)` because `_run`
  owns conversion of nonzero exits to `ScriptCommandError` with bounded stderr.
- `agents/ideator/ideator.py` catches ordinary provider/validation exceptions
  after explicitly propagating cancellation; process-level `BaseException`s
  are no longer converted into another model turn.
- Supervisor exception boundaries remain scoped: malformed persisted JSON,
  external Agent/provider failures, optional corpus reads, reviewed-diff
  rejection, infrastructure failures and best-effort reaping have distinct
  policies. No lifecycle exceptions or trust checks were broadened for brevity.
- Kept `ProjectionContext` forwarding properties: real `DocumentProjector`
  implementations and test support consume them. A removal experiment caused
  eight concrete regressions; the properties were restored.
- Kept the persisted-state and evaluator invalid-value exceptions as
  `ValueError`: existing contract tests assert that type for malformed durable
  payloads. A Ruff suggestion to convert them to `TypeError` was rejected as a
  behavior change.
- No production research/agent module was deleted or merged. The active code
  already has distinct durable state, process-local resources, scheduling,
  trust settlement, validation recovery and Agent registration owners; no
  additional abstraction had multiple callers or a clearer policy boundary.

## Verification evidence

- Earlier research baseline: 1,514 passed before the optimization edits.
- A transient 1,504 passed / 10 failed run was fully explained by the two
  behavior-changing review experiments above (eight public projection-property
  failures and two exception-type contract failures); both changes were
  reverted. Their directly affected tests then passed: 17 passed.
- After making evaluator layout mandatory: `test_evaluator_plan_column.py`
  plus `test_prepare_plan.py`: 30 passed.
- Broader owned boundary selection across Agent registration/tools, Ideator,
  script runner, splitter, ranking, settlement, validation and Supervisor:
  255 passed. Timeout cleanup plus evaluator/Ideator focused rerun: 47 passed.
- Scoped Ruff passed for research/agents and dedicated research unit/integration
  tests; Black check left 250 files unchanged; strict source docstring/hard-rule
  checker passed for research/agents; scoped `git diff --check` passed.
- The repository-wide final suite is owned by the parent task and has not been
  claimed by this slice.


## Manual standards cleanup (2026-10-15)

- Reviewed the scoped `manual-norm-inventory.json` exception-comment candidates
  in `research/` and `agents/`. Replaced generic exception-name paraphrases with
  the concrete rejected input, unavailable resource, interrupted operation, or
  fallback condition at each handler. The nine files involved in the encoding
  incident were restored by the parent from the preserved copies before their
  comments were rewritten. No unrelated tree paths were edited by this slice.
- Flattened true nested branches while retaining the same fallback and ordering:
  `output_freshness.py` handles existing file/directory archives with a flat
  `if/elif`; `experiment_documents/store.py` keeps the schema migration guard
  separate from legacy metric coercion; `pdf_parser.py` uses early continuation
  for margin text and streams unique authors into the existing list;
  `tex_parser.py` preserves newlines while masking comments, selects the first
  author macro explicitly, and gathers eligible reference contexts in one
  comprehension; `search_loop.py` isolates hypothesis generation and preserves
  the prior generated-state result if the run stops; `settlement.py` filters
  absent artifact refs before attaching them; `paper_scout/selection.py` records
  covered facets without replacing their first selecting paper.
- Rechecked the remaining branch candidates. The candidate set has no remaining
  branch-only depth above three after excluding `elif` chains; the remaining
  PDF/TeX selection and parsing paths keep their current semantic tests and
  exception/resource boundaries.

Focused verification after the cleanup: 415 tests passed across experiment
archive storage, output freshness, PDF/TeX parsing, paper selection, scheduler
and supervisor execution, baseline research contracts, source verification, and
PREPARE orchestration. Black check passed for `src/athena/research` and
`src/athena/agents` (147 files unchanged); strict source/style checker and Ruff
passed for both directories; `git diff --check` passed. The repository-wide final
suite remains owned by the parent.

## Remaining current-inventory files (2026-10-15)

- `src/athena/research/literature/source_payload.py` — **Retain.** This keeps
  tar sniffing and the shared 256 MiB source bound in one small policy module;
  the TeX markdown loader and paper-source fetcher both consume it, preventing
  archive identification/size limits from drifting. Existing coverage is in
  `tests/unit/test_tex_source.py` (tar/zip/gzip and archive safety) and
  `tests/unit/test_paper_source.py` (format sniffing and fetched archive flow).
- `src/athena/research/prediction_contract.py` — **Retain.** It is the shared
  boundary for SEARCH and VALIDATE tabular prediction coverage: it honors the
  declared prediction file/format and ID columns, retains the legacy first-
  column rule, rejects duplicate/missing/extra identities, and skips
  non-tabular contracts. SEARCH maps invalid frozen feature identities to an
  infrastructure failure; malformed candidate output remains repairable, so
  this distinction must stay explicit. Direct and caller coverage lives in
  `tests/unit/research/test_prediction_contract.py`,
  `tests/unit/research/supervisor/test_validate_predicts_heldout.py`, and
  `tests/integration/research/test_data_contract_reaches_candidates.py`.
- `src/athena/research/prepare/authority_local.py` — **Retain.** The controller-
  owned local authority store is the concrete same-user persistence backend;
  it hashes project/session identity into opaque filenames, locks generation
  reads/writes, and atomically replaces the sealed record. Its caller wiring is
  in `src/gui_gateway/controller.py`; persistence, scope separation, conflicts,
  and malformed-record behavior are covered by
  `tests/unit/research/prepare/test_authority_local.py`, with gateway use
  covered by `tests/test_gui_gateway_main.py` and
  `tests/test_gui_gateway_e2e.py`.
- `src/athena/research/runtime/environment_repair.py` — **Retain.** This
  capability-scoped preflight intentionally allows repair only for transient
  authority timeouts/connections, uses at most two repair attempts, invokes at
  most one injected allowlisted action per repair Agent, and always rechecks
  authority. Invalid authority/configuration fails closed without repair, and
  cancellation remains propagated after cleanup. The startup caller is
  `src/athena/research/runtime/control.py`; attempt bounds, nonrepairable paths,
  action limits and cleanup are exercised by
  `tests/unit/research/test_environment_repair.py`.
