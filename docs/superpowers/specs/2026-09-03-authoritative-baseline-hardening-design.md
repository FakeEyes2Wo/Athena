# Authoritative Baseline Hardening Design

Date: 2026-09-03
Status: approved for implementation
Amends: `2026-09-02-authoritative-baseline-research-design.md`

## 1. Why the completed gate needs hardening

The first implementation verifies Git or OpenAlex correctly, but its restart cache is
stored beside agent-authored files. PREPARE agents have a host shell and can therefore
rewrite the research, design, and verification files together before a restart. The
current cache then validates a self-consistent forgery without calling the independent
source verifier.

Three related contracts are also weaker than the approved design:

- verification hashes the research JSON but not the complete Markdown design;
- one generic evidence string can authorize every numeric dataset fact;
- modality-aware strategy constraints and single-candidate exceptions are not fully
  represented as deterministic data.

This design fixes those boundaries without weakening Git/OpenAlex verification or
adding general-purpose configuration knobs.

## 2. Decisions and non-goals

Athena will retain safe cache reuse across process restarts. The cache authority will
move to a controller-owned external service whose credentials and storage are not
available to agent tools. Local files, `.athena`, the local artifact store, local Git
refs, and `ResearchState` remain recovery data, not security authorities.

This change does not build a general secret manager, distributed database, agent
sandbox, or universal evidence graph. It does not introduce sample-count thresholds.
The external authority service is supplied by the host through one narrow capability;
Athena will not expose its endpoint or credentials to PREPARE code.

## 3. Maintainability constraints

- Add one public capability interface, `BaselineAuthorityStore`, rather than passing
  URLs, tokens, retry counts, serializers, or storage paths through PREPARE.
- Keep the interface task-specific: load a sealed baseline, atomically seal a newly
  verified baseline, and attest PREPARE completion. Do not expose generic blob CRUD.
- Prefer frozen dataclasses and small Pydantic value objects with one responsibility.
  Split dataset facts, training policy, search evidence, and authority records instead
  of adding more attributes to the existing `DatasetAssessment` class.
- Keep policy constants in code and version them with the artifact schema. Maintainers
  must not tune security thresholds through runtime parameters.
- Centralize exact-byte hashing, canonical serialization, repository URL
  canonicalization, and artifact-to-verification matching in one implementation each.
- A production runtime receives at most one new constructor argument: the authority
  capability. Tests use an in-memory implementation of the same interface.

## 4. Trust boundary and deployment invariant

The controller owns `BaselineAuthorityStore`. The concrete production implementation
must execute outside the agent worker's filesystem and credential boundary. Authority
credentials must not be placed in the agent environment, workspace, prompt, tool
registry, command arguments, logs, or local artifact store.

The application must fail closed when no production authority capability is supplied.
It must never silently construct a local fallback. An explicit in-memory authority is
allowed only in tests and non-resumable development harnesses that opt into it.

This is a deployment invariant, not something a digest can simulate. If the controller
and arbitrary agent shell share an OS identity that can inspect controller memory, the
deployment does not satisfy this design even when all Python tests pass.

## 5. Compact authority model

The interface has three operations:

```python
class BaselineAuthorityStore(Protocol):
    async def load(self) -> SealedBaseline | None: ...
    async def seal(
        self,
        bundle: VerifiedBaselineBundle,
        *,
        expected_generation: int | None,
    ) -> SealedBaseline: ...
    async def attest_prepare(
        self,
        evidence: PrepareAttestation,
        *,
        expected_generation: int,
    ) -> SealedBaseline: ...
```

The injected capability is already bound by the controller to one stable workspace
identity. PREPARE never receives or constructs an authority key. This removes a public
parameter and prevents task text or an agent-controlled path from becoming an identity.
`VerifiedBaselineBundle` contains the exact bytes of the research JSON, design
Markdown, canonical verification JSON, and their parsed verification.
`SealedBaseline` adds a monotonic generation and optional matching
`PrepareAttestation`. `PrepareAttestation` binds the verified research/design digests
to the accepted baseline commit and trusted evaluation reference.

The external service is responsible for immutable payload retention and atomic
compare-and-exchange. Athena treats generation conflicts, corrupt responses, missing
payloads, and service outages as typed failures. The core interface deliberately says
nothing about HTTP, databases, tokens, retries, or object-store references.

