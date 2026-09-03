# Authoritative Baseline Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make baseline verification safely reusable across process restarts through an external authority capability, bind the complete design, and enforce the approved data-policy contract deterministically.

**Architecture:** A controller injects one workspace-bound `BaselineAuthorityStore`; PREPARE never sees authority identity, credentials, transport, or storage parameters. Small version-two research value objects validate evidence and strategy policy, while a single exact-byte matcher and shared repository canonicalizer guard fresh verification, cache resume, PREPARE turns, trusted scoring, and completion resume.

**Tech Stack:** Python 3.12, asyncio, Pydantic v2, pytest, existing Athena runtime/agent/evaluator abstractions.

## Global Constraints

- Follow `docs/superpowers/specs/2026-09-03-authoritative-baseline-hardening-design.md`.
- The authority capability must be external to the agent filesystem/credential boundary; never create a local production fallback.
- Add only one public `ResearchRuntime` parameter, the already workspace-bound authority capability.
- Keep classes focused and attribute-light; do not append policy fields to the existing wide `DatasetAssessment`.
- Keep Git/OpenAlex thresholds and security policy versioned constants, not maintainer-tunable runtime parameters.
- Preserve the one-repair lifecycle, frozen evaluator, trusted scoring, and unrelated worktree changes.
- Use exact raw bytes for research/design digests and canonical verification serialization.
- Implement every behavior test-first and commit each task separately after its two-stage review passes.

---

### Task 1: Compact research schema v2 and deterministic policy

**Files:**
- Modify: `src/athena/research/prepare/baseline_research.py`
- Modify: `src/athena/agents/prompts/baseline_ideator_agent.md`
- Test: `test/unit/research/prepare/test_baseline_research_contract.py`
- Test: `test/unit/research/prepare/test_baseline_research_tools.py`

**Interfaces:**
- Produces: `EvidenceRef`, `DatasetFact`, `DatasetProfile`, `PretrainedAssessment`, `FineTuneSafeguards`, `ScratchScaleComparison`, `TrainingPolicy`, `OneCandidateException`, `SearchRecord`, and `BaselineResearch` with `schema_version: Literal[2]`.
- Produces: `BaselineResearch.training.strategy` as the sole downstream strategy accessor.
- Consumes: existing `BaselineSource`, `CandidateDecision`, `TrainingStrategy`, and design markers.

- [x] **Step 1: Replace permissive fixture coverage with behavior-first v2 tests**

Create a `valid_payload()` whose focused shape is:

```python
{
    "schema_version": 2,
    "dataset": {
        "modality": "image",
        "task_type": "classification",
        "input_scale": "224x224 RGB",
        "regime": "small",
        "facts": [
            {
                "field": "labeled_samples",
                "value": 480,
                "evidence": {
                    "kind": "eda",
                    "reference": "EDA_HANDOFF.md#dataset-size",
                    "claim": "480 labeled training images",
                },
            }
        ],
        "rationale": "Few labels relative to image dimensionality.",
    },
    "training": {
        "strategy": "partial_finetune",
        "pretrained": {
            "status": "available",
            "representation": "ImageNet encoder",
            "evidence": {
                "kind": "source",
                "reference": "https://example.org/paper",
                "claim": "The selected method provides pretrained weights.",
            },
        },
        "safeguards": None,
        "scratch_scale": None,
    },
    "search": {"queries": ["query one", "query two"], "one_candidate": None},
}
```

Add parameterized failures for every numeric fact name when its evidence/value is
missing, duplicated, blank, negative, or uses `kind="source"`. Add one-candidate
failures for blank scope/limitation, duplicate/out-of-range query indices, and a
redundant exception with two candidates. Add scratch selected-source/local-value
mismatch cases and policy-matrix cases for tiny/small/adequate/unknown regimes.

- [x] **Step 2: Run the focused contract tests and capture the expected red state**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare/test_baseline_research_contract.py test/unit/research/prepare/test_baseline_research_tools.py
```

Expected: failures show that schema version 2 and structured policy fields are not yet
implemented; no failure may come from test collection or an unrelated fixture error.

- [x] **Step 3: Implement small v2 value objects and validators**

Use focused models rather than extending `DatasetAssessment`. Normalize every string
through one private substantive-string validator. Make `DatasetFact.field` unique and
bind its evidence to the supplied value. Implement the policy with one pure function:

```python
def validate_training_policy(
    dataset: DatasetProfile,
    training: TrainingPolicy,
    selected: BaselineSource,
) -> None:
    """Raise ValueError when v2 evidence does not justify the strategy."""
