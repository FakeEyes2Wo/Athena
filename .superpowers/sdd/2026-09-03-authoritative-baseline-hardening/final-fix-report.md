# Final Security Fix Wave Report

Date: 2026-09-03 (Asia/Shanghai)

Requested base: `6882eed`

Plan-amendment base used for the authorized breakpoint fixture change: `c18f9e7`

Implementation commit: `df95429` (`fix: harden baseline trust boundaries`)

## Outcome

All seven required final-review findings are fixed. The implementation keeps the
external-authority/no-local-fallback design, exact-byte binding, one-repair lifecycle,
frozen evaluator, and trusted scoring unchanged. No Task 7 completion draft,
`codex_docs/CURRENT.md`, historical report, active plan, or unrelated `.task*` path was
modified or staged.

## Finding-by-finding TDD evidence

The commands below used Python 3.12.11 from the worktree `.venv`. Every required
behavior was driven by an observed RED checkpoint before its production change;
later parameter additions characterize the already-green shared paths.

### 1. Git executable capture and runtime PATH-shim resistance

RED:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare/test_baseline_git_verification.py::test_git_verifier_uses_restricted_shallow_no_checkout_clone test/unit/research/prepare/test_baseline_git_verification.py::test_git_verifier_ignores_runtime_path_git_shim_with_real_subprocess --basetemp .final-fix-git-path-red
```

Observed: `2 failed in 2.71s`; Git argv still began with bare `git`. The real
subprocess probe was strengthened to place a copied shell under the name `git.exe`
(or `git` on POSIX) in both writable cwd and runtime `PATH`, with a sentinel-writing
script. The strengthened probe independently remained RED: `1 failed in 2.78s`
because the sentinel was created.

GREEN:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare/test_baseline_git_verification.py::test_git_verifier_uses_restricted_shallow_no_checkout_clone test/unit/research/prepare/test_baseline_git_verification.py::test_git_verifier_ignores_runtime_path_git_shim_with_real_subprocess --basetemp .final-fix-git-path-green
```

Observed: `2 passed in 2.38s`. The module now captures one resolved absolute Git
executable at import time. Both clone and rev-parse use the identical captured path;
the runtime shim sentinel is not created.

### 2. Multicast and non-public literal handling shared by live and cached proofs

RED:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare/test_baseline_git_verification.py test/unit/research/prepare/test_baseline_source_verification.py --basetemp .final-fix-review-pytest
```

Observed: `11 failed, 63 passed in 3.22s`. IPv4 `224.0.0.1`, IPv6 `ff0e::1`,
IPv4-mapped multicast, IPv4-compatible multicast, and site-local cases exposed the
`ipaddress.is_global` ambiguity; cached proof validation also accepted the multicast
forms.

GREEN:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare/test_baseline_git_verification.py test/unit/research/prepare/test_baseline_source_verification.py --basetemp .final-fix-review-pytest2
```

Observed at this TDD checkpoint: `74 passed in 2.29s`. A single private classifier now
checks both the original address and its mapped/compatible IPv4 routing identity,
requires global routability, and explicitly rejects multicast and site-local values.
The URL model used for cached proofs and the live Git verifier both use that
canonicalizer. Later DNS-response variants of the same four multicast forms are also
covered in the final suite and never reach the runner.

### 3. Safe authority errors and agent-readable supervisor output

RED:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare/test_baseline_research_orchestration.py::test_authority_load_error_is_stable_and_suppresses_adapter_details test/unit/research/prepare/test_baseline_research_orchestration.py::test_authority_seal_error_is_stable_and_suppresses_adapter_details test/unit/research/prepare/test_baseline_research_orchestration.py::test_invalid_authority_lifecycle_fails_before_verifier_or_agent test/unit/research/prepare/test_baseline_research_orchestration.py::test_run_baseline_wraps_completion_attestation_outage test/unit/research/supervisor/test_supervisor.py::test_prepare_authority_failure_does_not_publish_cause_traceback --basetemp .final-fix-authority-red
```

Observed: `7 failed in 3.12s` (the lifecycle test has three parameters). Adapter
messages and chained causes remained observable, invalid generations continued to a
side effect, and the Supervisor published the secret-bearing traceback.

GREEN:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare/test_baseline_research_orchestration.py::test_authority_load_error_is_stable_and_suppresses_adapter_details test/unit/research/prepare/test_baseline_research_orchestration.py::test_authority_seal_error_is_stable_and_suppresses_adapter_details test/unit/research/prepare/test_baseline_research_orchestration.py::test_authority_load_cancellation_is_not_translated test/unit/research/prepare/test_baseline_research_orchestration.py::test_invalid_authority_lifecycle_fails_before_verifier_or_agent test/unit/research/prepare/test_baseline_research_orchestration.py::test_run_baseline_wraps_completion_attestation_outage test/unit/research/prepare/test_baseline_research_orchestration.py::test_authority_attestation_cancellation_is_not_translated test/unit/research/supervisor/test_supervisor.py::test_prepare_authority_failure_does_not_publish_cause_traceback test/unit/research/supervisor/test_supervisor.py::test_phase_actions_preserves_historical_positional_argument_order test/unit/research/supervisor/test_supervisor.py::test_prepare_resume_callback_type_alias_is_module_private --basetemp .final-fix-authority-green
```