## 6. Exact-byte artifact binding

`BaselineArtifacts` retains both `raw_research` and `raw_design`. Markdown is decoded
as strict UTF-8 only after its raw bytes are retained. Verification schema version 2
requires lowercase SHA-256 digests for both byte sequences.

One matcher enforces all of the following wherever authority is consumed:

- both exact-byte digests match;
- the selected candidate and strategy markers match the research contract;
- a Git proof uses the selected candidate's canonical repository URL;
- an OpenAlex proof names the selected candidate's resolved paper identity;
- the verification contains a successful attempt for its selected route.

The strict public HTTPS repository canonicalizer moves into a dependency-leaf module
and is shared by live Git verification and cached-proof validation. Loopback, private,
link-local, multicast, credential-bearing, non-HTTPS, query-bearing, and fragment URLs
therefore receive identical treatment on both paths.

The workspace `BASELINE_RESEARCH_VERIFICATION.json` is a byte-identical audit mirror of
the externally stored canonical verification bytes. Parsing a local mirror can never
authorize a cache hit.

## 7. Restart and mutation flow

### Fresh verification

1. Load and validate research/design version 2 artifacts.
2. Run the existing restricted Git/OpenAlex verifier.
3. Build verification version 2 and re-read both workspace artifacts.
4. Reject any change that occurred during verification.
5. Atomically seal all canonical bytes in the authority service.
6. Write the exact canonical verification bytes as the workspace mirror.
7. Carry the sealed generation in memory into PREPARE.

Authority is written before the mirror. A crash before sealing has no cache; a crash
after sealing can reconstruct a missing mirror from authority.

### Restart

1. Load only from the controller-bound authority capability.
2. Validate the sealed record, both hashes, route proof, and generation.
3. If the workspace verification mirror is missing, reconstruct it from the canonical
   bytes. If any present research, design, or verification file differs, emit a
   tamper/corruption diagnostic and fail before PREPARE.
4. When every byte matches, reuse the sealed record without Git/OpenAlex traffic.

Version-one records and a matching workspace-only trio are cache misses. They undergo
one live verification and become version two only after the authority service accepts
the new sealed bundle. No local data is promoted to trusted status by migration.

### PREPARE and scoring guards

Authority and mirror equality are checked immediately before the first PREPARE side
effect, after every PREPARE-agent turn, and before trusted scoring. A modified baseline
artifact prevents plan execution and scoring even when its two Markdown markers remain
unchanged.

After a baseline commit receives trusted evaluation, Athena writes a
`PrepareAttestation` to the same authority generation. On process restart, a local
research tree or SOTA entry may skip PREPARE only when this external attestation matches
the local baseline commit and evaluation reference. Forged local tree state alone is
never sufficient.

## 8. Research schema version 2

Version 2 replaces the wide dataset object with four focused records:

```python
class EvidenceRef(BaseModel):
    kind: Literal["eda", "data_contract", "calculation", "source"]
    reference: str
    claim: str

class DatasetFact(BaseModel):
    field: Literal[
        "labeled_samples", "effective_training_units", "group_count",
        "class_count", "minority_class_samples",
    ]
    value: int
    evidence: EvidenceRef

class DatasetProfile(BaseModel):
    modality: Modality
    task_type: str
    input_scale: str
    regime: Literal["tiny", "small", "adequate", "unknown"]
    facts: list[DatasetFact]
    rationale: str

class TrainingPolicy(BaseModel):
    strategy: TrainingStrategy
    pretrained: PretrainedAssessment | None
    safeguards: FineTuneSafeguards | None
    scratch_scale: ScratchScaleComparison | None
```

`BaselineResearchV2` contains `dataset`, `training`, candidates, decisions, a focused
`SearchRecord`, and qualitative limitations. Each value object rejects blank strings
and unknown fields. `DatasetFact` binds every supplied number to its own traceable
EDA/data-contract/calculation record; duplicate fact names are rejected. A calculation
claim includes its expression in `reference` or `claim` rather than adding another
public field.

`SearchRecord` contains normalized-distinct queries and an optional structured
single-candidate exception. The exception is required exactly when only one candidate
exists and identifies at least two query indices, the searched scope, and a nonblank
limitation. It does not claim the schema can prove what a search engine returned.

