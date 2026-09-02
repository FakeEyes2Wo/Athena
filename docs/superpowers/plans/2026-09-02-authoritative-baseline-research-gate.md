# Authoritative Baseline Research Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Require a web- or paper-researched, independently verified source and an explicit data-regime/training-strategy assessment before Athena implements a PREPARE baseline.

**Architecture:** The baseline ideator writes a typed research artifact and a matching Markdown design. Deterministic platform code validates the artifacts, proves either a safe shallow Git clone or an OpenAlex citation exception, persists a digest-bound verification artifact, and only then registers the prepare agent; one failed agent response receives one repair turn. Baseline-only web tools and injectable Git/OpenAlex adapters keep research useful, tests offline, and third-party code outside Athena's execution boundary.

**Tech Stack:** Python 3.11+, Pydantic v2, asyncio, subprocess/Git, OpenAlex REST metadata, Athena `ToolRegistry`, pytest/pytest-asyncio.

## Global Constraints

- Read `codex_docs/CURRENT.md` and this plan before every implementation task; update each task checkbox as it is completed.
- Preserve every unrelated staged and unstaged worktree change. Stage and commit only the paths listed in the current task.
- The selected source must pass a public HTTPS Git clone or a matching OpenAlex work with `cited_by_count >= 100`.
- Git verification accepts no credentials, local path, query, fragment, SSH, `git:`, `file:`, or plain HTTP URL.
- Git verification uses `--depth 1 --filter=blob:none --no-checkout`, disables terminal prompts and non-HTTPS protocols, caps diagnostics, and removes its temporary directory.
- Verification must never checkout, import, install, copy, or execute third-party repository content.
- `BASELINE_RESEARCH_VERIFICATION.json` is written only after platform verification and is reusable only while its research digest and selected candidate still match.
- Dataset sufficiency remains modality-aware. There is no global numeric small-data cutoff.
- `unknown` data regime may not select `train_from_scratch`; scratch training requires an `adequate` regime plus explicit EDA/calculation and comparable-source evidence.
- Ordinary SEARCH ideators and General Agents must not gain new tools as a side effect of baseline web research.
- Normal tests use injected fakes and make no external network calls.
- Missing, invalid, or twice-rejected baseline evidence stops PREPARE before the prepare agent is registered; task-only fallback is forbidden.
- Existing evaluator freezing, directory split isolation, workspace policy, trusted scoring, and metric extraction remain unchanged.

## File map

- Create `src/athena/research/prepare/baseline_research.py`: versioned artifact models, Markdown marker parsing, policy validation, digests, and cache I/O.
- Create `src/athena/research/prepare/source_verification.py`: restricted Git runner, OpenAlex authority route, title matching, diagnostics, and verifier composition.
- Modify `src/athena/research/literature/paper_source/openalex.py`: expose non-negative `cited_by_count` in `OpenAlexWork`.
- Modify `src/athena/retrieval/web_search.py`: expose one reusable factory for a shared-session web tool pair.
- Modify `src/athena/research/turns/general.py`: consume the shared web-tool factory without changing General Agent behavior.
- Modify `src/athena/research/runtime/bootstrap.py`: add a lazy baseline-only tool provider.
- Modify `src/athena/research/runtime/facade.py`: expose `baseline_ideator_tools()` separately from ordinary `ideator_tools()`.
- Modify `src/athena/agents/ideator_agent.py`: document the two baseline ideator artifacts.
- Modify `src/athena/agents/prompts/baseline_ideator_agent.md`: require search, candidates, data assessment, exact Markdown markers, and both artifacts.
- Modify `src/athena/agents/prompts/prepare_agent.md`: require the platform verification artifact and provenance in reports/handoff.
- Modify `src/athena/research/prepare/baseline.py`: run/reuse/repair/verify research and gate prepare-agent registration.
- Modify `src/athena/research/prepare/orchestrator.py`: carry `VerifiedBaseline` from research into baseline execution.
- Create focused unit and integration tests under `test/unit/research/prepare/` and `test/integration/research/`.
- Modify `docs/eda_workflow.md` and `docs/research_core_mechanisms_ch.md`: document the implemented PREPARE evidence gate.

## Execution preflight

- [x] Record `git status --short` and `git diff --cached --name-status` before editing. Do not unstage, overwrite, or commit pre-existing changes.
- [x] Run `.venv\Scripts\python.exe -m pytest -q test/unit/research/test_prepare_modules.py test/unit/research/test_runtime_survey.py test/unit/test_paper_source.py test/unit/retrieval/test_web_search.py test/integration/research/test_prepare_agent_contract.py` and record the exact baseline pass/fail counts in the plan notes.
- [x] If execution is isolated, use the `using-git-worktrees` skill and ensure the worktree contains the currently staged research-layout moves before changing code; do not start from a revision that still uses the retired flat module paths.

Preflight notes (2026-09-02):

- The isolated worktree and index were clean before feature edits.
- The focused PREPARE/source/web slice passed: 96 passed in 39.29s.
- The research suite baseline was 735 passed and 6 known failures in 127.00s.
- The repository baseline was 2183 passed, 8 known failures, 1 warning, and 50
  subtests passed in 236.71s.
- Human ruling: test prompt-driven requirements through consumer-observable behavior.
  Do not add source-text or prompt-substring change detectors; task and final reviewers
  inspect the prompt content directly.

---

### Task 1: Versioned research artifact and data-policy contract

**Files:**
- Create: `src/athena/research/prepare/baseline_research.py`
- Create: `test/unit/research/prepare/__init__.py`
- Create: `test/unit/research/prepare/test_baseline_research_contract.py`

**Interfaces:**
- Produces: `DatasetAssessment`, `BaselineSource`, `CandidateDecision`, `BaselineResearch`, `BaselineDesignSelection`, `BaselineArtifacts`, `BaselineVerification`, `VerifiedBaseline`, and `BaselineResearchError`.
- Produces: `load_baseline_artifacts(root: Path) -> BaselineArtifacts`, `research_sha256(raw: bytes) -> str`, `load_cached_verified_baseline(root: Path) -> VerifiedBaseline | None`, `assert_verified_files(root: Path, verified: VerifiedBaseline) -> None`, and `write_verification(root: Path, verification: BaselineVerification) -> Path`.
- Consumes later: `BaselineSourceVerifier.verify(artifacts: BaselineArtifacts) -> BaselineVerification` from Task 3.