```

Require pretrained evidence for relevant modalities, all three safeguards for full
fine-tuning, and selected-source scale proof for scratch. Reject unused safeguards or
scratch proof. Keep the no-global-cutoff rule explicit.

- [x] **Step 4: Update the ideator prompt to emit only the v2 contract**

Name every v2 object and state that each numeric fact carries its own evidence. Explain
the single-candidate and scratch structures, the full-finetune safeguards, the unknown
regime rules, and the absence of a universal sample threshold. Keep the prohibition on
writing the verification mirror.

- [x] **Step 5: Run Task 1 tests and formatting**

```powershell
.venv\Scripts\python.exe -m black --check src/athena/research/prepare/baseline_research.py test/unit/research/prepare/test_baseline_research_contract.py test/unit/research/prepare/test_baseline_research_tools.py
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare/test_baseline_research_contract.py test/unit/research/prepare/test_baseline_research_tools.py
```

Expected: Black exits 0 and all focused tests pass.

- [x] **Step 6: Commit Task 1**

```powershell
git add -- src/athena/research/prepare/baseline_research.py src/athena/agents/prompts/baseline_ideator_agent.md test/unit/research/prepare/test_baseline_research_contract.py test/unit/research/prepare/test_baseline_research_tools.py
git commit -m "feat: enforce baseline research schema v2"
```

---

### Task 2: Shared repository canonicalization and complete artifact binding

**Files:**
- Create: `src/athena/research/prepare/repository_url.py`
- Modify: `src/athena/research/prepare/baseline_research.py`
- Modify: `src/athena/research/prepare/source_verification.py`
- Test: `test/unit/research/prepare/test_baseline_research_contract.py`
- Test: `test/unit/research/prepare/test_baseline_source_verification.py`

**Interfaces:**
- Produces: `normalize_public_https_repository_url(value: str) -> str`.
- Produces: `design_sha256(raw: bytes) -> str`, `verification_bytes(verification) -> bytes`, and one `assert_verification_matches_artifacts(artifacts, verification) -> None` matcher.
- Changes: `BaselineArtifacts.raw_design: bytes` and `BaselineVerification.schema_version: Literal[2]` with required `design_sha256`.
- Consumes: Task 1's `BaselineResearch.training.strategy`.

- [ ] **Step 1: Write failing exact-binding and canonicalization tests**

Add tests proving that a same-marker/different-body design is rejected, CRLF and LF
produce different design digests, invalid UTF-8 is rejected after raw bytes are read,
a loopback/private repository proof is rejected, and a safe but non-selected repository
proof is rejected. Preserve every existing Git subprocess-hardening test.

- [ ] **Step 2: Run the focused tests and confirm behavior failures**

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare/test_baseline_research_contract.py test/unit/research/prepare/test_baseline_source_verification.py
```

Expected: new tests fail because design bytes/schema-v2 verification/shared matching do
not exist; legacy Git security tests remain green.

- [ ] **Step 3: Extract the dependency-leaf repository canonicalizer**

Move the existing URL/host/IP logic unchanged from `source_verification.py` into
`repository_url.py`. Import it from both source verification and artifact matching.
Do not leave a second weaker URL predicate in `baseline_research.py`.

- [ ] **Step 4: Bind raw research, raw design, route identity, and canonical bytes**

Read Markdown with `read_bytes()` then strict UTF-8 decode. Add the design digest to all
Git/OpenAlex success constructors. Canonicalize verification JSON exactly once:

```python
def verification_bytes(value: BaselineVerification) -> bytes:
    return (value.model_dump_json(indent=2) + "\n").encode("utf-8")
```

Use one matcher in all direct checks; remove duplicate digest/candidate checks.

- [ ] **Step 5: Run Task 2 verification**

```powershell
.venv\Scripts\python.exe -m black --check src/athena/research/prepare/repository_url.py src/athena/research/prepare/baseline_research.py src/athena/research/prepare/source_verification.py test/unit/research/prepare/test_baseline_research_contract.py test/unit/research/prepare/test_baseline_source_verification.py
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare/test_baseline_research_contract.py test/unit/research/prepare/test_baseline_source_verification.py
```

Expected: formatting and focused tests pass, including existing restricted Git cases.

- [ ] **Step 6: Commit Task 2**

```powershell
git add -- src/athena/research/prepare/repository_url.py src/athena/research/prepare/baseline_research.py src/athena/research/prepare/source_verification.py test/unit/research/prepare/test_baseline_research_contract.py test/unit/research/prepare/test_baseline_source_verification.py
git commit -m "feat: bind complete baseline artifacts"
```

