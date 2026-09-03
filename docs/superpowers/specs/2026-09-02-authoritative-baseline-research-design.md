# Authoritative Baseline Research Gate Design

Date: 2026-09-02
Status: implemented; superseded for cache/schema guarantees by the 2026-09-03 hardening design

Supersession notice: this document remains the historical design for source
qualification and the Git/OpenAlex evidence gate. The normative schema-v2,
exact-research/design-byte binding, external-authority cache, mirror, scoring-guard,
and completion-attestation guarantees are defined by
`2026-09-03-authoritative-baseline-hardening-design.md`. Version-one and
workspace-local cache language below records the original implementation only and must
not be used as the current security or restart contract.

## 1. Problem summary

Athena's PREPARE phase currently asks a baseline ideator to infer a model from the task
and EDA handoff. The ideator may mention prior knowledge, but it has no web-search
tools, no required source record, and no deterministic check that a cited repository is
downloadable or that a repository-free paper is sufficiently authoritative. If ideation
fails, PREPARE silently degrades to the raw task text and lets the prepare agent invent
the baseline.

This makes baseline selection hard to audit and can choose training from scratch when
the labeled dataset is too small. It also conflates three decisions that need separate
evidence: which prior method is credible, whether its implementation can be obtained,
and whether the local data supports training, transfer learning, or fine-tuning.

## 2. Product decision

Adopt **agent research plus a deterministic evidence gate**. The agent searches the
web and scholarly sources, assesses the data regime, and proposes a baseline. Platform
code then verifies the source independently before any baseline implementation begins.

The authoritative flow is:

```text
frozen evaluator + EDA handoff
    -> data-regime assessment
    -> web / paper search
    -> BASELINE_RESEARCH.json + BASELINE_DESIGN.md
    -> deterministic source validation
         |-> valid public Git repository
         |-> or high-citation paper exception
         `-> invalid: one agent repair turn, then PREPARE fails
    -> prepare agent implements the validated design
    -> trusted baseline evaluation
```

An unverified idea is research input, not an executable baseline. PREPARE must not
silently continue from task text alone after the research gate is introduced.

## 3. Goals

- Require an attributable baseline source before implementation.
- Prefer papers with public, cloneable Git repositories.
- Permit a repository-free paper only through a deterministic authority exception.
- Make dataset size and transfer-learning decisions explicit and reproducible.
- Give the research agent web access without giving downloaded third-party code
  execution authority.
- Preserve the existing frozen-evaluator and trusted-scoring boundary.
- Produce durable artifacts that explain accepted and rejected candidates.
- Fail with actionable evidence when no candidate qualifies.

## 4. Non-goals

- Automatically executing setup scripts, notebooks, or code from a cited repository.
- Vendoring an entire third-party repository into the research workspace.
- Replacing Athena's existing EDA, evaluator freezing, or trusted execution model.
- Building a universal model recommender from fixed sample-count thresholds.
- Guaranteeing that a cited method can be reproduced exactly on every dataset.
- Broadening general-agent web access outside baseline research.

## 5. Research artifacts

The baseline ideator writes two workspace-root files.

### 5.1 `BASELINE_RESEARCH.json`

This machine-readable file is the gate input. The following version-one contract is a
historical record of the original implementation. New verification, repair success,
cache reuse, and PREPARE entry require the version-two contract in the hardening
design.

```python
class DatasetAssessment(BaseModel):
    modality: Literal[
        "tabular", "image", "text", "audio", "video", "time_series", "multimodal", "other"
    ]
    task_type: str
    labeled_samples: int | None
    effective_training_units: int | None
    group_count: int | None
    class_count: int | None
    minority_class_samples: int | None
    input_scale: str
    regime: Literal["tiny", "small", "adequate", "unknown"]
    recommended_strategy: Literal[
        "classical",
        "frozen_pretrained",
        "partial_finetune",
        "full_finetune",
        "train_from_scratch",
    ]
    evidence: list[str]
    rationale: str

class BaselineSource(BaseModel):
    candidate_id: str
    title: str
    method: str
    source_url: HttpUrl
    source_kind: Literal["paper", "official_implementation", "technical_reference"]
    paper_locator: str | None
    repository_url: HttpUrl | None
    publication_year: int | None
    claimed_citation_count: int | None
    relevance: str

class CandidateDecision(BaseModel):
    candidate_id: str
    decision: Literal["selected", "rejected"]
    reason: str

class BaselineResearch(BaseModel):
    schema_version: Literal[1]
    dataset: DatasetAssessment
    candidates: list[BaselineSource]
    decisions: list[CandidateDecision]
    selected_candidate_id: str
    search_queries: list[str]
    limitations: list[str]
