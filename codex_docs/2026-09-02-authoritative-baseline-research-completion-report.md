# Authoritative Baseline Research Gate Completion Report

Date: 2026-09-02
Status: reopened by final whole-branch review; not complete until hardening Task 7 evidence exists

Hardening notice (2026-09-03): this is a historical completion report for the original
authoritative baseline gate. Final branch review reopened completion because the
workspace-local cache and version-one schema guarantees were superseded by
`docs/superpowers/specs/2026-09-03-authoritative-baseline-hardening-design.md`. The
commands, results, regression history, and known-failure data below are intentionally
preserved, but they are not closeout evidence for the hardening work. References to
"Task 7" below name the original 2026-09-02 implementation plan; the reopened status
remains in force until the 2026-09-03 hardening plan's Task 7 produces fresh final
evidence.

Implementation commits:

- Task 1 — research contract: `5732c6bfe6df2c51fd854871f7106fa7088b8c6e`,
  `283712c66abdb612c0ba8b69652f675bdc0192df`,
  `036965b8a1967aa44d7832fc6eed659be1702bea`
- Task 2 — restricted Git verification: `0acb4bc066af6f941fc4eb2d302d9d8b40030b23`,
  `2a7f12dec4196323d1e827ab308bffe66242d654`,
  `b3299390521a3b86ff83da4ec1b31c21dbb5d1ac`,
  `c7ff73fc8c8c87ed3d4d28461c5fa51132c0f07a`,
  `0db11a11eae610e3baec048b32f9a133e6ebc24a`,
  `5519e9d64301776d9e1b172c47f4cbaa2bfdb374`,
  `51661c071641832220167494bb80e63ebae01f0c`
- Task 3 — Git/OpenAlex source composition: `997e6bc8b5021ab36215bab38dccdec7d27b7df7`,
  `1a9aebd47d166ba39761e7d9c07c28f7f97650bf`,
  `2df4e7d2647003cc6679612ec9620885d1f6acb1`
- Task 4 — baseline-only research tools and prompts:
  `fdb9e3cbc52daa7d817bc8351cede2910fb45251`,
  `a23c6938b63ceb9376ade58e86732d2641fefb1c`
- Task 5 — verified PREPARE lifecycle: `b6cdf9dc39892d98d25bc13b6688b06a3e166ff1`,
  `ef22ca995d456898ba9d303d5ca561f969dd3253`,
  `c9d5a768bb7fef11b8e3bb57a85810e2f3e01736`,
  `b690870b4cb0a9065278731d9c948e42f49f187b`
- Task 6 — end-to-end evidence gate: `0a8c5eaa509384f4fc0a2444116e7052be39a241`,
  `e53f7bf4a375fc5d2bbde06538d896456da5bb70`
- Task 7 — stale-fixture repair, with the production gate unchanged:
  `dba438c77541282dd03a8876a54e4b6c35b38bd5`
- Task 7 — documentation closeout:
  `36b3b6da3d65797e2a405e93293c0bdb205ed4e6`
- Task 7 — report bookkeeping: the follow-up commit named
  `docs: record authoritative baseline closeout hash` records the closeout hash above.
  Its own hash is intentionally not embedded because a commit cannot contain its own
  final hash.

## Delivered behavior

PREPARE now follows one evidence-gated path:

```text
EDA_HANDOFF.md
  -> baseline web/paper research
  -> BASELINE_RESEARCH.json + BASELINE_DESIGN.md
  -> platform Git/OpenAlex verification
  -> BASELINE_RESEARCH_VERIFICATION.json
  -> prepare implementation
  -> trusted scoring
```

The baseline ideator receives baseline-only web search/fetch tools, reads the task/data
contract and EDA handoff, records search queries and candidate decisions, and writes a
typed research artifact plus a matching Markdown design. Its data-regime assessment is
modality-aware rather than based on a universal sample-count cutoff. An `unknown`
regime cannot choose scratch training; scratch training additionally requires explicit
local EDA/calculation evidence and comparable-scale source evidence.

The platform independently qualifies the selected source. It tries a credential-free
public HTTPS Git repository first. If no Git route qualifies, a DOI/OpenAlex locator
may use the repository-free exception only when the resolved title matches and OpenAlex
reports at least 100 citations. Agent-claimed citation counts never satisfy the gate.

A rejected first response receives exactly one structured repair turn on the same
baseline-ideator thread. The agent must rewrite both complete artifacts. A second
failure raises `BaselineResearchError` and stops PREPARE before the prepare agent is
registered; the retired task-only fallback is not used.

After qualification, Athena writes three durable workspace artifacts:
`BASELINE_RESEARCH.json`, `BASELINE_DESIGN.md`, and the platform-owned
`BASELINE_RESEARCH_VERIFICATION.json`. Resume reuses verification only while its
schema, selected candidate, and SHA-256 research digest still match. The prepare agent
then receives the three filenames, selected candidate, training strategy, and pinned
Git/OpenAlex provenance before normal trusted scoring.

