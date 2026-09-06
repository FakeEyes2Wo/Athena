# Authoritative Baseline Hardening Completion Report

Date: 2026-09-03 (Asia/Shanghai)

Status: **COMPLETE**

This report supersedes
`codex_docs/2026-09-02-authoritative-baseline-research-completion-report.md`.
It records the final behavior and fresh verification evidence for the hardened
authoritative-baseline flow. The affected suite is green. The two wider suites are
accurately reported as non-green because they retain preflight failures and observed
Windows timing sensitivity; none of those failures traverses the changed hardening
path.

## Delivered behavior

### Evidence-led baseline policy

- `BaselineResearch` uses schema version 2 and separates dataset facts, pretrained
  availability, fine-tuning safeguards, scratch-scale comparisons, and search records
  into focused value objects.
- Every numeric dataset fact carries its own EDA evidence and exact value binding.
- The training strategy is selected by a deterministic policy matrix. There is no
  universal sample-count cutoff: modality, task, observed regime, pretrained support,
  and training safeguards determine whether transfer learning, partial/full
  fine-tuning, or training from scratch is admissible.
- A single-candidate search result needs an explicit, query-indexed exception. Scratch
  training needs selected-source scale evidence. Full fine-tuning needs its complete
  safeguard set.

### Exact provenance and external authority

- Research Markdown and design Markdown are read as raw bytes, decoded strictly as
  UTF-8, and bound independently by SHA-256.
- Verification schema version 2 also binds route identity, the selected candidate,
  canonical repository identity or OpenAlex identity, and canonical verification
  bytes.
- One shared matcher checks research bytes, design bytes, policy, route, source proof,
  and canonical serialization. No weaker workspace-only cache predicate remains.
- `BaselineAuthorityStore` is a workspace-bound capability with only three operations:
  `load`, `seal`, and `attest_prepare`. It exposes no endpoint, token, storage path,
  retry, serializer, or transport parameter to PREPARE.
- Local files are audit mirrors, never authority. Missing verification mirrors may be
  restored from a valid external record; mutations, conflicts, invalid generations,
  authority outages, and malformed responses fail closed.

### PREPARE and restart lifecycle

- Fresh verification rereads both Agent-authored files after network work, seals the
  exact bundle externally, and only then writes the canonical verification mirror.
- Authority/mirror guards run before PREPARE side effects, after every Agent turn,
  before trusted evaluation, after trusted scoring, and before completion attestation.
- Only generation 0 without an attestation and generation 1 with a digest-matching
  attestation are valid. Invalid lifecycle values are rejected before the verifier,
  Agent, mirror, evaluator, or authority mutation is invoked.
- Restart can skip PREPARE only when generation 1 attests the exact baseline commit,
  evaluator reference, evidence reference, both baseline digests, and trusted score.
- Authority adapter exceptions are translated to stable typed errors without chained
  secret-bearing details. Agent-readable Supervisor output does not publish authority
  tracebacks; cancellation keeps its native semantics.

### Git and OpenAlex boundaries

- Repository URLs are canonical public HTTPS URLs with no credentials, query,
  fragment, control characters, numeric-host ambiguity, or non-public literal.
- DNS verification inspects every A/AAAA result, rejects mixed or non-public answer
  sets (including multicast, site-local, mapped, and compatible forms), requires two
  identical pre-connect snapshots, and pins the accepted set with
  `http.curloptResolve`. Redirects are disabled.
- Git is captured only from fixed controller-owned installation roots: Windows
  Program Files Git `cmd`/`bin`, or POSIX `/usr/bin` and `/bin`. PATH and cwd are never
  searched, and a resolved candidate must remain under its trusted root. Missing or
  escaping candidates fail closed.
- Git receives an allowlisted child environment, list argv, `shell=False`, HTTPS-only
  protocol policy, disabled hooks/helpers/proxies/redirects/cookies/extra headers,
  explicit TLS verification, and no checkout. Ambient NTLM/GSS/SSPI authentication is
  disabled with `http.emptyAuth=false`, `http.proactiveAuth=none`, and
  `http.delegation=none`.
- OpenAlex uses its fixed API origin, typed identity checks, non-negative citation
  counts, title matching, and the inclusive versioned authority threshold. The
  approved high-citation paper exception does not require a repository; other paper
  candidates require a cloneable public Git proof.