```

Contract rules:

- Candidate IDs are unique and exactly one decision is `selected`.
- `selected_candidate_id` names that selected candidate.
- At least two plausible candidates are recorded unless the report explains why only
  one could be found after multiple distinct queries.
- Every numeric dataset fact cites an EDA file, data-contract field, or an explicit
  calculation in `evidence`.
- `paper_locator` is a DOI or OpenAlex work ID when the paper path or authority
  exception may be needed. A title alone is not sufficient for citation verification.
- Agent-claimed citation counts are informational only and never satisfy the gate.
- URLs must not contain credentials.

After validation, Athena augments the parsed model in memory and writes an adjacent
`BASELINE_RESEARCH_VERIFICATION.json`. This platform-owned record contains the selected
candidate ID, verification route, normalized repository URL, resolved commit when
applicable, OpenAlex work ID and citation count when applicable, check timestamp, and
failure diagnostics from prior attempts. The agent cannot write or overwrite this
verification file.

### 5.2 `BASELINE_DESIGN.md`

This human-readable design must name the selected candidate ID and include:

- the source method and the local adaptation boundary;
- the dataset assessment and chosen training strategy;
- preprocessing, split, leakage, model, loss, metric, and resource decisions;
- which parts come from the source and which are Athena-specific adaptations;
- fallback behavior that remains within the same validated method family;
- risks, expected weaknesses, and the evidence that would trigger later changes.

The selected candidate and recommended strategy must agree across both artifacts.
Disagreement is a validation error, not an instruction for the prepare agent to choose.

## 6. Data-regime and training-strategy policy

Dataset sufficiency is modality-aware. Athena must not define one global threshold such
as “fewer than N rows means transfer learning.” The ideator uses EDA facts, effective
independent training units, label balance, input dimensionality, grouping, and source
expectations to classify the regime. The report must expose the reasoning so it can be
reviewed.

The following policy constrains the recommendation:

- For image, text, audio, video, and multimodal tasks, a tiny labeled regime with a
  relevant pretrained representation starts with frozen features or a linear head.
- A small labeled regime normally uses partial fine-tuning after the frozen baseline is
  viable. Full fine-tuning requires augmentation, regularization, and validation
  evidence appropriate to the modality.
- An adequate labeled regime may use full fine-tuning. Training from scratch still
  requires an explicit data-sufficiency argument and a source showing that the method is
  appropriate at comparable scale.
- Tabular tasks default to an authoritative classical or boosted-tree baseline.
  Transfer learning is not forced merely because the row count is small.
- Time-series and grouped data use the number of independent entities or windows after
  leakage-safe splitting, not raw row count alone.
- `unknown` is allowed only when EDA cannot establish a fact. It cannot be paired with
  `train_from_scratch`; the conservative strategy is required.

These rules are deterministic validation constraints around an evidence-based agent
assessment. They intentionally do not encode universal numeric cutoffs.

## 7. Source qualification

The selected source must pass one of two routes.

### 7.1 Preferred Git route

A source qualifies when it identifies a public HTTPS Git repository that Athena can
shallow-clone without credentials.

The verifier:

1. Accepts only `https://` repository URLs with no username, password, query, or
   fragment. Local paths, `file:`, SSH, and Git transport URLs are rejected.
2. Creates a dedicated temporary directory outside the research workspace.
3. Runs `git clone --depth 1 --filter=blob:none --no-checkout -- <url> <temp-target>`
   with a bounded timeout and capped diagnostic output.
4. Reads the cloned `HEAD` commit with Git and records it in the platform-owned
   verification artifact.
5. Removes the temporary directory in success and failure paths.

No file from the clone is imported, executed, installed, or copied into the workspace.
The check proves public availability and pins what was reachable at verification time;
it is not a security review of the repository.

### 7.2 High-citation paper exception

A paper without a cloneable repository qualifies only when OpenAlex independently
resolves its DOI or OpenAlex work ID and reports `cited_by_count >= 100` at verification
time. One hundred citations is the version-one authority threshold: high enough to make
the exception genuinely exceptional while remaining deterministic and testable.

The verifier records the normalized OpenAlex ID, publication year, citation count, and
timestamp. The returned title must normalize to a close match with the reported title;
a locator that resolves to an unrelated work is rejected. A title or citation number
supplied only by the agent never qualifies.

If OpenAlex is unavailable, a valid Git route may still pass. An unavailable or
under-threshold OpenAlex check never waives the repository requirement.

