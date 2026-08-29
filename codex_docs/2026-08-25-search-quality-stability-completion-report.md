# Search Quality and Stability Completion Report (2026-08-25)

## Outcome

The Rubric V2/headless research path now has a bounded, auditable way to run
the next SUPPORT2 acceptance experiment with `search-limit=3`, three parallel
Ideators, up to five hypotheses per Ideator, and DeepSeek pro thinking on only
the reasoning-heavy boundaries.  Local implementation and acceptance are
complete.  No paid DeepSeek run was started as part of this verification.

The change deliberately does not redesign Gate, Selector, Scheduler,
`ResearchState`, GUI behavior, or the frozen Evaluation Policy contract.

## Delivered behavior

### Headless search controls and audit log

- `scripts/run_headless.py` accepts `--ideator-count`,
  `--hypotheses-per-ideator`, and `--pro-reasoning` in addition to the existing
  `--search-limit`.
- Existing defaults remain `search-limit=10`, `ideator-count=3`, and
  `hypotheses-per-ideator=2`; the larger SUPPORT2 run is explicitly requested
  rather than becoming a global default.
- Startup output records fresh/resume mode, default and reasoning model,
  thinking status, and requested/effective SEARCH settings.
- A resumed project whose persisted SEARCH settings differ from the requested
  settings now fails before doing work.  This prevents a resumed
  `search-limit=2` run from being reported as `search-limit=3`.

### Scoped model routing

- With `--pro-reasoning`, the three existing default SEARCH Ideator profiles
  and the two Rubric agents use `MODEL_PRO` with DeepSeek thinking enabled.
- Supervisor, Plan, Data, General, Validate, and format repair retain the
  original/default provider and thinking setting.
- Reasoning text is kept private.  When a reasoning model issues a tool call,
  its complete `reasoning_content` is retained internally and replayed on the
  next DeepSeek request as required for a valid continuation; it is not
  published as an agent text event.

### Candidate quality without workflow expansion

- The existing exploit, bold, and moonshot prompts ask for diverse,
  falsifiable, non-duplicate hypotheses, up to the configured per-Ideator
  maximum.  They do not pad a batch with weak duplicates.
- Ideation may inspect existing evidence and non-predictive descriptive
  diagnostics, but it must not fit models, run cross-validation, tune
  parameters, or calculate the primary benchmark.  SEARCH remains the owner
  of experiments.
- Every generated, Gate-approved hypothesis enters the existing graph.  The
  existing Layer 2 batch review and deterministic Selector decide execution
  order; no new queue, Gate, Selector, or scheduler was added.

### Bounded structured-output repair

- Validate, the two Rubrics, and default SEARCH Ideators can recover one
  unambiguous schema-valid JSON object wrapped in ordinary prose.
- If terminal output is still invalid, exactly one immediate format-only pass
  runs with an empty tool registry on the normal provider.  Completed tools or
  experiments are not run again.
- Multiple valid embedded objects are treated as ambiguous and fail closed.
  Invalid repair output also fails explicitly.
- Every explicitly emitted scalar must already be present in the original
  response.  The repair cannot invent scores, actions, evidence, or a success
  result.

### Windows and EDA regression fixes

- Headless output remains safe across native Windows PowerShell pipe encodings
  and uses a non-fatal representation for unsupported glyphs.
- EDA and handoff worker event forwarders are asynchronous and await the event
  bus, preventing dropped coroutine events and warning/error floods.
- Interrupt handling persists a STOPPED state before closing when possible.

## Frozen contracts preserved

Fresh Rubric/Supervisor regression coverage confirms that the following
contracts remain unchanged:

- one authoritative Evaluation Policy is frozen before evaluator creation;
- precedence remains Human > Official > Protocol > AI;
- unknown metrics remain fail-closed and never silently become Accuracy;
- Layer 2 remains one whole-batch LLM review;
- the four top-level weights remain 30/30/20/20;
- Selector consumes the stored score and makes no LLM call;
- Gate behavior and deterministic fallback ordering are unchanged.

## Full SUPPORT2 provenance proof