---

### Task 3: Workspace-bound external authority capability

**Files:**
- Create: `src/athena/research/prepare/authority.py`
- Modify: `src/athena/research/runtime/services.py`
- Modify: `src/athena/research/runtime/bootstrap.py`
- Modify: `src/athena/research/runtime/facade.py`
- Test: `test/unit/research/prepare/test_baseline_authority.py`
- Test: `test/unit/research/test_runtime_survey.py`

**Interfaces:**
- Produces: frozen `VerifiedBaselineBundle`, `PrepareAttestation`, and `SealedBaseline` values.
- Produces: runtime-checkable `BaselineAuthorityStore` with only `load`, `seal`, and `attest_prepare`.
- Changes: `ResearchRuntime(..., baseline_authority: BaselineAuthorityStore | None = None)`; this is the only new public runtime parameter.
- Changes: `ResearchInfrastructure.baseline_authority` holds the injected capability without exposing transport details.

- [ ] **Step 1: Write authority contract and runtime-injection tests**

Implement a test-only `MemoryBaselineAuthorityStore` that preserves state across two
fresh runtime instances when the same fake external service object is injected. Test
generation 0 creation, compare-and-exchange conflict, immutable returned values,
attestation generation advance, and that `None` remains `None` without a local fallback.

- [ ] **Step 2: Run tests and confirm missing-interface failures**

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare/test_baseline_authority.py test/unit/research/test_runtime_survey.py
```

Expected: the new module/types/constructor parameter are missing; existing runtime
composition tests otherwise collect normally.

- [ ] **Step 3: Implement the minimal authority values and protocol**

Use frozen dataclasses with defensive byte copies and no transport fields:

```python
@runtime_checkable
class BaselineAuthorityStore(Protocol):
    async def load(self) -> SealedBaseline | None: ...
    async def seal(
        self, bundle: VerifiedBaselineBundle, *, expected_generation: int | None
    ) -> SealedBaseline: ...
    async def attest_prepare(
        self, evidence: PrepareAttestation, *, expected_generation: int
    ) -> SealedBaseline: ...
```

Add typed `BaselineAuthorityError` and `BaselineAuthorityConflict`. Do not add a local
filesystem implementation to production code.

- [ ] **Step 4: Thread one capability through the composition root**

Pass the constructor value directly to `build_services`; do not add it to persisted
`ResearchConfig`, settings RPCs, prompts, logs, or shell environments. Expose it only
through a read-only runtime property used by PREPARE and supervisor resume checks.

- [ ] **Step 5: Run Task 3 verification**

```powershell
.venv\Scripts\python.exe -m black --check src/athena/research/prepare/authority.py src/athena/research/runtime/services.py src/athena/research/runtime/bootstrap.py src/athena/research/runtime/facade.py test/unit/research/prepare/test_baseline_authority.py
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare/test_baseline_authority.py test/unit/research/test_runtime_survey.py
```

Expected: authority and composition tests pass.

- [ ] **Step 6: Commit Task 3**

```powershell
git add -- src/athena/research/prepare/authority.py src/athena/research/runtime/services.py src/athena/research/runtime/bootstrap.py src/athena/research/runtime/facade.py test/unit/research/prepare/test_baseline_authority.py test/unit/research/test_runtime_survey.py
git commit -m "feat: inject baseline authority capability"
```

---

### Task 4: Authority-only cache, mirror recovery, and PREPARE guards

**Files:**
- Modify: `src/athena/research/prepare/baseline_research.py`
- Modify: `src/athena/research/prepare/baseline.py`
- Modify: `src/athena/research/supervisor/prepare.py`
- Test: `test/unit/research/prepare/test_baseline_research_orchestration.py`
- Test: `test/unit/research/supervisor/test_prepare_plan.py`
- Test: `test/integration/research/test_authoritative_baseline_gate.py`

**Interfaces:**
- Produces: `load_authoritative_baseline(root, authority) -> VerifiedBaseline | None` and `seal_verified_baseline(authority, verified) -> VerifiedBaseline`.
- Changes: `VerifiedBaseline` carries canonical verification bytes and authority generation.
- Changes: `run_prepare_plan(..., assert_baseline: Callable[[], Awaitable[None]])` invokes the guard after every agent turn and before evaluator execution.
- Consumes: Tasks 2 and 3 exact matcher, canonical bytes, and authority capability.

- [ ] **Step 1: Replace forged-cache acceptance with failing security tests**

Add separate cases for: forged workspace trio with empty authority calls the live
verifier; exact authority restart skips network; research mutation, same-marker design
mutation, and verification-mirror mutation each stop PREPARE; missing verification
mirror is restored exactly; v1/workspace-only cache revalidates; authority outage does
not fall back; and an agent-turn mutation prevents evaluator execution.

- [ ] **Step 2: Run the new orchestration slice and capture red evidence**

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare/test_baseline_research_orchestration.py test/unit/research/supervisor/test_prepare_plan.py test/integration/research/test_authoritative_baseline_gate.py
```