## Key commits

- Design and activation: `e80f954`, `ff33ec9`, `2cf477d`
- Schema v2 and deterministic policy: `d3de90b` through `29f8e9f`
- Exact artifact/source binding: `ba9f25c` through `51d23d7`
- External authority capability: `eb937a6` through `05fc044`
- Authority-only PREPARE guards: `2600546`, `35d5b86`, `7fcdb48`
- Completion attestation and restart gate: `e7bd8e7`, `0689784`, `f71636a`
- End-to-end migration: `d4053d9`, `40bd8c7`
- Final whole-branch security fixes: `df95429`, `175aade`, `99e2804`
- Verified implementation HEAD before documentation closeout:
  `99e2804dd093101a6c0ef61d9050b6f523e3170f`
- Merge base with `main`: `d7a29284daa552fdf9d2f188d7c992f2163652fa`

The detailed TDD evidence for the first final-review fix wave is retained at
`.superpowers/sdd/2026-09-03-authoritative-baseline-hardening/final-fix-report.md`.

## Fresh final verification

All commands below ran sequentially against committed HEAD `99e2804`. Pytest cache was
disabled for the three main suites. Short, previously absent system-temp basetemps were
used for the two wider suites to reduce known Windows path-length timing noise.

### Static gates

1. Black check over `src/athena/research/prepare`, `runtime`, `supervisor`, the affected
   unit directories, and the three baseline integration files:
   exit `0`; `74 files would be left unchanged`.
2. `.venv\Scripts\python.exe -m compileall -q src/athena`:
   exit `0`.
3. Import smoke test for `BaselineAuthorityStore`, `SealedBaseline`,
   `BaselineResearch`, `BaselineVerification`, and `BaselineSourceVerifier`:
   exit `0`.
4. `git diff --check`:
   exit `0`.

### Affected suite

Command:

```powershell
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=.t7fa test/unit/research/prepare test/unit/research/test_prepare_modules.py test/unit/research/test_runtime_survey.py test/unit/research/test_breakpoint_resume.py test/unit/research/supervisor test/unit/retrieval/test_web_search.py test/unit/test_paper_source.py test/integration/research/test_authoritative_baseline_gate.py test/integration/research/test_prepare_agent_contract.py test/integration/research/test_autonomous_research.py
```

Result: exit `0`; **793 passed**, **3 subtests passed**, no failures; pytest
`1281.09s (0:21:21)`.

### Research-wide suite

Command:

```powershell
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=C:\Users\80163\AppData\Local\Temp\athena-t7r-20260903-1919 test/unit/research test/integration/research
```

Result: exit `1`; **1106 passed**, **11 failed**, **6 warnings**; pytest
`1592.39s (0:26:32)`. This suite is not green.

Six failures exactly match the preflight-known research failures:

- `test/unit/research/test_search_budget_override.py::test_an_explicit_budget_replaces_the_persisted_one`
- `test/integration/research/test_fork_runtime.py::test_both_arms_inherit_one_evaluator_and_start_at_search`
- `test/integration/research/test_human_plan_boundary.py::test_persistent_guidance_is_frozen_into_every_later_plan`
- `test/integration/research/test_human_plan_boundary.py::test_next_guidance_is_frozen_once`
- `test/integration/research/test_human_plan_boundary.py::test_unlimited_waiting_plan_keeps_execution_safety_limits`
- `test/integration/research/test_human_plan_boundary.py::test_budget_extension_resumes_waiting_plan_before_new_plan`

Five additional observations were fixed-five-second `_eventually` timeouts under the
wide run's sustained subprocess load:

- `test_rolling_search.py::test_first_completion_refills_without_batch_barrier`
- `test_rolling_search.py::test_attempts_count_created_plans_not_turns_or_scores`
- `test_rolling_search.py::test_supervisor_settlement_uses_the_scheduler_policy`
- `test_rolling_search.py::test_turn_is_persisted_before_dispatch`
- `test_search_recovery.py::test_abandon_without_best_marks_inconclusive`

### Repository-wide suite

Command:

```powershell
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=C:\Users\80163\AppData\Local\Temp\athena-t7p-20260903-1946
```

Result: exit `1`; **2560 passed**, **10 failed**, **4 warnings**, **53 subtests
passed**; pytest `696.02s (0:11:36)`. This suite is not green.