- [x] **Step 1: Write failing artifact-schema and cross-file tests**

Create a concrete valid fixture and tests for duplicate IDs, selected-decision mismatch,
one-candidate justification, exact design markers, URL credentials, and policy pairs:

```python
def valid_payload() -> dict:
    return {
        "schema_version": 1,
        "dataset": {
            "modality": "image",
            "task_type": "classification",
            "labeled_samples": 480,
            "effective_training_units": 120,
            "group_count": 120,
            "class_count": 5,
            "minority_class_samples": 32,
            "input_scale": "paired 224x224 images",
            "regime": "small",
            "recommended_strategy": "partial_finetune",
            "evidence": ["eda:EDA_REPORT_LABELS.md: 480 labels across 120 groups"],
            "rationale": "Grouped labels are limited relative to pretrained vision capacity.",
        },
        "candidates": [
            {
                "candidate_id": "resnet-transfer",
                "title": "Deep Residual Learning for Image Recognition",
                "method": "pretrained ResNet feature extractor",
                "source_url": "https://arxiv.org/abs/1512.03385",
                "source_kind": "paper",
                "paper_locator": "doi:10.1109/CVPR.2016.90",
                "repository_url": "https://github.com/pytorch/vision.git",
                "publication_year": 2016,
                "claimed_citation_count": 100000,
                "relevance": "A standard transfer baseline for small image datasets.",
            },
            {
                "candidate_id": "linear-probe",
                "title": "PyTorch transfer learning tutorial",
                "method": "frozen visual features with a linear head",
                "source_url": "https://docs.pytorch.org/tutorials/beginner/transfer_learning_tutorial.html",
                "source_kind": "technical_reference",
                "paper_locator": None,
                "repository_url": "https://github.com/pytorch/tutorials.git",
                "publication_year": None,
                "claimed_citation_count": None,
                "relevance": "A conservative alternative for scarce labels.",
            },
        ],
        "decisions": [
            {"candidate_id": "resnet-transfer", "decision": "selected", "reason": "best fit"},
            {"candidate_id": "linear-probe", "decision": "rejected", "reason": "less adaptive"},
        ],
        "selected_candidate_id": "resnet-transfer",
        "search_queries": ["small image classification transfer baseline GitHub"],
        "limitations": [],
    }


def test_loads_matching_research_and_design(tmp_path: Path) -> None:
    (tmp_path / "BASELINE_RESEARCH.json").write_text(
        json.dumps(valid_payload()), encoding="utf-8"
    )
    (tmp_path / "BASELINE_DESIGN.md").write_text(
        "# Baseline\n\nSelected candidate: `resnet-transfer`\n"
        "Training strategy: `partial_finetune`\n",
        encoding="utf-8",
    )
    artifacts = load_baseline_artifacts(tmp_path)
    assert artifacts.selected.candidate_id == "resnet-transfer"
    assert artifacts.design.training_strategy == "partial_finetune"


@pytest.mark.parametrize(
    ("regime", "strategy"),
    [("unknown", "train_from_scratch"), ("tiny", "train_from_scratch")],
)
def test_rejects_unsafe_scratch_policy(regime: str, strategy: str) -> None:
    payload = valid_payload()
    payload["dataset"]["regime"] = regime
    payload["dataset"]["recommended_strategy"] = strategy
    with pytest.raises(ValidationError):
        BaselineResearch.model_validate(payload)
```

Add an adequate scratch case that passes only when `evidence` contains both an
`eda:` or `calculation:` entry and a concrete `source:resnet-transfer:` comparable-scale
support. Add a tabular/classical passing case to prove small tabular data is not forced
into transfer learning.

- [x] **Step 2: Run the focused tests and confirm the missing-module failure**

Run `.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare/test_baseline_research_contract.py`.
Expected: collection fails because `athena.research.prepare.baseline_research` does not exist.

- [x] **Step 3: Implement strict models, markers, and artifact loading**

Use strict, extra-forbidding Pydantic models and exact anchored Markdown markers:

```python
RESEARCH_FILENAME = "BASELINE_RESEARCH.json"
DESIGN_FILENAME = "BASELINE_DESIGN.md"
VERIFICATION_FILENAME = "BASELINE_RESEARCH_VERIFICATION.json"
AUTHORITY_CITATION_THRESHOLD = 100

_SELECTED_RE = re.compile(r"(?mi)^Selected candidate:\s*`([^`]+)`\s*$")
_STRATEGY_RE = re.compile(
    r"(?mi)^Training strategy:\s*`(classical|frozen_pretrained|partial_finetune|"
    r"full_finetune|train_from_scratch)`\s*$"
)


class BaselineResearchError(RuntimeError):
    def __init__(self, message: str, diagnostics: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.diagnostics = tuple(diagnostics)


class DatasetAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    modality: Literal[
        "tabular", "image", "text", "audio", "video", "time_series", "multimodal", "other"
    ]
    task_type: str = Field(min_length=1)
    labeled_samples: int | None = Field(default=None, ge=0)
    effective_training_units: int | None = Field(default=None, ge=0)
    group_count: int | None = Field(default=None, ge=0)
    class_count: int | None = Field(default=None, ge=1)
    minority_class_samples: int | None = Field(default=None, ge=0)
    input_scale: str = Field(min_length=1)
    regime: Literal["tiny", "small", "adequate", "unknown"]
    recommended_strategy: Literal[
        "classical", "frozen_pretrained", "partial_finetune",
        "full_finetune", "train_from_scratch"
    ]
    evidence: list[str] = Field(min_length=1)
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def reject_unsafe_scratch(self) -> "DatasetAssessment":
        if self.recommended_strategy == "train_from_scratch" and self.regime != "adequate":
            raise ValueError("train_from_scratch requires an adequate data regime")
        return self
```

`BaselineResearch`'s model validator must enforce unique candidate IDs, exactly one
selected decision, a decision for every candidate, matching `selected_candidate_id`,
and either two candidates or at least two distinct queries plus a non-empty limitation.
Its scratch branch must require both local data evidence and a
`source:{selected_candidate_id}:` evidence prefix. `BaselineSource` must reject URL userinfo.

Parse both markers exactly once and raise `BaselineResearchError` on missing, duplicate,
or mismatched values. Preserve raw JSON bytes in `BaselineArtifacts` so the digest is
computed over the file that was actually checked.

Use these immutable carrier shapes so later tasks share one exact interface:

```python
class BaselineDesignSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    selected_candidate_id: str
    training_strategy: TrainingStrategy


@dataclass(frozen=True)
class BaselineArtifacts:
    root: Path
    raw_research: bytes
    research: BaselineResearch
    design_text: str
    design: BaselineDesignSelection
    selected: BaselineSource


@dataclass(frozen=True)
class VerifiedBaseline:
    artifacts: BaselineArtifacts
    verification: BaselineVerification
```

Declare `TrainingStrategy` once as the `Literal` used by both `DatasetAssessment` and
`BaselineDesignSelection`; do not duplicate a diverging string union.

- [x] **Step 4: Implement digest-bound verification cache I/O**

Define the platform-owned record and atomic writer:

```python
class VerificationAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    route: Literal["git", "openalex"]
    success: bool
    diagnostic: str


class BaselineVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    research_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_candidate_id: str
    route: Literal["git", "openalex"]
    verified_at: datetime
    repository_url: str | None = None
    commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")
    openalex_id: str | None = None
    title: str | None = None
    publication_year: int | None = None
    cited_by_count: int | None = Field(default=None, ge=0)
    attempts: list[VerificationAttempt]


def write_verification(root: Path, verification: BaselineVerification) -> Path:
    target = root / VERIFICATION_FILENAME
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(verification.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(target)
    return target
```

`load_cached_verified_baseline` must return `None` rather than raise for a missing,
malformed, wrong-candidate, or wrong-digest verification file. It must still call
`load_baseline_artifacts` so design-marker mismatch cannot be hidden by a cache.
`assert_verified_files` reloads the artifacts and raises `BaselineResearchError` unless
their digest, candidate, and strategy equal the supplied `VerifiedBaseline`.

- [x] **Step 5: Run, format, and commit Task 1**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare/test_baseline_research_contract.py
.venv\Scripts\python.exe -m black src/athena/research/prepare/baseline_research.py test/unit/research/prepare/test_baseline_research_contract.py
git diff --check -- src/athena/research/prepare/baseline_research.py test/unit/research/prepare
```

Expected: all focused tests pass and both diff checks are clean. Commit only Task 1
paths with `git commit -m "feat: define baseline research contract"`.

---

### Task 2: Restricted public Git qualification

**Files:**
- Create: `src/athena/research/prepare/source_verification.py`
- Create: `test/unit/research/prepare/test_baseline_git_verification.py`

**Interfaces:**
- Consumes: a selected `BaselineSource.repository_url`.
- Produces: `GitCloneEvidence(repository_url: str, commit: str)`.
- Produces: `GitCloneVerifier.verify(repository_url: str) -> Awaitable[GitCloneEvidence]`.
- Produces: injectable `CommandRunner(argv, *, cwd, env, timeout_s) -> Awaitable[CompletedProcess[str]]`.

- [x] **Step 1: Write failing safe-clone tests with an async fake runner**

Use a fake that returns a 40-character commit for `rev-parse`, records every argument,
and never touches the network:

```python
class FakeRunner:
    def __init__(self, *, clone_code: int = 0, stderr: str = "") -> None:
        self.clone_code = clone_code
        self.stderr = stderr
        self.calls: list[tuple[list[str], Path | None, dict[str, str], float]] = []

    async def __call__(self, argv, *, cwd, env, timeout_s):
        self.calls.append((list(argv), cwd, dict(env), timeout_s))
        if "clone" in argv:
            return subprocess.CompletedProcess(argv, self.clone_code, "", self.stderr)
        return subprocess.CompletedProcess(argv, 0, "a" * 40 + "\n", "")


@pytest.mark.asyncio
async def test_git_verifier_uses_no_checkout_and_no_prompt() -> None:
    runner = FakeRunner()
    evidence = await GitCloneVerifier(runner=runner).verify(
        "https://github.com/pytorch/vision.git"
    )
    clone_argv, _, clone_env, _ = runner.calls[0]
    assert "--no-checkout" in clone_argv
    assert ["--depth", "1"] == clone_argv[clone_argv.index("--depth") : clone_argv.index("--depth") + 2]
    assert clone_env["GIT_TERMINAL_PROMPT"] == "0"
    assert evidence.commit == "a" * 40
```

Parametrize rejection of `http://example.com/x.git`, `ssh://host/x.git`,
`git@github.com:org/repo.git`, `file:///tmp/repo`, a local path, userinfo, query, and
fragment. Add clone non-zero, `asyncio.TimeoutError`, invalid HEAD, 20,000-character
stderr capping/redaction, and temporary-directory removal cases.

- [x] **Step 2: Run the Git tests and confirm missing symbols**

Run `.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare/test_baseline_git_verification.py`.
Expected: import failure for `GitCloneVerifier` and `GitCloneEvidence`.

- [x] **Step 3: Implement URL validation, the async runner, and two Git commands**

The production runner must call `subprocess.run` through `asyncio.to_thread` with a list
argument and `shell=False` behavior:

```python
async def run_command(
    argv: Sequence[str], *, cwd: Path | None, env: Mapping[str, str], timeout_s: float
) -> subprocess.CompletedProcess[str]:
    return await asyncio.to_thread(
        subprocess.run,
        list(argv),
        cwd=cwd,
        env=dict(env),
        timeout=timeout_s,
        check=False,
        capture_output=True,
        text=True,
    )
```

`GitCloneVerifier.verify` must create one `TemporaryDirectory`, an empty hooks directory,
and a clone target. Build the command as:

```python
git_config = [
    "-c", f"core.hooksPath={hooks_dir}",
    "-c", "protocol.file.allow=never",
    "-c", "protocol.ext.allow=never",
    "-c", "protocol.ssh.allow=never",
    "-c", "protocol.git.allow=never",
    "-c", "protocol.http.allow=never",
    "-c", "protocol.https.allow=always",
]
clone_argv = [
    "git", *git_config, "clone", "--depth", "1", "--filter=blob:none",
    "--no-checkout", "--", normalized_url, str(clone_dir),
]
head_argv = ["git", "-C", str(clone_dir), "rev-parse", "HEAD"]
```

Set `GIT_TERMINAL_PROMPT=0` and `GCM_INTERACTIVE=Never` in an environment copied from
`os.environ`. Catch `TimeoutError`/`subprocess.TimeoutExpired`, sanitize credentials,
collapse whitespace, and cap every diagnostic at 4,000 characters. Raise
`BaselineResearchError("Git source verification failed", diagnostics=[message])` for
all rejected paths.

- [x] **Step 4: Run, format, and commit Task 2**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare/test_baseline_git_verification.py
.venv\Scripts\python.exe -m black src/athena/research/prepare/source_verification.py test/unit/research/prepare/test_baseline_git_verification.py
git diff --check -- src/athena/research/prepare/source_verification.py test/unit/research/prepare/test_baseline_git_verification.py
```

Expected: all cases pass without a network request. Commit only Task 2 paths with
`git commit -m "feat: verify baseline repositories safely"`.

---

### Task 3: OpenAlex authority exception and composed verifier

**Files:**
- Modify: `src/athena/research/literature/paper_source/openalex.py:28-98`
- Modify: `src/athena/research/prepare/source_verification.py`
- Create: `test/unit/research/prepare/test_baseline_source_verification.py`
- Modify: `test/unit/test_paper_source.py`

**Interfaces:**
- Extends: `OpenAlexWork.cited_by_count: int` with default `0` and `ge=0`.
- Produces: `titles_match(reported: str, resolved: str) -> bool`.
- Produces: `BaselineSourceVerifier(git: GitCloneVerifier, openalex: OpenAlexLookup, now: Callable[[], datetime])`.
- Produces: `BaselineSourceVerifier.verify(artifacts: BaselineArtifacts) -> Awaitable[BaselineVerification]`.
- Produces: `build_default_source_verifier() -> BaselineSourceVerifier` using the existing `HostRateLimiter`, `UrllibTransport`, and `OpenAlexClient`.

- [x] **Step 1: Write failing OpenAlex parse and qualification tests**

Add a parser assertion to `test_paper_source.py`:

```python
def test_openalex_work_retains_citation_count() -> None:
    work = parse_work(
        {
            "id": "https://openalex.org/W123",
            "display_name": "A Baseline Paper",
            "publication_year": 2020,
            "cited_by_count": 137,
        }
    )
    assert work.openalex_id == "W123"
    assert work.cited_by_count == 137
```

In the new verifier test file, define fake Git and OpenAlex adapters and cover these
routes:

```python
@pytest.mark.asyncio
async def test_repository_success_does_not_require_openalex(valid_artifacts) -> None:
    git = FakeGit(result=GitCloneEvidence("https://github.com/org/repo.git", "b" * 40))
    openalex = FakeOpenAlex(error=AssertionError("OpenAlex must not be called"))
    result = await BaselineSourceVerifier(git=git, openalex=openalex).verify(valid_artifacts)
    assert result.route == "git"
    assert result.commit == "b" * 40


@pytest.mark.asyncio
@pytest.mark.parametrize(("citations", "passes"), [(99, False), (100, True)])
async def test_openalex_threshold_is_inclusive(valid_artifacts, citations, passes) -> None:
    valid_artifacts.research.candidates[0].repository_url = None
    openalex = FakeOpenAlex(
        work=OpenAlexWork(
            openalex_id="W123", title=valid_artifacts.selected.title,
            publication_year=2016, cited_by_count=citations
        )
    )
    verifier = BaselineSourceVerifier(git=FakeGit(), openalex=openalex)
    if passes:
        assert (await verifier.verify(valid_artifacts)).route == "openalex"
    else:
        with pytest.raises(BaselineResearchError, match="no qualifying source"):
            await verifier.verify(valid_artifacts)
```

Also test title mismatch, unresolved locator, OpenAlex outage, Git failure followed by
OpenAlex success, both routes failing with two bounded attempts, and agent-claimed
citations being ignored.

- [x] **Step 2: Run the focused tests and confirm the new metadata/route failures**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/test_paper_source.py test/unit/research/prepare/test_baseline_source_verification.py
```

Expected: the citation field assertion and composed-verifier imports fail.

- [x] **Step 3: Parse citation metadata and implement deterministic title matching**

Add to `OpenAlexWork` and `parse_work`:

```python
cited_by_count: int = Field(
    default=0, ge=0, description="OpenAlex cited_by_count at fetch time."
)

count = payload.get("cited_by_count")
# in OpenAlexWork(...)
cited_by_count=count if isinstance(count, int) and count >= 0 else 0,
```

Normalize titles with Unicode NFKC, `casefold()`, and alphanumeric token joining.
`titles_match` passes on exact normalized equality, or `SequenceMatcher` ratio at least
`0.90` when both normalized titles have at least 20 characters. No fuzzy match is
allowed for shorter titles.

- [x] **Step 4: Compose Git-first and OpenAlex-fallback verification**

Implement this exact precedence:

```python
attempts: list[VerificationAttempt] = []
if selected.repository_url is not None:
    try:
        evidence = await self.git.verify(str(selected.repository_url))
        return BaselineVerification(
            research_sha256=research_sha256(artifacts.raw_research),
            selected_candidate_id=selected.candidate_id,
            route="git",
            verified_at=self.now(),
            repository_url=evidence.repository_url,
            commit=evidence.commit,
            attempts=[*attempts, VerificationAttempt(route="git", success=True, diagnostic="clone verified")],
        )
    except BaselineResearchError as error:
        attempts.append(VerificationAttempt(route="git", success=False, diagnostic=joined_diagnostic(error)))

if selected.paper_locator:
    work = await self.openalex.fetch_work(selected.paper_locator)
    if work is not None and titles_match(selected.title, work.title) and work.cited_by_count >= 100:
        return BaselineVerification(
            research_sha256=research_sha256(artifacts.raw_research),
            selected_candidate_id=selected.candidate_id,
            route="openalex",
            verified_at=self.now(),
            openalex_id=work.openalex_id,
            title=work.title,
            publication_year=work.publication_year,
            cited_by_count=work.cited_by_count,
            attempts=[*attempts, VerificationAttempt(route="openalex", success=True, diagnostic="authority threshold verified")],
        )
```

Convert `fetch_work` exceptions and every non-qualifying result into a failed OpenAlex
attempt. If neither route returns, raise `BaselineResearchError("selected candidate has no qualifying source", diagnostics=[attempt.diagnostic for attempt in attempts])`.

`build_default_source_verifier` must construct only metadata access; it must not invoke
the paid OpenAlex content endpoint or require an API key.

- [x] **Step 5: Run, format, and commit Task 3**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/test_paper_source.py test/unit/research/prepare/test_baseline_git_verification.py test/unit/research/prepare/test_baseline_source_verification.py
.venv\Scripts\python.exe -m black src/athena/research/literature/paper_source/openalex.py src/athena/research/prepare/source_verification.py test/unit/test_paper_source.py test/unit/research/prepare/test_baseline_source_verification.py
git diff --check -- src/athena/research/literature/paper_source/openalex.py src/athena/research/prepare/source_verification.py test/unit/test_paper_source.py test/unit/research/prepare
```

Expected: all source-verification tests pass. Commit only Task 3 paths with
`git commit -m "feat: add baseline authority verification"`.

---

### Task 4: Baseline-only web research tools and prompt contracts

**Files:**
- Modify: `src/athena/retrieval/web_search.py`
- Modify: `src/athena/research/turns/general.py:15-51`
- Modify: `src/athena/research/runtime/bootstrap.py:285-340`
- Modify: `src/athena/research/runtime/facade.py:440-451`
- Modify: `src/athena/agents/ideator_agent.py:49-59`
- Modify: `src/athena/agents/prompts/baseline_ideator_agent.md`
- Modify: `src/athena/agents/prompts/prepare_agent.md`
- Create: `test/unit/research/prepare/test_baseline_research_tools.py`
- Modify: `test/unit/retrieval/test_web_search.py`

**Interfaces:**
- Produces: `build_web_tools() -> ToolRegistry`, containing `web_search` and `web_fetch` backed by one `WebSession`.
- Produces: `bootstrap.baseline_ideator_tools(runtime) -> Callable[[], ToolRegistry]`.
- Produces: `ResearchRuntime.baseline_ideator_tools() -> Callable[[], ToolRegistry]`.
- Preserves: `ResearchRuntime.ideator_tools()` without web tools.

- [x] **Step 1: Write failing tool-isolation and shared-session tests**

```python
def test_build_web_tools_shares_search_session() -> None:
    tools = build_web_tools()
    search = tools.resolve("web_search")
    fetch = tools.resolve("web_fetch")
    assert search.session is fetch.session


def test_only_baseline_ideator_tools_include_web() -> None:
    runtime = SimpleNamespace(
        kaggle_tools=lambda _kind: None,
        corpus_tools=lambda **_kwargs: None,
    )
    assert ideator_tools(runtime)() is None
    names = {spec.name for spec in baseline_ideator_tools(runtime)().specs}
    assert names == {"web_fetch", "web_search"}
```

Add a merge case with fake Kaggle and corpus tools, asserting the baseline registry has
all names while the ordinary registry has no web names. Keep the existing General Agent
test green to prove the refactor does not remove its web pair.

- [x] **Step 2: Add behavior-level prompt-consumer coverage**

Do not add prompt-substring or source-text change detectors. Prove the requirements at
their deterministic consumer boundaries instead:

- Task 1 artifact tests reject missing or mismatched filenames, design markers,
  candidate decisions, and training strategies.
- This task's registry tests prove only the baseline ideator receives the shared web
  tools and that ordinary ideators retain their prior registry.
- Task 5 orchestration tests prove the ideator must produce both complete artifacts,
  verification precedes PREPARE registration, and the prepare task receives the three
  artifact filenames plus candidate, route, revision/authority, and strategy.
- Task 6 integration tests prove the reports and handoff preserve verified provenance.

The task reviewer must manually inspect both modified prompts against Step 5 and record
the result in the review report.

- [x] **Step 3: Run focused tests and confirm missing factory/prompt clauses**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/retrieval/test_web_search.py test/unit/research/prepare/test_baseline_research_tools.py test/unit/research/supervisor/test_prepare_prompt_contract.py
```

Expected: the missing factory and baseline-only provider assertions fail; existing
prompt-consumer coverage remains green until the new orchestration tests are added.

- [x] **Step 4: Extract the reusable web pair and wire only the baseline ideator**

Add to `web_search.py`:

```python
def build_web_tools() -> ToolRegistry:
    session = WebSession()
    registry = ToolRegistry()
    registry.register(WebSearchTool(session=session))
    registry.register(WebFetchTool(session=session))
    return registry
```

Replace GeneralTurnMixin's local session construction with `build_web_tools()`, merging
Kaggle tools into that registry. In `bootstrap.py`, add:

```python
def baseline_ideator_tools(runtime: Any) -> Callable[[], ToolRegistry]:
    def build() -> ToolRegistry:
        return _merged(
            runtime.kaggle_tools("ideator"),
            runtime.corpus_tools(for_ideation=True),
            build_web_tools(),
        )
    return build
```

Because `build_web_tools()` is always present, tighten `_merged`'s return at this call
site with an assertion or overload; do not change `_merged` behavior for ordinary
ideators. Add the matching facade import, method, and `__all__` export.

Change `_register_ideator` in Task 5 to consume `runtime.baseline_ideator_tools()`;
ordinary ideator registration continues to consume `runtime.ideator_tools()`.

- [x] **Step 5: Replace the baseline ideator prompt with the approved contract**

The prompt must require reading the task/data contract and `EDA_HANDOFF.md`, at least
two distinct search queries, primary/official sources, two candidates unless the
single-candidate exception is documented, and both complete files. It must require
these exact Markdown lines:

```markdown
Selected candidate: `resnet-transfer`
Training strategy: `partial_finetune`
```

It must state that citation claims are not authoritative, Git proof is performed by the
platform, the shell must not clone/install/execute candidate repositories, and evidence
strings use `eda:`, `data_contract:`, `calculation:`, and, for scratch training,
`source:{selected_candidate_id}:` prefixes. It must use the approved candidate
precedence (paper plus official Git, official implementation plus Git, then the
high-citation paper exception), and it must record any source-license constraint in the
Markdown design. Keep the final `HandoffResult` JSON pointing
to `BASELINE_DESIGN.md`.

The prepare prompt must read the research, verification, and design files before
editing code; refuse to invent another method; include candidate ID, verification route,
commit/OpenAlex evidence, and training strategy in the report and
`RESEARCH_HANDOFF.md`; and retain all existing environment/evaluator constraints.

- [x] **Step 6: Run, format, and commit Task 4**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/retrieval/test_web_search.py test/unit/research/prepare/test_baseline_research_tools.py test/unit/research/supervisor/test_prepare_prompt_contract.py test/unit/research/test_runtime_survey.py
.venv\Scripts\python.exe -m black src/athena/retrieval/web_search.py src/athena/research/turns/general.py src/athena/research/runtime/bootstrap.py src/athena/research/runtime/facade.py src/athena/agents/ideator_agent.py test/unit/retrieval/test_web_search.py test/unit/research/prepare/test_baseline_research_tools.py
git diff --check -- src/athena/retrieval/web_search.py src/athena/research/turns/general.py src/athena/research/runtime/bootstrap.py src/athena/research/runtime/facade.py src/athena/agents test/unit/retrieval/test_web_search.py test/unit/research/prepare
```

Expected: tool isolation, existing prompt-consumer behavior, existing web behavior,
and ordinary ideator tests pass. Commit only Task 4 paths with
`git commit -m "feat: add web research baseline prompts"`.

---

### Task 5: Two-turn research gate and PREPARE ordering

**Files:**
- Modify: `src/athena/research/prepare/baseline.py:1-123`
- Modify: `src/athena/research/prepare/orchestrator.py:38-53`
- Create: `test/unit/research/prepare/test_baseline_research_orchestration.py`
- Modify: `test/unit/research/test_prepare_modules.py`

**Interfaces:**
- Changes: `prepare_baseline_design(...) -> Awaitable[VerifiedBaseline]`.
- Changes: `run_baseline(..., verified: VerifiedBaseline) -> Awaitable[PrepareResult]`.
- Produces: `verified_baseline_task(task: str, verified: VerifiedBaseline) -> str`.
- Consumes: `runtime.baseline_ideator_tools()`, `build_default_source_verifier()`, and the existing reusable handoff agent thread.

- [ ] **Step 1: Write failing first-pass, repair, terminal-failure, and resume tests**

Use a fake handoff function that writes complete files for each scripted response and
records `agent_id`, content, and `reap_after`. Use a fake verifier whose outcomes are
queued. The tests must prove:

```python
@pytest.mark.asyncio
async def test_valid_first_response_writes_verification_and_reaps(tmp_path: Path) -> None:
    runtime, handoff, verifier = harness(tmp_path, outcomes=[valid_verification()])
    result = await prepare_baseline_design(
        runtime, workspace(tmp_path), "task", True, handoff, verifier=verifier
    )
    assert result.verification.route == "git"
    assert (tmp_path / "BASELINE_RESEARCH_VERIFICATION.json").is_file()
    assert runtime.agents.reaped == ["baseline_ideator"]
    assert len(handoff.calls) == 1


@pytest.mark.asyncio
async def test_invalid_first_response_gets_exactly_one_repair(tmp_path: Path) -> None:
    runtime, handoff, verifier = harness(
        tmp_path,
        outcomes=[BaselineResearchError("bad source", ["clone failed"]), valid_verification()],
    )
    await prepare_baseline_design(
        runtime, workspace(tmp_path), "task", True, handoff, verifier=verifier
    )
    assert len(handoff.calls) == 2
    assert "clone failed" in handoff.calls[1]["content"]
    assert "rewrite both complete artifacts" in handoff.calls[1]["content"]
```

Add tests for two invalid responses raising `BaselineResearchError`, `eda_ready=False`
hard failure, missing research file receiving repair rather than task-only fallback,
matching cache returning without an agent call, wrong digest causing revalidation,
pre-existing unverified artifacts being verified before an agent call, invalid resumed
artifacts receiving only one repair turn, and cleanup after exceptions.

- [ ] **Step 2: Write failing prepare-registration ordering tests**

Monkeypatch `_register_prepare_agent` and `run_prepare_plan` in `baseline.py`:

```python
@pytest.mark.asyncio
async def test_run_baseline_rejects_changed_research_before_registering_prepare(
    monkeypatch, tmp_path: Path
) -> None:
    verified = write_valid_verified_fixture(tmp_path)
    (tmp_path / "BASELINE_RESEARCH.json").write_text("{}", encoding="utf-8")
    registered: list[str] = []
    monkeypatch.setattr(baseline, "_register_prepare_agent", lambda *_: registered.append("prepare"))
    with pytest.raises(BaselineResearchError, match="digest"):
        await run_baseline(fake_runtime(), workspace(tmp_path), "eval", "task", None, verified)
    assert registered == []
```

Add a passing case that captures the task passed to `run_prepare_plan` and asserts it
contains all three filenames, candidate ID, route, commit/OpenAlex ID, and selected
training strategy.

- [ ] **Step 3: Run orchestration tests and confirm old fallback/signature failures**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare/test_baseline_research_orchestration.py test/unit/research/test_prepare_modules.py
```

Expected: new return values, hard gate, retry behavior, and function signatures fail.

- [ ] **Step 4: Implement cache-first verification and one repair turn**

In `prepare_baseline_design`:

1. Return a matching `load_cached_verified_baseline(root)` before creating an agent.
2. If both research/design files exist without a valid cache, load and verify them once.
3. If resumed artifacts fail, register the ideator and give exactly one repair request.
4. If no complete artifacts exist, run one initial research request and allow one repair
   request after a parse or verification failure.
5. Before every agent turn remove `BASELINE_RESEARCH_VERIFICATION.json`; ignore anything
   the agent writes at that path and overwrite it only after independent verification.
6. Reap `baseline_ideator` in `finally` with `asyncio.wait_for(..., timeout=5.0)` and log
   cleanup failure without hiding the primary exception.

Use explicit prompt builders:

```python
def _initial_research_request(task: str) -> str:
    return (
        f"{task}\n\nRead the task/data contract and EDA_HANDOFF.md. Use web and "
        "paper tools, then write complete BASELINE_RESEARCH.json and BASELINE_DESIGN.md."
    )


def _repair_request(error: BaselineResearchError) -> str:
    diagnostics = "\n".join(f"- {item}" for item in error.diagnostics)
    return (
        "The deterministic baseline evidence gate rejected the artifacts.\n"
        f"{diagnostics}\n"
        "Correct the source or locator and rewrite both complete artifacts. "
        "Do not write BASELINE_RESEARCH_VERIFICATION.json."
    )
```

`_register_ideator` must use `runtime.baseline_ideator_tools()`. Remove the broad
`except Exception` that publishes “using the task only.” Convert handoff/parsing errors
to bounded `BaselineResearchError` diagnostics and publish a terminal PREPARE error
before re-raising after the last allowed response.

- [ ] **Step 5: Gate prepare registration and thread the verified value through orchestration**

`verified_baseline_task` appends a compact, deterministic block:

```text
Platform-verified baseline artifacts:
- BASELINE_RESEARCH.json
- BASELINE_RESEARCH_VERIFICATION.json
- BASELINE_DESIGN.md
Selected candidate: resnet-transfer
Training strategy: partial_finetune
Verification route: git
Verified revision: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
```

Use `assert_verified_files(root, verified)` immediately before
`_register_prepare_agent`. Then pass the enriched task to `run_prepare_plan`.

Change the orchestrator to:

```python
verified = await prepare_baseline_design(
    runtime, workspace, candidate_task, eda_ready, run_handoff_agent
)
return await run_baseline(
    runtime, workspace, evaluators.search_ref, candidate_task,
    predict_features, verified
)
```

No `run_baseline` call may occur if research verification raises.

- [ ] **Step 6: Run, format, and commit Task 5**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare test/unit/research/test_prepare_modules.py test/unit/research/test_handoff_agent_events.py test/unit/research/supervisor/test_prepare_plan.py test/integration/research/test_prepare_agent_contract.py
.venv\Scripts\python.exe -m black src/athena/research/prepare/baseline.py src/athena/research/prepare/orchestrator.py test/unit/research/prepare/test_baseline_research_orchestration.py test/unit/research/test_prepare_modules.py
git diff --check -- src/athena/research/prepare test/unit/research/prepare test/unit/research/test_prepare_modules.py
```

Expected: all focused and existing PREPARE tests pass. Commit only Task 5 paths with
`git commit -m "feat: gate baseline preparation on verified evidence"`.

---

### Task 6: End-to-end evidence-gate integration coverage

**Files:**
- Create: `test/integration/research/test_authoritative_baseline_gate.py`
- Modify: `test/integration/research/test_prepare_agent_contract.py`

**Interfaces:**
- Exercises: `run_prepare_phase(runtime, run_handoff_agent)` through research gate into the existing trusted prepare plan.
- Verifies: no prepare agent before qualification, valid Git and OpenAlex routes, one repair, provenance output, and no network dependency.

- [ ] **Step 1: Add a fake-provider integration harness**

Build on the existing `_Harness` patterns. Seed `EDA_HANDOFF.md`, script a baseline
ideator provider to write both research files, inject fake Git/OpenAlex verifiers, and
keep the existing frozen evaluator and `_ManifestExecution`. The prepare provider must
read the enriched task and write provenance into its outputs:

```python
("report.md", "# PREPARE baseline\nCandidate: resnet-transfer\nRoute: git\nStrategy: partial_finetune\n"),
("RESEARCH_HANDOFF.md", "candidate=resnet-transfer\nroute=git\nstrategy=partial_finetune\n"),
```

Do not replace trusted `run_prepare_plan` or `TrustedEvaluator` in the successful path.

- [ ] **Step 2: Add the acceptance-path integration tests**

Add tests with these explicit assertions:

```python
@pytest.mark.asyncio
async def test_git_qualified_research_reaches_trusted_baseline(harness) -> None:
    result = await harness.run(git_commit="c" * 40)
    root = Path(harness.branch.path)
    verification = BaselineVerification.model_validate_json(
        (root / "BASELINE_RESEARCH_VERIFICATION.json").read_text(encoding="utf-8")
    )
    assert verification.route == "git"
    assert verification.commit == "c" * 40
    assert result.metric == 1.0
    assert "resnet-transfer" in (root / "RESEARCH_HANDOFF.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_two_research_failures_never_start_prepare(harness) -> None:
    with pytest.raises(BaselineResearchError):
        await harness.run(research_responses=["invalid", "still invalid"])
    assert harness.prepare_provider.calls == 0
```

Also cover a repository-free 100-citation paper, a 99-citation rejection, Git failure
then OpenAlex exception, and invalid-first/valid-repair using the same logical
`baseline_ideator` agent ID.

- [ ] **Step 3: Run integration tests and fix only contract mismatches**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q test/integration/research/test_authoritative_baseline_gate.py test/integration/research/test_prepare_agent_contract.py
```

Expected: all integration cases pass, trusted metric remains `1.0`, and no external
network call occurs.

- [ ] **Step 4: Run the complete affected test slice and commit Task 6**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare test/unit/research/test_prepare_modules.py test/unit/research/test_runtime_survey.py test/unit/research/supervisor/test_prepare_plan.py test/unit/research/supervisor/test_prepare_prompt_contract.py test/unit/retrieval/test_web_search.py test/unit/test_paper_source.py test/integration/research/test_authoritative_baseline_gate.py test/integration/research/test_prepare_agent_contract.py
git diff --check -- test/integration/research/test_authoritative_baseline_gate.py test/integration/research/test_prepare_agent_contract.py
```

Expected: the entire affected slice passes. Commit only Task 6 test paths with
`git commit -m "test: cover authoritative baseline prepare flow"`.

---

### Task 7: Documentation, full verification, and plan closeout

**Files:**
- Modify: `docs/eda_workflow.md`
- Modify: `docs/research_core_mechanisms_ch.md`
- Modify: `docs/superpowers/specs/2026-09-02-authoritative-baseline-research-design.md`
- Create: `codex_docs/2026-09-02-authoritative-baseline-research-completion-report.md`
- Modify: `codex_docs/CURRENT.md`
- Delete after all acceptance evidence exists: `docs/superpowers/plans/2026-09-02-authoritative-baseline-research-gate.md`

**Interfaces:**
- Produces: user-facing PREPARE flow and artifact documentation.
- Produces: completion evidence tied to every acceptance criterion.
- Leaves: no active plan pointer to a deleted file.

- [ ] **Step 1: Update user-facing workflow documentation**

Change the EDA workflow diagram to:

```text
EDA_HANDOFF.md
  -> baseline web/paper research
  -> BASELINE_RESEARCH.json + BASELINE_DESIGN.md
  -> platform Git/OpenAlex verification
  -> BASELINE_RESEARCH_VERIFICATION.json
  -> prepare implementation
  -> trusted scoring
```

Document the Git-first/100-citation exception, modality-aware strategy assessment,
single repair turn, terminal failure, three durable artifacts, digest-based resume,
and the fact that repository code is not executed. Update the PREPARE mechanism and
module tables in the Chinese core-mechanisms document. Do not claim a repository is
safe merely because it cloned.

- [ ] **Step 2: Run formatting, static, and import checks**

Run:

```powershell
.venv\Scripts\python.exe -m black --check src/athena/research/prepare/baseline_research.py src/athena/research/prepare/source_verification.py src/athena/research/prepare/baseline.py src/athena/research/prepare/orchestrator.py src/athena/research/literature/paper_source/openalex.py src/athena/retrieval/web_search.py src/athena/research/turns/general.py src/athena/research/runtime/bootstrap.py src/athena/research/runtime/facade.py src/athena/agents/ideator_agent.py test/unit/research/prepare test/unit/test_paper_source.py test/unit/retrieval/test_web_search.py test/integration/research/test_authoritative_baseline_gate.py test/integration/research/test_prepare_agent_contract.py
.venv\Scripts\python.exe -m compileall -q src/athena
.venv\Scripts\python.exe -c "from athena.research.prepare.baseline_research import BaselineResearch, BaselineVerification; from athena.research.prepare.source_verification import BaselineSourceVerifier, GitCloneVerifier; from athena.research.prepare.baseline import prepare_baseline_design"
git diff --check
```

Expected: every command exits zero. If `git diff --check` reports an unrelated pre-existing
path, record it separately and rerun the check with every task-owned path explicitly.

- [ ] **Step 3: Run targeted, research-wide, and repository-wide tests**

Run in this order:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare test/unit/research/test_prepare_modules.py test/unit/research/test_runtime_survey.py test/unit/research/supervisor/test_prepare_plan.py test/unit/research/supervisor/test_prepare_prompt_contract.py test/unit/retrieval/test_web_search.py test/unit/test_paper_source.py test/integration/research/test_authoritative_baseline_gate.py test/integration/research/test_prepare_agent_contract.py
.venv\Scripts\python.exe -m pytest -q test/unit/research test/integration/research
.venv\Scripts\python.exe -m pytest -q
```

Expected: no new failure relative to the execution-preflight baseline. Record command,
timestamp, pass/fail/skip counts, duration, and every remaining known failure in the
completion report. Do not describe the suite as green if any command exits non-zero.

- [ ] **Step 4: Audit the security boundary from fresh evidence**

Run:

```powershell
rg -n "shell=True|checkout|pip install|uv add|copytree|GIT_TERMINAL_PROMPT|protocol\.file\.allow|--no-checkout|cited_by_count" src/athena/research/prepare src/athena/research/literature/paper_source/openalex.py
```

Required evidence: no `shell=True`, checkout, package installation, or repository copy
in the verifier; prompt disabling, non-HTTPS protocol denial, `--no-checkout`, and the
OpenAlex citation field are present. Summarize this audit in the completion report.

- [ ] **Step 5: Write the completion report and close the active plan**

The completion report must contain:

```markdown
# Authoritative Baseline Research Gate Completion Report

Date: 2026-09-02
Implementation commits:

## Delivered behavior
## Acceptance criteria evidence
## Verification commands and results
## Security/provenance audit
## Remaining known failures
## Unrelated worktree changes preserved
```

List the actual Task 1-7 commit hashes as bullets under `Implementation commits`.
Set the design spec status to `implemented and verified`. Delete this implementation
plan only after every acceptance criterion has fresh evidence. Update `CURRENT.md` to:

```markdown
# Current Athena Work

Active implementation plan:
- None.

Most recent completed work:
- `codex_docs/2026-09-02-authoritative-baseline-research-completion-report.md`

Design spec:
- `docs/superpowers/specs/2026-09-02-authoritative-baseline-research-design.md`

Paused implementation plan:
- `docs/superpowers/plans/2026-09-02-jw-ssd-tui-run.md` (not started)
```

- [ ] **Step 6: Commit only documentation and closeout files**

Stage only `docs/eda_workflow.md`, `docs/research_core_mechanisms_ch.md`, the design
spec, the completion report, `codex_docs/CURRENT.md`, and the deletion of this plan.
Review `git diff --cached --name-status` before committing. Commit with
`git commit -m "docs: complete authoritative baseline research gate"` and report the
final hash plus all verification evidence.