## Acceptance criteria evidence

- **Three artifacts:** the targeted authoritative-gate integration tests passed and
  exercise the research, design, and platform verification files on accepted paths.
- **Qualified source:** unit/integration coverage passed for Git-first precedence,
  repository-free OpenAlex qualification, title matching, and the inclusive 99/100
  citation boundary.
- **No implementation before verification:** ordering and double-rejection tests passed;
  prepare-agent registration is absent when qualification fails.
- **Modality-aware strategy:** research-contract tests passed for modality/task evidence,
  allowed strategies, tabular classical behavior, and no global small-data cutoff.
- **Scratch safety:** contract tests passed for rejection without an `adequate` regime,
  local EDA/calculation evidence, and comparable-source evidence.
- **No third-party execution:** the fresh source audit found the restricted no-checkout
  Git boundary and no shell execution, package installation, or repository-copy path.
- **One repair then terminal failure:** focused orchestration and integration cases
  passed for invalid-first/valid-repair and twice-invalid termination.
- **Provenance handoff:** the successful integration path preserved candidate, route,
  revision/authority, and strategy through the report and `RESEARCH_HANDOFF.md`.
- **Existing boundaries:** the complete affected slice passed. The research-wide and
  repository-wide suites returned only the documented preflight-known failures; the
  evaluator, workspace, and trusted-scoring fixture regressions discovered during the
  first Task 7 run were repaired without changing production code.

## Verification commands and results

All timestamps use Asia/Shanghai (`+08:00`). These commands were rerun from scratch on
final code state `dba438c77541282dd03a8876a54e4b6c35b38bd5`; pre-repair evidence is not
used for completion.

1. Black formatting check

   ```powershell
   .venv\Scripts\python.exe -m black --check --workers 1 src/athena/research/prepare/baseline_research.py src/athena/research/prepare/source_verification.py src/athena/research/prepare/baseline.py src/athena/research/prepare/orchestrator.py src/athena/research/literature/paper_source/openalex.py src/athena/retrieval/web_search.py src/athena/research/turns/general.py src/athena/research/runtime/bootstrap.py src/athena/research/runtime/facade.py src/athena/agents/ideator_agent.py test/unit/research/prepare test/unit/test_paper_source.py test/unit/retrieval/test_web_search.py test/integration/research/test_authoritative_baseline_gate.py test/integration/research/test_prepare_agent_contract.py
   ```

   Start `2026-09-02T22:54:25.5417413+08:00`; exit `0`; 20 files unchanged;
   wall `0.433s`.

2. Compile check

   ```powershell
   .venv\Scripts\python.exe -m compileall -q src/athena
   ```

   Start `2026-09-02T22:54:44.3308755+08:00`; exit `0`; wall `0.425s`.

3. Import check

   ```powershell
   .venv\Scripts\python.exe -c "from athena.research.prepare.baseline_research import BaselineResearch, BaselineVerification; from athena.research.prepare.source_verification import BaselineSourceVerifier, GitCloneVerifier; from athena.research.prepare.baseline import prepare_baseline_design"
   ```

   Start `2026-09-02T22:54:46.1276165+08:00`; exit `0`; wall `4.438s`.

4. Full worktree diff check

   ```powershell
   git diff --check
   ```

   Start `2026-09-02T22:54:51.5061342+08:00`; exit `0`; wall `0.593s`.

5. Complete affected slice

   ```powershell
   .venv\Scripts\python.exe -m pytest -q test/unit/research/prepare test/unit/research/test_prepare_modules.py test/unit/research/test_runtime_survey.py test/unit/research/supervisor/test_prepare_plan.py test/unit/research/supervisor/test_prepare_prompt_contract.py test/unit/retrieval/test_web_search.py test/unit/test_paper_source.py test/integration/research/test_authoritative_baseline_gate.py test/integration/research/test_prepare_agent_contract.py
   ```

   Start `2026-09-02T22:55:02.6685975+08:00`; exit `0`; `291 passed`, `0 failed`,
   `0 skipped`, `0 warnings`, `3 subtests passed`; pytest `131.62s`, wall `134.353s`.

6. Research-wide suite

   ```powershell
   .venv\Scripts\python.exe -m pytest -q test/unit/research test/integration/research
   ```

   Start `2026-09-02T22:57:25.4502905+08:00`; exit `1`; `906 passed`, `6 failed`,
   `0 skipped`, `0 warnings`, `0 subtests`; pytest `342.61s`, wall `345.186s`.
   The command is not green; all six failures are the unchanged preflight-known set
   listed below, with no feature-related failure.