Expected: the pre-entry forgery and same-marker rewrite are accepted by old code, and
post-turn guard tests fail because the callback is absent.

- [ ] **Step 3: Implement authority-only load and seal order**

Delete the workspace-authorizing `load_cached_verified_baseline`. Load authority first,
validate its exact bytes with the shared matcher, and compare all present mirrors.
Restore only a missing verification mirror. During fresh verification, re-read files,
seal externally, validate the returned generation/bundle, then atomically write the
canonical verification mirror. Never write a mirror if sealing fails.

- [ ] **Step 4: Add no-side-effect guards around PREPARE**

Require authority before baseline ideation starts. Check authority/mirrors before the
publish/register/tree snapshot sequence. Pass an async guard to `run_prepare_plan` and
invoke it immediately after each agent result is loaded and immediately before trusted
evaluation. Guard failures are terminal authority/evidence failures, not feedback that
lets the agent rewrite canonical inputs.

- [ ] **Step 5: Run Task 4 tests and the full prepare unit directory**

```powershell
.venv\Scripts\python.exe -m black --check src/athena/research/prepare/baseline_research.py src/athena/research/prepare/baseline.py src/athena/research/supervisor/prepare.py test/unit/research/prepare/test_baseline_research_orchestration.py test/unit/research/supervisor/test_prepare_plan.py test/integration/research/test_authoritative_baseline_gate.py
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare test/unit/research/supervisor/test_prepare_plan.py test/integration/research/test_authoritative_baseline_gate.py
```

Expected: all tests pass, including existing one-repair and cancellation/reap behavior.

- [ ] **Step 6: Commit Task 4**

```powershell
git add -- src/athena/research/prepare/baseline_research.py src/athena/research/prepare/baseline.py src/athena/research/supervisor/prepare.py test/unit/research/prepare/test_baseline_research_orchestration.py test/unit/research/supervisor/test_prepare_plan.py test/integration/research/test_authoritative_baseline_gate.py
git commit -m "fix: trust only external baseline authority"
```

---

### Task 5: Completion attestation and restart-to-SEARCH protection

**Files:**
- Modify: `src/athena/research/prepare/baseline.py`
- Modify: `src/athena/research/runtime/phase_runner.py`
- Modify: `src/athena/research/runtime/bootstrap.py`
- Modify: `src/athena/research/supervisor/deps.py`
- Modify: `src/athena/research/supervisor/phases.py`
- Test: `test/unit/research/prepare/test_baseline_research_orchestration.py`
- Test: `test/unit/research/supervisor/test_supervisor.py`
- Test: `test/unit/research/test_breakpoint_resume.py`

**Interfaces:**
- Produces: `PhaseRunner.baseline_resume_is_attested() -> Awaitable[bool]`.
- Changes: `PhaseActions` receives one internal `prepare_resume_is_attested` callback.
- Consumes: Task 3 `PrepareAttestation`; no new public runtime parameter is added.

- [ ] **Step 1: Write failing completion and forged-tree tests**

Test that successful trusted evaluation attests commit, evaluator ref, evidence ref,
and both baseline digests. Test that a fresh runtime skips PREPARE only when the local
baseline experiment matches that attestation. A forged/mutated tree, missing authority,
wrong generation, or authority outage must not transition to SEARCH.