Observed: `11 passed in 2.27s`. Load, seal, and attestation calls now translate every
ordinary adapter exception, including an adapter-provided `BaselineAuthorityError`,
to fixed typed messages using `from None`; `asyncio.CancelledError` propagates. For an
authority failure, agent-readable output and persisted session/state contain only the
fixed Supervisor message and no exception/cause traceback or secret marker. Detailed
controller logging remains outside that projection.

### 4. Credential-free Git environment and explicit transport policy

RED:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare/test_baseline_git_verification.py::test_git_verifier_isolates_git_environment_and_config_for_both_commands test/unit/research/prepare/test_baseline_git_verification.py::test_git_verifier_does_not_expose_tls_environment_in_diagnostic --basetemp .final-fix-review-pytest
```

Observed: `2 failed in 2.53s`; the copied ambient environment reached the runner,
including controlled TLS/trust markers.

GREEN:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare/test_baseline_git_verification.py::test_git_verifier_isolates_git_environment_and_config_for_both_commands test/unit/research/prepare/test_baseline_git_verification.py::test_git_verifier_does_not_expose_tls_environment_in_diagnostic --basetemp .final-fix-review-pytest2
```

Observed: `2 passed in 2.07s`. The child environment is now constructed from a fixed
allowlist (only captured Windows process roots when present, fixed locale, disabled
system/global Git config, and non-interactive settings). It never copies runtime
`PATH`, Git config/trace/askpass variables, TLS certificate/key/CA overrides, curl CA
bundles, or proxy variables. Both commands explicitly deny alternate protocols,
hooks, credential helpers, redirects, cookies, extra headers, client certificates,
client keys, and proxies, while requiring TLS verification.

### 5. DNS all-address validation, stable resolution, pinning, and no redirects

RED:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare/test_baseline_git_verification.py::test_repository_dns_resolver_collects_all_a_and_aaaa_deterministically test/unit/research/prepare/test_baseline_git_verification.py::test_git_verifier_rejects_mixed_public_private_dns_before_git test/unit/research/prepare/test_baseline_git_verification.py::test_git_verifier_rejects_dns_failure_before_git test/unit/research/prepare/test_baseline_git_verification.py::test_git_verifier_pins_all_addresses_and_disables_redirects --basetemp .final-fix-review-pytest
```

Observed: `4 failed in 2.52s`; the resolver helper and libcurl pin did not exist, and
mixed/failing DNS still allowed a Git invocation.

GREEN:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare/test_baseline_git_verification.py::test_repository_dns_resolver_collects_all_a_and_aaaa_deterministically test/unit/research/prepare/test_baseline_git_verification.py::test_git_verifier_rejects_mixed_public_private_dns_before_git test/unit/research/prepare/test_baseline_git_verification.py::test_git_verifier_rejects_dns_failure_before_git test/unit/research/prepare/test_baseline_git_verification.py::test_git_verifier_pins_all_addresses_and_disables_redirects --basetemp .final-fix-review-pytest2
```

Observed: `4 passed in 2.04s`. A separate change-of-answer case was then driven RED
(`1 failed in 2.67s`) and GREEN (`1 passed in 2.08s`). The resolver now requests all
A/AAAA stream/TCP answers, rejects the entire set if any answer is non-public or
multicast, sorts/deduplicates deterministically, compares two pre-connect snapshots,
and aborts before Git on failure, mixed answers, or change. The accepted set is pinned
with `http.curloptResolve` using the canonical URL's effective port; IPv6 addresses in
the value are bracketed. Literal IP URLs already identify their connection address and
therefore do not need DNS-cache pinning.

### 6. Authority lifecycle validation before all side effects