### 7.3 Candidate precedence

When similarly relevant candidates exist, selection order is:

1. relevant paper with a cloneable official repository;
2. relevant authoritative implementation with a cloneable repository;
3. repository-free paper satisfying the citation exception.

Citation count is not a relevance score. The report may reject a highly cited but
inapplicable method, and the validator checks qualification rather than overruling the
documented task-specific selection.

## 8. Agent and tool boundaries

The baseline ideator receives the existing workspace, shell, Kaggle/data, and corpus
tools plus `web_search` and `web_fetch`. Web tools are added only to the baseline
ideator's tool set in `research/runtime/bootstrap.py`; they do not become global
research-agent tools.

The prompt requires the agent to:

1. Read the task contract, data contract, and `EDA_HANDOFF.md`.
2. Inspect data only as needed to resolve missing scale or grouping facts.
3. Search with task-, modality-, metric-, and baseline-specific queries.
4. Prefer primary sources, project pages, paper records, and official repositories.
5. Record candidates and rejections in `BASELINE_RESEARCH.json`.
6. Choose the training strategy under Section 6.
7. Write the matching `BASELINE_DESIGN.md`.

The agent's shell tool is not used to prove Git qualification. Only the platform
verifier's result is authoritative. Prompt instructions also prohibit executing or
installing third-party repository content.

## 9. PREPARE orchestration

`prepare_baseline_design` becomes the owner of the research-and-verification loop:

```text
run baseline ideator without reaping its thread
    -> parse both artifacts
    -> validate cross-file contract and selected source
    -> independently verify Git and/or OpenAlex
    -> success: write verification artifact and reap agent
    -> failure: send structured diagnostics in one follow-up turn
         -> parse and verify replacement artifacts
         -> success: write verification artifact and reap agent
         -> failure: reap agent and raise BaselineResearchError
```

The follow-up request lists exact failed fields and checks. The agent may select a new
candidate or correct a bad locator, but it must rewrite the complete research and design
artifacts. There is exactly one repair turn so network or citation mistakes are
recoverable without creating an unbounded research loop.

Parsing failures, missing artifacts, cross-file disagreement, clone failure, OpenAlex
failure, and lack of a qualifying candidate are explicit PREPARE failures after the
repair turn. The current task-only fallback is removed for baseline-research failures.
Error messages include the attempted verification routes without exposing credentials
or unbounded subprocess output.

Only after verification succeeds does `run_baseline` register the prepare agent. Its
prompt requires it to read all three artifacts, implement the validated source-adapted
method, and record the selected candidate, pinned commit or OpenAlex evidence, and
training strategy in the baseline report and `RESEARCH_HANDOFF.md`.

Existing evaluator freezing, workspace policies, execution backend, time limits, and
trusted metric extraction remain authoritative. A validated source does not grant the
prepare agent permission to modify the evaluator or bypass execution controls.

## 10. Component changes

Implementation planning should assign the following responsibilities:

- `src/athena/research/prepare/baseline_research.py`: artifact schemas, parsing,
  cross-file validation, source-verification orchestration, and typed errors.
- `src/athena/research/prepare/baseline.py`: two-turn ideator lifecycle, verification
  artifact write, hard failure policy, and prepare-agent context.
- `src/athena/research/literature/paper_source/openalex.py`: expose OpenAlex work ID,
  title, publication year, and `cited_by_count` required by the authority check.
- `src/athena/research/runtime/bootstrap.py`: construct baseline-only web-search and
  fetch tools using the existing retrieval implementation.
- `src/athena/agents/prompts/baseline_ideator_agent.md`: research, artifact, source,
  and data-regime contract.
- `src/athena/agents/prompts/prepare_agent.md`: consume only a verified baseline design
  and carry evidence into its handoff.

Git verification uses a narrow subprocess adapter so tests can replace it without
network access. OpenAlex verification uses the existing client behind an injectable
interface. Configuration does not expose the citation threshold in version one; the
schema version and constant change together if policy changes later.

The original implementation required no persisted `ResearchState` schema change and
placed the three baseline files in the durable research workspace. Its workspace-local
verification reuse rule is superseded: local files are now read-only audit mirrors and
cannot authorize reuse. Resume authority comes only from the injected external
capability and binds exact research, design, and canonical verification bytes.

## 11. Failure and recovery behavior

- Web search or fetch failure: the ideator may use its remaining sources and repair
  turn; no empty-source design is accepted.
- Git timeout, authentication prompt, invalid URL, or clone failure: reject that route
  with a bounded diagnostic. The paper may still pass the independent citation route.