`ScratchScaleComparison` identifies the selected candidate, one of its canonical
source locators, the local fact and value, the source value and unit, the comparison
relationship, and rationale. It is required only for scratch training and is forbidden
otherwise.

Version 1 may remain parseable for diagnostics, but it is never eligible for new
verification, cache reuse, repair success, or PREPARE entry.

## 9. Deterministic training-policy matrix

For image, text, audio, video, and multimodal data:

- pretrained availability must always be assessed with traceable evidence;
- `tiny` plus an available relevant representation requires `frozen_pretrained`;
- `small` permits frozen or partial fine-tuning; full fine-tuning is permitted only
  with explicit augmentation, regularization, and validation evidence;
- frozen or partial fine-tuning requires an available representation;
- every full fine-tune requires the three safeguards and an available representation;
- `unknown` permits `classical`, or frozen pretrained when availability is confirmed;
  scratch, partial, and full fine-tuning are rejected;
- `adequate` permits transfer strategies subject to their evidence requirements, and
  permits scratch only with a selected-source scale comparison.

Tabular data defaults to classical methods but does not prohibit a sourced alternative.
Time-series/grouped data uses an evidenced `effective_training_units` or `group_count`
fact when it asserts those quantities. Scratch always requires `adequate`, regardless
of modality. No global numeric cutoff is introduced.

Unused policy evidence is rejected: safeguards exist only for full fine-tuning, and a
scratch comparison exists only for scratch. This prevents stale evidence from making a
later strategy change appear justified.

## 10. Failure semantics

- Authority unavailable/corrupt/conflicted: fail closed with a typed authority error;
  do not consult local cache as a fallback.
- No authority record: perform live verification of valid v2 artifacts.
- Present mirror or artifact differs from authority: fail before agent registration or
  scoring and report the mismatched filename without leaking service credentials.
- Missing verification mirror with valid authority: restore it atomically.
- Missing research or design file: fail; do not silently reconstruct agent-authored
  inputs because their presence is part of the audit contract.
- Live verification failure: preserve the existing single repair turn, then terminate.
- Authority seal failure after live verification: do not start PREPARE and do not leave
  a workspace mirror that could be mistaken for authoritative.
- PREPARE completion attestation failure: retain the trusted evaluation result locally
  for diagnosis, but do not mark the phase resumably complete.

## 11. Test strategy

Unit tests cover one negative case per numeric fact, blank/duplicate evidence,
single-candidate query bindings, each policy-matrix boundary, and selected-source
scratch comparisons. Shared repository canonicalization receives the existing Git
security suite plus cached-proof loopback and selected-repository mismatch cases.

Authority contract tests use a small in-memory store to prove compare-and-exchange,
generation conflicts, canonical byte preservation, and completion attestation. They do
not describe that store as production-safe.

Orchestration tests prove:

- an all-three-file pre-entry forgery cannot bypass live verification;
- a fresh runtime safely reuses an external sealed record without network calls;
- same-marker design rewrites and each other mirror mutation stop PREPARE;
- a missing verification mirror is reconstructed byte-for-byte;
- version-one and workspace-only caches reverify once;
- authority outage never falls back locally;
- an interrupted PREPARE rewrite is caught after the agent turn and before scoring;
- a forged local tree/SOTA checkpoint cannot skip PREPARE without matching external
  completion attestation.

The authoritative integration harness continues through the real phase runner,
PREPARE agent boundary, evaluator, and script runner for both Git and OpenAlex routes.
Default tests fake only external Git/OpenAlex/authority network boundaries.

## 12. Acceptance criteria

- Restart cache hits originate only from an agent-inaccessible authority capability.
- Exact research and design bytes and canonical verification bytes are bound to one
  monotonic authority generation.
- Workspace-only, version-one, malformed, stale, or forged records never authorize
  PREPARE or trusted scoring.
- Every supplied numeric dataset fact has field-specific traceable evidence.
- One-candidate research and scratch training require their structured exceptions.
- The modality/regime/strategy matrix is enforced without a universal numeric cutoff.
- PREPARE completion can be resumed only with a matching external attestation.
- The implementation adds one narrow authority interface and no local security
  fallback; configuration details remain outside the research workflow.
- Existing restricted Git/OpenAlex behavior, one-repair lifecycle, evaluator freezing,
  and trusted metric extraction remain intact.