RED (also included in Finding 3's combined RED command):

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare/test_baseline_research_orchestration.py::test_invalid_authority_lifecycle_fails_before_verifier_or_agent --basetemp .final-fix-authority-red
```

Observed within that checkpoint: all three parameters failed. They supply generation
0 with an attestation, generation 1 without an attestation, and generation 2 with an
attestation.

GREEN (also included in Finding 3's combined GREEN command):

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare/test_baseline_research_orchestration.py::test_invalid_authority_lifecycle_fails_before_verifier_or_agent --basetemp .final-fix-authority-green
```

Observed within that checkpoint: all three parameters passed. Verification, agent
handoff, scoring, mirror writing, and authority sealing are asserted untouched on
rejection. Generation 0/no-attestation and generation 1 with digest-matching
attestation are the only accepted states; the existing generation 1/no-tree exact
replay remains idempotent.

The stricter lifecycle intentionally changed one Task 5 fixture contract. Before the
authorized fixture migration, this broader root slice reported `1 failed, 45 passed in
4.36s` at the `wrong_generation` branch. After plan amendment `c18f9e7`, the branch was
updated to expect typed fail-closed behavior:

```powershell
.venv\Scripts\python.exe -m pytest -q "test/unit/research/test_breakpoint_resume.py::test_fresh_runtime_rejects_missing_or_wrong_authority_state" --basetemp .final-fix-breakpoint-generation-green
```

Observed: `4 passed in 2.86s`.

### 7. `PhaseActions` positional compatibility and private callback type

RED:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/supervisor/test_supervisor.py::test_phase_actions_preserves_historical_positional_argument_order --basetemp .final-fix-review-pytest
```

Observed: `1 failed`; the third historical positional argument bound to the new
callback instead of `validation`. The companion alias test was also RED because the
callback type alias remained public/exported.

GREEN:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/supervisor/test_supervisor.py::test_phase_actions_preserves_historical_positional_argument_order --basetemp .final-fix-review-pytest2
```

Observed: `1 passed in 2.09s`; the combined GREEN command in Finding 3 additionally
proves the alias is absent from the module and `__all__`. The sole internal callback
field is last, after the six historical fields.

## Fresh final verification

Formatting:

```powershell
.venv\Scripts\python.exe -m black --check src/athena/research/prepare/repository_url.py src/athena/research/prepare/source_verification.py src/athena/research/prepare/baseline.py src/athena/research/supervisor/deps.py src/athena/research/supervisor/phases.py test/unit/research/prepare/test_baseline_git_verification.py test/unit/research/prepare/test_baseline_source_verification.py test/unit/research/prepare/test_baseline_research_orchestration.py test/unit/research/supervisor/test_supervisor.py test/unit/research/test_breakpoint_resume.py
```

Result: exit 0, `10 files would be left unchanged`.

Focused owned-file suite:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare/test_baseline_git_verification.py test/unit/research/prepare/test_baseline_source_verification.py test/unit/research/prepare/test_baseline_research_orchestration.py test/unit/research/supervisor/test_supervisor.py test/unit/research/test_breakpoint_resume.py --basetemp .final-fix-focused-final
```

Result: exit 0, `234 passed in 6.58s` (9.13 seconds wall clock).

Complete requested prepare/supervisor/restart slice:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare test/unit/research/supervisor test/unit/research/test_breakpoint_resume.py --basetemp .final-fix-final-unit
```

Result: exit 0, `676 passed in 10.25s` (12.61 seconds wall clock). This is the final
unique automated-test count for this wave; the 234-test focused run is a subset and is
not added to it.

The exact committed implementation was then rerun with a fresh temp root:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/prepare test/unit/research/supervisor test/unit/research/test_breakpoint_resume.py --basetemp .final-fix-postcommit-unit2
```

Post-commit result: exit 0, `676 passed in 64.49s (0:01:04)`. The slower second wall
time did not change any result.

Quality and interface checks:

```powershell
.venv\Scripts\python.exe -m compileall -q src/athena
.venv\Scripts\python.exe -c "import dataclasses; import athena.research.supervisor.deps as d; from athena.research.prepare.authority import BaselineAuthorityStore, SealedBaseline; from athena.research.prepare.baseline_research import BaselineResearch, BaselineVerification; from athena.research.prepare.source_verification import BaselineSourceVerifier; print([f.name for f in dataclasses.fields(d.PhaseActions)]); print('PrepareResumeIsAttested' in getattr(d, '__all__', ()), hasattr(d, 'PrepareResumeIsAttested'))"
.venv\Scripts\python.exe scripts/check_code_style.py src/athena/research/prepare/repository_url.py src/athena/research/prepare/source_verification.py src/athena/research/prepare/baseline.py src/athena/research/supervisor/deps.py src/athena/research/supervisor/phases.py
git diff --check
```

Result: every command exited 0. Interface output was:

```text
['publish', 'prepare', 'validation', 'publish_agent_event', 'on_plan_settled', 'auto_validate', 'prepare_resume_is_attested']
False False
```

The implementation commit's pre-commit hooks also passed mixed-line-ending, trailing
whitespace, EOF, Black, and Athena code-style checks.

## API and attribute budget

- New public or maintainer-configurable parameters in this wave: **0**.
- New class/instance attributes in this wave: **0**.
- `PhaseActions` remains at seven fields; no second callback field was introduced. Its
  six historical positional slots are unchanged, and the existing internal resume
  callback is now seventh.
- The callback type alias is module-private and not exported.
- Git executable/environment captures and DNS helpers are module-private; no runtime,
  config, CLI, or persisted-state knob was added.

## Platform and Git/libcurl behavior

`git version --build-options` on the verification host reported:

```text
git version 2.46.0.windows.1
cpu: x86_64
libcurl: 8.9.0
OpenSSL: OpenSSL 3.2.2 4 Jun 2024
```

The official Git configuration documentation defines
`http.curloptResolve` as `[+]HOST:PORT:ADDRESS[,ADDRESS]`; omitting `+` creates the
permanent libcurl cache entry used here. The official libcurl documentation requires
IPv6 ADDRESS values to be bracketed and documents that the cache entry controls name
resolution for subsequent transfers. Git documents `http.followRedirects=false` as
failing rather than following an HTTP redirect. References:

- <https://git-scm.com/docs/git-config#Documentation/git-config.txt-httpcurloptResolve>
- <https://git-scm.com/docs/git-config#Documentation/git-config.txt-httpfollowRedirects>
- <https://curl.se/libcurl/c/CURLOPT_RESOLVE.html>

Independent local probes with this Git build confirmed that the strict environment
still starts Git and its HTTPS remote helper, the `curloptResolve` value routes the
request to a pinned local endpoint, and `http.followRedirects=false` stops at the
initial 302. Because libcurl 8.9 predates support for an IPv6 literal in the HOST field
of `CURLOPT_RESOLVE` (added in 8.13), literal IPv6 URLs are left as their already-pinned
literal address; DNS hostnames may still pin IPv6 ADDRESS values, which 8.9 supports.

## Explicit pushback and deferred items

- **No concrete/local authority service added.** The approved architecture assigns
  the concrete service and sandbox isolation to the trusted host. Athena's protocol
  and composition seam are sufficient, while the built-in `None` entry remains
  fail-closed. A same-process filesystem/environment adapter would weaken that design.
- **No new guard around `prepare_phase`.** It is a trusted controller-injected
  test/development callable. A caller able to inject arbitrary Python already has
  controller code execution; treating it as an untrusted boundary or adding a runtime
  toggle would not create a meaningful security boundary.
- **Byte/output/disk ceilings and async subprocess cancellation were not expanded.**
  They remain Low defense-in-depth work. This wave preserves the existing bounded
  diagnostic, timeout, temporary-directory cleanup, and cancellation behavior without
  broadening scope.
- **Repeated test memory-authority implementations were not consolidated.** That is a
  test-maintenance refactor with conflict risk and no required security behavior.

## Residual concerns and deployment invariant

- The security invariant is that Athena imports this verifier under a trusted
  controller environment before an arbitrary-shell Agent runs. The captured absolute
  executable prevents later cwd/`PATH` replacement; it is not a cryptographic
  attestation of the controller-installed Git binary.
- Two identical DNS snapshots are intentionally required before Git starts. DNS
  rotation between those calls can cause a safe availability failure; the mandated
  fail-closed behavior takes precedence over availability.
- Equivalent textual forms of accepted global IPv6 literals remain represented as the
  URL parser/canonicalizer emits them. Security classification is shared and strict;
  textual compression beyond that was not part of this finding and existing
  compatibility coverage preserves those valid forms.
- The repository suite asserts the final Git argv/pin deterministically without public
  DNS. The independent real local Git/libcurl probe supplies platform behavior evidence
  but is not an automated public-network test, keeping normal tests hermetic.

No blocking concern remains for this fix wave.
