# Universal `continue` Resume Design

Date: 2026-09-03
Status: approved for implementation; active

## 1. Problem summary

Athena already has a durable phase checkpoint and a canonical `/resume` control, but
plain text submitted through the different user surfaces is routed inconsistently.
After a research failure, the React composer sees neither `running` nor `paused` and
therefore treats the exact text `continue` as a brand-new task. It calls
`task_clarification_start("continue")`; the clarification controller compares that text
with the confirmed draft's original task and correctly raises:

```text
different_task: cancel the active draft before starting another task
```

The resulting optimistic card says that task understanding is running and then failed,
even though the user asked to resume an already-confirmed research run. Other callers
are inconsistent as well:

- TUI input reaches `ResearchRuntime.message("continue")`, which currently treats the
  word as ordinary Supervisor guidance when the lifecycle was previously started.
- Direct Python callers can use `start_task("continue")` or `/resume`, but the former's
  preservation behavior is implicit rather than a declared command contract.
- The CLI exposes `resume` but not the user-facing `continue` synonym.
- Browser WebSocket and native Tauri share the `resume` RPC, but the React composer does
  not choose it after failure and the UI has no authoritative `resume_available` field.

## 2. Product decision

Make the exact, whitespace-trimmed, case-insensitive word `continue` a universal human
alias for Athena's existing resume operation. It resumes the currently persisted task
and current durable phase. It never becomes task text, never creates a clarification
draft, never replaces the confirmed task contract, and never restarts task
understanding.

`/resume` remains the canonical explicit control command. The Chinese `继续` button in
the GUI continues to call that canonical operation. Phrases containing additional text,
such as `continue research`, `continue three more attempts`, or
`please continue with trees`, remain ordinary task/guidance text.

## 3. Scope and caller inventory

| Surface | Current path | Required behavior |
| --- | --- | --- |
| React browser UI | `usePipeline.sendPrompt` -> WebSocket RPC | Exact `continue` calls `resume`, without an optimistic clarification card. |
| React Tauri UI | `usePipeline.sendPrompt` -> native command -> Python RPC | Same behavior and result as browser mode. |
| GUI run controls | `RunControls` -> `resumeSearch` | Continue is enabled for a durable failed/interrupted task, not only `paused`. |
| TUI composer | `TuiController.send_message` -> `ResearchRuntime.message` | Exact `continue` resumes the checkpoint through the runtime command parser. |
| Python CLI | `Athena-cli resume` -> `ResearchRuntime.message("/resume")` | Preserve `resume`; add `continue` as an exact CLI alias. |
| GUI RPC | `message`, `resume`, and legacy `start_task` | All three reach the same runtime resume primitive for an exact continuation request. |
| Direct Python API | `ResearchRuntime.message/start_task` | Exact `continue` has explicit resume semantics and cannot reseed the task. |
| Session/workspace restore | rebuilt `ResearchRuntime` with persisted state | Advertise and resume an interrupted PREPARE/SEARCH/VALIDATE phase. |

The standalone TypeScript `AutoResearchRuntime` and DSH Supervisor are separate state
machines and do not call the Python clarification workflow. They already expose
`AutoResearchRuntime.resume()` and `research_resume`. They are compatibility-audit and
regression-test targets, not implementation targets for this fix. Extending their
independent HTTP demo with a text composer or persisted-run loader would require a
separate design.

## 4. Non-goals

- Do not weaken the `different_task` conflict for actual new task text.
- Do not cancel or rewrite a clarification draft as a side effect of resume.
- Do not reinterpret phrases that merely contain the word `continue`.
- Do not resume a deliberately stopped or completed run.
- Do not add a new research phase or change checkpoint file schemas.
- Do not redesign TypeScript AutoResearch persistence or its HTTP demo.
- Do not add automatic retry loops after a resumed phase fails again.

## 5. Authoritative resume contract

Add a small pure contract module at
`src/athena/research/runtime/resume_contract.py`:

```python
ResumeReason = Literal[
    "already_running",
    "paused",
    "failed",
    "interrupted",
    "no_task",
    "stopped",
    "completed",
]

@dataclass(frozen=True)
class ResumeCapability:
    available: bool
    reason: ResumeReason

def is_continue_command(text: str) -> bool:
    return text.strip().casefold() == "continue"

def resume_capability(state: ResearchState) -> ResumeCapability:
    ...
```

The implementation must classify state deterministically:

| Durable state | Capability | Result of exact `continue` |
| --- | --- | --- |
| `RUNNING` with a confirmed task | `already_running` | Idempotently return `RUNNING`; do not send the word to the Supervisor. |
| `WAITING` with a confirmed/persisted task | `paused` | Resume the current phase. |
| In-process `FAILED` with task evidence | `failed` | Rearm the completed lifecycle task and resume the same phase. |
| Reloaded `IDLE` with task evidence and phase PREPARE/SEARCH/VALIDATE | `interrupted` | Resume the persisted phase; this covers the existing FAILED-to-IDLE load normalization. |
| Fresh `IDLE` without task evidence | `no_task` | Raise typed `resume_unavailable`; do not create a task or draft. |
| `STOPPED` | `stopped` | Raise typed `resume_unavailable`; explicit stop remains terminal. |
| `COMPLETED` or phase `COMPLETED` | `completed` | Raise typed `resume_unavailable`; no completed run is restarted. |

Task evidence is the persisted original task or confirmed understanding:
`state.task_text` or `state.task_understanding`. Legacy confirmed checkpoints that lack
`task_text` remain resumable through their structured understanding. Process-local
lifecycle identity is not an input to the pure capability projection; the mutating
runtime checks it under the resume lock for idempotent `already_running` handling.

Expose one public runtime method:

```python
async def ResearchRuntime.resume_current_task(self) -> str:
    """Resume the current durable task without changing its confirmed contract."""
```

Both `/resume` and plain `continue` call this method. `start_task("continue")` detects
the alias before `seed_unconfirmed_task`, then calls the same method. This ordering is
the key invariant that prevents a resume word from reaching task clarification.

Every runtime owns a process-local `asyncio.Lock` in its lifecycle session. The public
resume method holds that lock across capability classification, terminal-task rearming,
`Supervisor.resume(...)`, and lifecycle start. A FAILED state whose old lifecycle task
is still unwinding waits with `asyncio.gather(..., return_exceptions=True)` before
rearming; the old phase exception is observed but not re-raised from the resume request,
and the task is not mistaken for a live RUNNING task. Concurrent GUI, TUI, CLI, or
Python resume requests therefore observe or create one lifecycle task, never two. The
lock is not persisted and requires no migration.

A PREPARE failure caused by baseline authority outage, generation conflict, or
attestation/evidence mismatch is eligible for an explicit resume attempt. Resume
re-enters the same authoritative PREPARE gate at the same durable phase. It never
regenerates, rewrites, or trusts a local baseline as a substitute for authority. If the
authority condition has recovered, PREPARE continues; if not, the original typed
authority failure is surfaced again. Neither outcome enters clarification or changes
the confirmed task artifacts. Resume itself returns `RUNNING` after starting the
lifecycle; a persistent authority error is observed asynchronously through the
replacement lifecycle task and the normal state/error event, not synchronously from the
resume RPC.

Unavailable resume attempts raise a stable domain error:

```text
resume_unavailable: there is no interrupted task to continue
```

The gateway carries its `code` in the existing structured RPC error data.

## 6. State/event projection

Extend the transient `StateEvent` projection with backward-compatible defaults:

```python
resume_available: bool = False
resume_reason: str | None = None
```

These fields are derived from the same pure `resume_capability` function used by the
runtime. The event model deliberately stores the validated reason as a string so the
Supervisor event layer does not import upward from the runtime package and create a
circular dependency. They are not persisted and require no migration. Frontends must not infer
resumability from plans, attempts, SOTA presence, or a locally remembered `runStarted`
flag once these fields are available.

The state event remains authoritative after session switches and workspace remounts.
Older event producers that omit the fields remain compatible because clients default to
`false` and `null`. Every complete state snapshot resets omitted resume fields to those
defaults; a new or legacy session cannot inherit resumability from the previous session.
Initial hydration applies state only after the target session is selected (or rejects a
stale snapshot with a session-aware epoch). The gateway restore path uses the public
`state_path` property rather than a fake-only `_state_path` attribute.

## 7. UI behavior

### React browser and Tauri

`sendPrompt` checks `isContinueCommand(content)` before its current
running-versus-new-task routing. It calls `resumeSearch()` and never calls
`taskClarificationStart()` for the alias. Browser and native modes continue to share the
same bridge function, so there is no transport-specific behavioral fork.

The UI stores `resumeAvailable` and `resumeReason` from state events. The Continue
button is enabled when the run is paused or when a failed/interrupted task advertises
resume availability. A successful resume marks the local run active while the next
authoritative state event changes the status to running. A rejected resume keeps the
existing preview/history intact and renders the typed error once.

No new screen or layout is required. The only visible change is that an error or
interrupted run offers an enabled Continue action and submitting `continue` no longer
creates a second task-understanding card.