- OpenAlex timeout or unresolved locator: reject the exception route. A valid Git route
  remains sufficient.
- Candidate passes neither route: return a typed `BaselineResearchError` and stop
  PREPARE before baseline code is written.
- Workspace contains stale agent-written verification data: overwrite only with a
  platform-generated verification artifact after revalidation.
- Process interruption after research but before verification: resume from artifacts,
  verify them, and do not rerun the agent unless validation supplies repair feedback.
- Process interruption after verification: the original design reused matching local
  verification. The hardening design instead requires a matching external authority
  generation, exact local mirrors, and, for PREPARE completion, a matching trusted
  completion attestation.

## 12. Security and provenance

- Repository checks are non-interactive, HTTPS-only, timeout-bounded, and executed in a
  disposable directory.
- Verification never runs repository hooks, checkout content, package managers, or
  source code. `--no-checkout` and disabled terminal prompting are mandatory.
- URLs and subprocess errors are sanitized before persistence or agent feedback.
- The platform-owned artifact records when and how qualification occurred; it does not
  claim scientific correctness or repository safety.
- The prepare agent adapts or reimplements the method inside Athena's normal workspace;
  source licenses remain visible in the design and must be respected by later code use.
- Frozen evaluator files and trusted metric computation remain inaccessible to source
  research and unchanged by this feature.

## 13. Testing strategy

### Unit tests

- Accept and reject version-one research artifacts, duplicate IDs, missing selections,
  invalid URLs, unsupported strategy/regime combinations, and cross-file mismatches.
- Verify modality-aware policy constraints, including `unknown` plus scratch rejection
  and tabular behavior that does not force transfer learning.
- Exercise Git success, invalid scheme, clone error, timeout, commit resolution, output
  capping, non-interactive flags, and cleanup through a fake subprocess adapter.
- Exercise OpenAlex resolution, title mismatch, 99/100 citation boundary, unavailable
  service, and Git fallback with a fake client.
- Prove verification-file digest matching and stale-artifact revalidation.

### Prompt and wiring tests

- Assert that only the baseline ideator receives `web_search` and `web_fetch`.
- Assert that the ideator prompt requires both artifacts, multiple candidates, data
  assessment, source attribution, and transfer/fine-tuning reasoning.
- Assert that the prepare prompt requires the platform-owned verification artifact and
  carries its evidence into the final handoff.

### Orchestration tests

- A valid first response reaches prepare after one verification pass.
- An invalid first response receives one structured follow-up and a valid replacement
  proceeds.
- Two invalid responses reap the ideator and stop before prepare-agent registration.
- A cloneable repository passes without OpenAlex availability.
- A repository-free paper passes only at the citation threshold with matching identity.
- Missing research files never trigger the legacy task-only fallback.
- Resume reuses matching verification evidence and revalidates changed research.
- Existing frozen-evaluator and trusted-scoring integration tests continue to pass.

Network-facing behavior is covered with injected fakes in the default suite. A separate
opt-in smoke test may verify one known public Git repository and one OpenAlex work, but
normal CI must not depend on external network availability.

## 14. Acceptance criteria

- Every newly implemented baseline has `BASELINE_RESEARCH.json`,
  `BASELINE_RESEARCH_VERIFICATION.json`, and `BASELINE_DESIGN.md`.
- The selected source has either a platform-verified public Git commit or a matching
  OpenAlex work with at least 100 citations.
- No baseline code is written before source verification succeeds.
- The research artifact records a modality-aware data-regime assessment and one allowed
  training strategy with traceable evidence.
- Deep-learning-from-scratch recommendations are rejected unless data sufficiency and
  comparable-source evidence are explicit.
- Third-party repository content is never executed during qualification.
- One failed research response can be repaired; a second failure stops PREPARE with an
  actionable typed error.
- The prepare handoff records the validated source and training strategy.
- Existing evaluator-freezing, workspace safety, and trusted metric tests remain green.

## 15. External technical references

The implementation plan should use primary documentation for unstable external
interfaces:

- Git clone options and `--no-checkout`: https://git-scm.com/docs/git-clone.html
- Git remote-reference behavior: https://git-scm.com/docs/git-ls-remote.html
- OpenAlex filtering and `cited_by_count`: https://help.openalex.org/api/filtering/
- OpenAlex API recipes: https://help.openalex.org/how-to/api-recipes/
- PyTorch transfer-learning patterns for small datasets:
  https://docs.pytorch.org/tutorials/beginner/transfer_learning_tutorial.html