7. Repository-wide suite

   ```powershell
   .venv\Scripts\python.exe -m pytest -q
   ```

   Start `2026-09-02T23:03:19.4597556+08:00`; exit `1`; `2357 passed`, `8 failed`,
   `0 skipped`, `1 warning`, `53 subtests passed`; pytest `569.38s`, wall `572.241s`.
   The command is not green; its eight failures are exactly the six research-known
   failures plus the two preflight-known PowerShell-environment failures below.

8. Security/provenance search

   ```powershell
   rg -n "shell=True|checkout|pip install|uv add|copytree|GIT_TERMINAL_PROMPT|protocol\.file\.allow|--no-checkout|cited_by_count" src/athena/research/prepare src/athena/research/literature/paper_source/openalex.py
   ```

   Start `2026-09-02T23:13:01.5997182+08:00`; exit `0`; wall `0.060s`.
   Matches were limited to required citation fields/checks, prompt suppression,
   `protocol.file.allow=never`, and `--no-checkout`; forbidden execution/install/copy
   patterns were absent.

### Regression repair evidence

The initial Task 7 suite found three failures in legacy PREPARE fixtures. The RCA
demonstrated that their inert EDA/provider setup encoded the retired task-only fallback;
the production hard gate behaved as designed. Commit
`dba438c77541282dd03a8876a54e4b6c35b38bd5` updated only
`test/unit/research/test_breakpoint_resume.py` and
`test/integration/research/test_autonomous_research.py` to seed deterministic EDA and
digest-matched verified artifacts while retaining the real phase runner and
`run_baseline` path. Independent review approved the change with no Critical,
Important, or Minor findings and verified that `src/athena` was unchanged. The fresh
post-repair sequence above supersedes all earlier pass/fail evidence.

## Security/provenance audit

- Git qualification accepts only credential-free public HTTPS URLs, disables terminal
  and credential prompts, denies `file`, `ext`, SSH, Git, and plain HTTP protocols,
  and uses `--depth 1 --filter=blob:none --no-checkout` in a disposable directory.
- The verifier records the reachable `HEAD` commit, sanitizes and bounds diagnostics,
  and removes the temporary directory on success or failure.
- Qualification does not checkout, import, install, copy, or execute third-party
  repository content. Cloneability proves retrievability at the recorded revision—not
  repository safety, scientific correctness, or license compatibility.
- OpenAlex citation metadata is parsed as non-negative, matched to the reported work,
  and checked against the inclusive 100-citation authority threshold.
- The platform-owned verification artifact records route and provenance and remains
  reusable only while its candidate and research digest match.

## Remaining known failures

The suites are not green. These failures are unchanged from the execution preflight
baseline and are unrelated to the authoritative baseline gate.

Research-wide and repository-wide:

- `test/unit/research/test_search_budget_override.py::test_an_explicit_budget_replaces_the_persisted_one`
- `test/integration/research/test_fork_runtime.py::test_both_arms_inherit_one_evaluator_and_start_at_search`
- `test/integration/research/test_human_plan_boundary.py::test_persistent_guidance_is_frozen_into_every_later_plan`
- `test/integration/research/test_human_plan_boundary.py::test_next_guidance_is_frozen_once`
- `test/integration/research/test_human_plan_boundary.py::test_unlimited_waiting_plan_keeps_execution_safety_limits`
- `test/integration/research/test_human_plan_boundary.py::test_budget_extension_resumes_waiting_plan_before_new_plan`

Repository-wide only, caused by PowerShell 7 not being available on this environment's
PATH and fallback to Windows PowerShell 5.1:

- `test/unit/execution/test_encoding.py::test_windows_shell_detection_finds_pwsh_on_path`
- `test/unit/execution/test_encoding.py::test_chain_operator_works_under_powershell7`

The single repository warning was `PytestUnraisableExceptionWarning` reported during
`tests/test_gui_gateway_handler.py::test_set_project_root_keeps_remembered_sessions`:
an asyncio subprocess transport was finalized after its event loop closed. The warning
count is unchanged from preflight.

## Unrelated worktree changes preserved

- The explicitly identified `_DeterministicScriptRunner` work in
  `test/integration/research/test_prepare_agent_contract.py` was never modified,
  formatted, staged, or committed by Task 7. The final Task 7 diff on that path was
  empty.
- All pre-existing untracked `.task3*`, `.task4*`, `.task5*`, and concurrent `.task7*`
  pytest/review directories were left in place and were not staged or removed.
- The documentation closeout commits `36b3b6d` and `6801a8c` contain only the
  plan-authorized closeout paths: the former contains the six documented closeout
  paths, and the latter changes only this report. Neither documentation commit includes
  code, tests, or temporary directories. The separately documented `dba438c` fixture
  repair remains the authorized two-test change.