`task_clarification_start` itself remains an explicit low-level clarification RPC; it
does not become a polymorphic resume endpoint. All official free-text entrypoints route
the exact alias before calling it. Direct callers that intentionally invoke the
clarification RPC retain the existing `different_task` protection, while genuine new
task text continues to use that same protection.

### TUI

The TUI remains a thin forwarder. Runtime parsing supplies the behavior, while TUI help
text documents `continue` as the plain-text resume alias. Existing send failure handling
restores the composer text and shows the typed error if nothing can be resumed.

### CLI

`Athena-cli continue --project <path>` is an alias for
`Athena-cli resume --project <path>`. Both issue the canonical runtime resume control.
The existing `resume` spelling and output format remain valid.

## 8. Error handling and safety

- Resume eligibility is checked before any phase, agent, survey, or Git startup.
- A failed resume never changes `task_text`, `task_understanding`,
  `handoff_refs["task_clarification"]`, the clarification draft ID, or its revision.
- Repeated `continue` while already running is idempotent and does not spawn a second
  Supervisor lifecycle task.
- Concurrent resume requests are serialized by the runtime lifecycle resume lock.
- Authority-related PREPARE failures may be retried only through the existing
  authoritative gate; an unchanged outage or mismatch fails again without local
  baseline regeneration.
- A fresh session cannot turn `continue` into a research title.
- An explicit STOP or completed phase cannot be bypassed with the alias.
- `different_task` remains unchanged and continues to protect real new task submissions.
- The explicit lifecycle resume lock is the concurrency boundary for two
  near-simultaneous resume requests.

## 9. Testing strategy

Tests must follow red-green-refactor and cover behavior rather than mock call counts
alone. Required layers are:

1. Pure contract tests for exact matching and every capability reason.
2. Runtime tests proving failed PREPARE, SEARCH, and VALIDATE resume without task
   reseeding, proving repeated/concurrent resume is idempotent, and proving baseline
   authority recovery or repeated failure never bypasses the authoritative gate.
3. Clarification integration tests proving the draft and named handoff are byte-for-byte
   unchanged across resume.
4. Gateway tests for `message`, `resume`, and `start_task` convergence and typed errors.
5. React hook/component tests for browser/native routing, restored-session state,
   enabled controls, no optimistic preview, and error preservation.
6. TUI protocol tests using the real runtime command path.
7. CLI parser and dispatch tests for the new alias and unchanged `resume` command.
8. Existing Python, frontend, Rust protocol, and relevant TypeScript DSH suites as
   regressions.

## 10. Acceptance criteria

1. After PREPARE, SEARCH, or VALIDATE fails, exact `continue` resumes the original task
   at that durable phase in React browser UI, React Tauri UI, TUI, CLI, GUI RPC, and the
   direct Python API.
2. Resume does not invoke `task_clarification_start`, create an intent-preview card,
   regenerate understanding, or change the confirmed clarification artifacts.
3. Exact matching is case-insensitive and whitespace-tolerant; longer prose containing
   `continue` retains its existing meaning.
4. `/resume` remains supported and converges on the same runtime method.
5. A restored failed/interrupted session advertises `resume_available=true` even when
   persisted FAILED state is normalized to in-memory IDLE.
6. The GUI Continue button is usable for paused, failed, and interrupted resumable runs,
   and remains disabled for a genuinely fresh session.
7. Fresh, stopped, and completed sessions reject plain `continue` with
   `resume_unavailable` and do not mutate state.
8. Two simultaneous or repeated continuation attempts create at most one live
   Supervisor lifecycle task.
9. A genuinely different task still raises `different_task` until the active draft is
   explicitly cancelled.
10. Focused Python, React, gateway, TUI, CLI, Rust contract, and TypeScript compatibility
    suites pass with fresh evidence.

## 11. Rollback and migration

No persisted-data migration is required. The event fields are additive and defaulted.
Rollback consists of removing the alias routing, public resume wrapper, event fields,
and UI enablement while leaving all checkpoint and clarification files untouched. The
existing `/resume` RPC and CLI command remain the safe fallback throughout rollout.

## 12. Sequencing constraint

The previously active LLM task-understanding output plan is explicitly paused while this
user-prioritized fix is active. `codex_docs/CURRENT.md` points to the universal-continue
plan, which runs in its own worktree from the latest `main`. Do not run both plans in the
same worktree.

## 13. Open questions

None required for implementation. The requested token is the exact English word
`continue`; adding other natural-language synonyms is intentionally deferred.