- [ ] **Step 2: Run the focused restart tests and confirm red behavior**

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/supervisor/test_supervisor.py test/unit/research/test_breakpoint_resume.py test/unit/research/prepare/test_baseline_research_orchestration.py
```

Expected: old supervisor skips solely because `best_experiment_id()` is non-null.

- [ ] **Step 3: Attest only a fully scored baseline**

After `run_prepare_plan` returns a complete `PrepareResult`, call
`authority.attest_prepare` with the in-memory verified generation. Validate that the
service returns the same bundle plus the new attestation. If attestation fails, return
no resumably complete PREPARE result.

- [ ] **Step 4: Gate supervisor skip through a narrow callback**

When a local SOTA exists in PREPARE, load authority and compare its attestation against
the exact baseline experiment commit, run-config/evaluator ref, and evidence ref. Only
then publish the resume message and transition to SEARCH. A mismatch raises a typed
failure and leaves the phase in PREPARE; it must not silently delete or bless the local
tree.

- [ ] **Step 5: Run Task 5 and affected phase tests**

```powershell
.venv\Scripts\python.exe -m black --check src/athena/research/prepare/baseline.py src/athena/research/runtime/phase_runner.py src/athena/research/runtime/bootstrap.py src/athena/research/supervisor/deps.py src/athena/research/supervisor/phases.py test/unit/research/supervisor/test_supervisor.py test/unit/research/test_breakpoint_resume.py
.venv\Scripts\python.exe -m pytest -q test/unit/research/supervisor test/unit/research/test_breakpoint_resume.py test/unit/research/prepare/test_baseline_research_orchestration.py
```

Expected: all affected supervisor/restart tests pass.

- [ ] **Step 6: Commit Task 5**

```powershell
git add -- src/athena/research/prepare/baseline.py src/athena/research/runtime/phase_runner.py src/athena/research/runtime/bootstrap.py src/athena/research/supervisor/deps.py src/athena/research/supervisor/phases.py test/unit/research/prepare/test_baseline_research_orchestration.py test/unit/research/supervisor/test_supervisor.py test/unit/research/test_breakpoint_resume.py
git commit -m "fix: attest resumable baseline completion"
```

---

### Task 6: Fixture migration, prompt contract, and real-flow integration

**Files:**
- Modify: `src/athena/agents/prompts/prepare_agent.md`
- Modify: `test/integration/research/test_authoritative_baseline_gate.py`
- Modify: `test/integration/research/test_prepare_agent_contract.py`
- Modify: `test/integration/research/test_autonomous_research.py`
- Modify: `test/unit/research/test_breakpoint_resume.py`
- Modify: `test/unit/research/test_prepare_modules.py`
- Modify: `test/unit/research/supervisor/test_prepare_prompt_contract.py`
- Modify: `docs/superpowers/specs/2026-09-02-authoritative-baseline-research-design.md`
- Modify: `codex_docs/2026-09-02-authoritative-baseline-research-completion-report.md`

**Interfaces:**
- Consumes: Tasks 1-5 final schemas, authority fake, lifecycle callbacks, and prompt text.
- Produces: v2-only deterministic test fixtures and corrected user-facing status.

- [ ] **Step 1: Migrate every baseline fixture producer to v2 and injected authority**

Search with:

```powershell
rg -n 'schema_version.*1|BaselineVerification\(' test src/athena
```

Update only baseline-research fixtures. Each supplied numeric fact gets matching typed
evidence; each relevant modality gets pretrained evidence; all verification builders
include both exact digests; runtime fixtures inject a shared external-memory fake.

- [ ] **Step 2: Strengthen PREPARE prompt contract tests**

Require the prompt/task to call the three local files read-only mirrors, name the
external authority generation without exposing credentials, and prohibit rewriting
research/design/verification. Assert that v1 field names are not presented as the
current output contract.

- [ ] **Step 3: Exercise real Git and OpenAlex flows through trusted scoring**

Keep external Git/OpenAlex/authority boundaries fake, but use the real phase runner,
PREPARE agent contract, `TrustedEvaluator`, and `DataScriptRunner`. Prove both routes
seal exact bytes, survive a fresh runtime, reject a forged local trio, and write a
matching completion attestation after scoring.

- [ ] **Step 4: Correct superseded documentation claims**

Mark the 2026-09-02 design as implemented but superseded for cache/schema guarantees by
the hardening design. Mark the old completion report as reopened by final branch review
until Task 7 evidence exists. Do not delete historical commands or known-failure data.

- [ ] **Step 5: Run the complete affected slice**

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare test/unit/research/test_prepare_modules.py test/unit/research/test_runtime_survey.py test/unit/research/test_breakpoint_resume.py test/unit/research/supervisor/test_prepare_plan.py test/unit/research/supervisor/test_prepare_prompt_contract.py test/unit/research/supervisor/test_supervisor.py test/unit/retrieval/test_web_search.py test/unit/test_paper_source.py test/integration/research/test_authoritative_baseline_gate.py test/integration/research/test_prepare_agent_contract.py test/integration/research/test_autonomous_research.py
```