The new read-only audit verifies the official public archive, raw CSV, and
prepared task data before a paid run.  The real local audit passed with:

- official source:
  `https://hbiostat.org/data/repo/support2csv.zip`;
- archive SHA-256:
  `8ed43980742a18e1847a8dfc5530bc4b30564ad9e4ad1b1b50bbc5d29d8c86fe`;
- raw CSV SHA-256:
  `79621945edf2a5c8dc36359684ff356d3c6025e773ba4fefac26f865f7894c78`;
- prepared CSV SHA-256:
  `df8f44eaa79cad65a4cf562b08adf4609292dab4caacfcd672f8e4bf9cb4b563`;
- raw rows: 9,105;
- eligible/prepared rows: 9,105;
- excluded rows: 0;
- sampling: none;
- input features: 16;
- target: `mortality_180d`;
- target counts: 4,840 non-events and 4,265 events;
- every prepared row, feature, order, and derived target matches the
  deterministic full-cohort reconstruction.

Audit command:

```powershell
uv run python scripts/audit_support2_dataset.py `
  --archive ".\local_data\support2\support2csv.zip" `
  --raw ".\local_data\support2\extracted\support2.csv" `
  --prepared ".\local_data\support2\prepared\support2_180d.csv"
```

## Fresh verification

- Focused provider/repair/Windows/EDA/headless/audit suite: `72 passed`.
- Rubric, frozen-policy, Gate/Selector wiring, supervisor, and resume suite:
  `308 passed`.
- PREPARE, task-seeding, and Validate integration contracts: `33 passed`.
- Broad unit suite: `1703 passed, 2 skipped, 3 deselected, 50 subtests`.
  It used a synthetic non-secret `LLM_API_KEY` only to prevent the developer's
  local credential precedence from contaminating the settings unit test; no
  API request was made.
- Black: `34 files would be left unchanged`.
- Scoped repository style checker: exit code 0; 17 non-blocking R3 docstring
  notices remain.
- `compileall`: exit code 0.
- `git diff --check`: exit code 0.

## Unchanged baseline integration failures

Three integration assertions remain red, but line-level diff checks show that
their responsible code paths are present in `HEAD` and were not changed by
this task:

1. `test_all_phases_use_one_authority` observes both `ResearchRuntime` and
   `Supervisor` writing state; the direct `ResearchRuntime.start()` save is
   already in `HEAD`.
2. `test_provider_failure_at_turn_limit_waits_without_consuming_patience`
   times out because the SEARCH task intentionally remains parked in WAITING.
3. `test_turn_is_persisted_before_dispatch` observes `turns_used=2` rather
   than 1 in the existing rolling-search persistence path.

These failures belong to the explicitly excluded state-authority,
Scheduler/Supervisor-loop, and persistence architecture.  Changing them here
would expand the review surface and risk unrelated team behavior.  They are
reported rather than hidden.

## Remaining live acceptance

The next paid acceptance step is a **fresh** SUPPORT2 project.  Do not resume
the earlier `search-limit=2` directory; the runner will reject mismatched
persisted settings.

```powershell
cd "C:\Users\15055\Documents\GitHub\Athena-rubric-v2-refactor"

$project = Join-Path "$env:USERPROFILE\Documents\AthenaRuns" `
  ("support2-180d-search3-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Force $project | Out-Null

$task = Get-Content -Raw `
  ".\local_data\support2\prepared\SUPPORT2_180D_TASK.md"
$data = (Resolve-Path `
  ".\local_data\support2\prepared\support2_180d.csv").Path
$log = Join-Path $project "console.log"

uv run python scripts/run_headless.py `
  --project $project `
  --task $task `
  --data $data `
  --search-limit 3 `
  --ideator-count 3 `
  --hypotheses-per-ideator 5 `
  --pro-reasoning 2>&1 | Tee-Object -FilePath $log
```

The startup `[config]` line must show `mode=fresh`,
`requested_search=3`, `effective_search=3`,
`requested_ideators=3x5`, `effective_ideators=3x5`, the default model, the
pro reasoning model, and `reasoning_thinking=enabled` before the run is treated
as the requested acceptance experiment.