It reproduced the same six preflight research failures, the two preflight Windows
PowerShell failures, and two rolling-search timing observations:

- `test/unit/execution/test_encoding.py::test_windows_shell_detection_finds_pwsh_on_path`
- `test/unit/execution/test_encoding.py::test_chain_operator_works_under_powershell7`
- `test_rolling_search.py::test_first_completion_refills_without_batch_barrier`
- `test_rolling_search.py::test_four_running_plans_have_distinct_agent_workspace_and_log_ids`

The PowerShell failures are caused by this host selecting Windows PowerShell 5.1 when
PowerShell 7 (`pwsh`) is unavailable. The warnings are pre-existing asyncio Windows
subprocess-transport finalization warnings.

### Timing-failure isolation

The six unique timing nodes observed across both wide suites were rerun together after
the wide suites, with no concurrent pytest and a fresh short basetemp:

```powershell
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=C:\Users\80163\AppData\Local\Temp\athena-t7rca-20260903-2000 <six exact node ids>
```

Result: exit `0`; **6 passed in 20.69s**. The hardening changes do not modify
`SearchLoop`, the recovery scheduler, or those tests. The wide-run observations are
therefore classified as load-sensitive test timing, not a confirmed product
regression.

## Security and provenance audit

The final text audit searched the research and Agent trees for the removed local-cache
authority, authority environment paths, shell execution, checkout/install behavior,
design binding, attestation, and final Git authentication controls.

- No `load_cached_verified_baseline`, local authority store, authority environment
  variable, `shell=True`, or `pip install` path exists in the baseline flow.
- `copytree` hits are pre-existing generic script-runner/fork operations, not baseline
  authority storage.
- Required `design_sha256`, `attest_prepare`, `--no-checkout`, DNS pinning, and ambient
  authentication denial are present.
- Independent whole-branch review initially found the Git/DNS, exception-projection,
  lifecycle, and positional-compatibility issues fixed by `df95429`.
- Follow-up review found and verified the ambient-auth fix `175aade` and the
  fresh-import cwd/PATH provenance fix `99e2804`.
- Final independent verdict: **APPROVED**, with no Critical or Important finding.

## API and attribute budget

- New public runtime parameters across the hardening design: **1** — the already
  workspace-bound `baseline_authority` capability.
- New public or maintainer-configurable parameters in the final security wave: **0**.
- New class/instance attributes in the final security wave: **0**.
- Policy thresholds, Git restrictions, evidence version, and security behavior are
  constants or private implementation details, not runtime knobs.
- New schema objects remain focused: field counts are 2–6 for most values; only the
  aggregate `BaselineResearch` and explicit scratch comparison reach 8. The existing
  wide `DatasetAssessment` was not extended.
- Authority values have 3–5 fields. `PhaseActions` remains at 7 fields: its six
  historical positional slots are unchanged and the one internal resume callback is
  last. Its callback alias is private and unexported.

This keeps the maintenance surface intentionally narrow: the host supplies one opaque
capability, while policy and security choices cannot drift through per-run parameters.

## Residual risks and deployment invariant

- The concrete authority service, credential isolation, and Agent sandbox belong to
  the trusted host. Athena intentionally ships no same-process filesystem fallback;
  built-in composition with no authority fails closed.
- The trusted host must protect the configured Program Files, `/usr/bin`, or `/bin`
  Git installation and its helpers, and import/run the controller inside that trust
  boundary. The code rejects cwd/PATH lookup but does not cryptographically attest the
  host-installed binary.
- Two identical DNS snapshots are required. Legitimate rotating DNS can therefore
  fail closed, an accepted availability cost.
- Explicit authority/file byte ceilings and expanded async subprocess cancellation are
  deferred Low-severity defense-in-depth work. The current timeout, bounded diagnostic,
  disposable clone, and cleanup behavior remains in force.
- Cloneability proves retrievability at a recorded revision, not scientific validity,
  repository safety, or license compatibility. Trusted scoring and evidence review
  remain separate gates.

## Worktree hygiene

Only plan-owned source, tests, and documentation were staged. Pre-existing and
review/test-created untracked temporary directories were preserved and excluded from
all commits. The tracked worktree was clean at verified implementation HEAD.