Expected: complete affected slice passes with no new warning.

- [ ] **Step 6: Commit Task 6**

```powershell
git add -- src/athena/agents/prompts/prepare_agent.md test/integration/research/test_authoritative_baseline_gate.py test/integration/research/test_prepare_agent_contract.py test/integration/research/test_autonomous_research.py test/unit/research/test_breakpoint_resume.py test/unit/research/test_prepare_modules.py test/unit/research/supervisor/test_prepare_prompt_contract.py docs/superpowers/specs/2026-09-02-authoritative-baseline-research-design.md codex_docs/2026-09-02-authoritative-baseline-research-completion-report.md
git commit -m "test: cover trusted baseline restart boundary"
```

---

### Task 7: Fresh verification, independent review, and closeout

**Files:**
- Create: `codex_docs/2026-09-03-authoritative-baseline-hardening-completion-report.md`
- Modify: `codex_docs/CURRENT.md`
- Modify: `codex_docs/2026-09-02-authoritative-baseline-research-completion-report.md`
- Delete after all evidence passes: `docs/superpowers/plans/2026-09-03-authoritative-baseline-hardening.md`

**Interfaces:**
- Consumes: all acceptance criteria and the preflight-known failure list from the prior completion report.
- Produces: fresh command evidence, security/threat-model audit, final independent review, and accurate current-work pointers.

- [ ] **Step 1: Run formatting, compile, import, and diff checks**

```powershell
.venv\Scripts\python.exe -m black --check src/athena/research/prepare src/athena/research/runtime src/athena/research/supervisor test/unit/research/prepare test/unit/research/supervisor test/integration/research/test_authoritative_baseline_gate.py test/integration/research/test_prepare_agent_contract.py test/integration/research/test_autonomous_research.py
.venv\Scripts\python.exe -m compileall -q src/athena
.venv\Scripts\python.exe -c "from athena.research.prepare.authority import BaselineAuthorityStore, SealedBaseline; from athena.research.prepare.baseline_research import BaselineResearch, BaselineVerification; from athena.research.prepare.source_verification import BaselineSourceVerifier"
git diff --check
```

Expected: every command exits 0.

- [ ] **Step 2: Run affected, research-wide, and repository-wide suites sequentially**

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare test/unit/research/test_prepare_modules.py test/unit/research/test_runtime_survey.py test/unit/research/test_breakpoint_resume.py test/unit/research/supervisor test/unit/retrieval/test_web_search.py test/unit/test_paper_source.py test/integration/research/test_authoritative_baseline_gate.py test/integration/research/test_prepare_agent_contract.py test/integration/research/test_autonomous_research.py
.venv\Scripts\python.exe -m pytest -q test/unit/research test/integration/research
.venv\Scripts\python.exe -m pytest -q
```

Expected: affected slice is green. Record exact outcomes for the wider suites; compare
every failure with the prior six research and two PowerShell preflight failures rather
than describing a nonzero suite as passing.

- [ ] **Step 3: Run the security/provenance audit**

```powershell
rg -n "load_cached_verified_baseline|LocalArtifactStore.*authority|BASELINE_AUTHORITY|shell=True|checkout|pip install|copytree|--no-checkout|design_sha256|attest_prepare" src/athena/research src/athena/agents
```

Expected: no workspace/local-store cache authority or leaked authority credential path;
required design digest, attestation, protocol denial, and no-checkout evidence exists.

- [ ] **Step 4: Obtain independent specification and code-quality reviews**

Review the full remediation range against the hardening design. A reviewer must inspect
cross-task authority flow, arbitrary-shell threat assumptions, v2 migration, exact
design binding, policy matrix, restart skip, API/attribute count, and the fresh test
evidence. Resolve every Critical or Important finding before closeout.

- [ ] **Step 5: Write the completion report and close the active plan**

Record commit hashes, exact commands, exit codes, counts, known failures, deployment
invariant, and any residual risk. Update the 2026-09-02 report to link the superseding
hardening report. Only after all acceptance criteria have fresh evidence, delete this
plan and update `codex_docs/CURRENT.md` to show no active plan and link the new report.

- [ ] **Step 6: Commit closeout documentation**

```powershell
git add -- codex_docs/CURRENT.md codex_docs/2026-09-02-authoritative-baseline-research-completion-report.md codex_docs/2026-09-03-authoritative-baseline-hardening-completion-report.md docs/superpowers/plans/2026-09-03-authoritative-baseline-hardening.md
git commit -m "docs: complete authoritative baseline hardening"
```
