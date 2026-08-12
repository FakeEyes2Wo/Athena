# Athena Codex-Inline TUI Design

## Goal

Refine `src/tui.py` into a quiet, readable inline terminal workspace inspired by
Codex CLI while preserving terminal scrollback and all existing Supervisor
behavior.

The change is presentation-focused. It must not alter the
`TASK_CONFIGURE -> RUN` lifecycle, status polling, persisted rollout format,
HumanRequest contract, or slash-command semantics.

## Audience And Success Criteria

The primary user is a researcher running a long-lived Athena workflow from a
terminal. The interface must make three facts immediately visible:

1. What task and project are active.
2. Which research phase is current and whether execution is healthy.
3. What agents are doing, without flooding the terminal with raw protocol data.

The redesign succeeds when users can scan phase changes, distinguish agent
text from tool activity, recover from failure, and retain normal terminal
scrollback on narrow and wide terminals.

## Chosen Direction

Use a Codex-style inline workspace built with the existing Rich dependency.
Do not introduce an alternate-screen application, Textual, or a continuously
redrawn dashboard.

Alternatives considered:

- Cosmetic-only recoloring was rejected because it would leave the current
  log-like information hierarchy unchanged.
- A full-screen Live/Textual dashboard was rejected because it would complicate
  stdin ownership, terminal compatibility, scrollback, and deterministic tests.

## Information Architecture

### Session Header

Render one compact header before task collection. It contains:

- `Athena` as the primary product label.
- `Conversational research workspace` as restrained supporting text.
- The resolved project path as metadata.

Do not use a large banner or decorative ASCII art. The header is a lightweight
orientation block, not a landing screen.

### Task Collection

Use a consistent `›` prompt language for task and dataset input. Keep the
existing default dataset path and empty-task validation. Show one short example
as dim supporting text.

### Execution Start

After configuration and `RUN`, render a single execution summary containing the
short execution ID, interaction mode, and initial phase. Do not print raw
`configured phase=...` and `execution=...` protocol lines.

### Phase Progress

Represent the deterministic lifecycle as an inline track:

`PREPARE  ->  SEARCH  ->  VALIDATE  ->  DONE`

Completed phases are subdued-success, the current phase is emphasized, and
future phases are dim. Beside the track, render execution control status and
state version as secondary metadata. Every distinct `(phase, status, version)`
snapshot is printed once, preserving the existing no-duplicate behavior.

Color is supplemental. Phase labels, status words, and symbols must remain
understandable when color is unavailable.

### Agent Activity

Each persisted rollout message is rendered as an inline activity block:

- Header: `◆ <short-agent-id>  #<message-number>`.
- User/model text: readable wrapped body text with restrained role labels.
- Tool calls: dim `↳ <tool-name> <compact-arguments>` lines.
- Tool results: dim completion lines with errors visibly distinguished.

Folded output remains the default. `/msg <number>` prints the complete message
using the same visual grammar. Existing `athena.agent_messages` parsing remains
the source of message content; `tui.py` owns only terminal composition.

### Commands

Keep `/msg`, `/retry`, `/quit`, and `/help`. Present command feedback using one
consistent vocabulary:

- Success: `✓` plus a concise action result.
- In progress: `●` plus the active state.
- Warning: `!` plus a recoverable condition.
- Failure: `×` plus the reason and next valid command.

The help output is a small aligned command list rather than one semicolon-heavy
line. Unknown commands name `/help` as the recovery path.

### Human Requests

Render HumanRequest as a clearly bounded confirmation block with its question
and available options. Continue using the existing Rich prompt and stdin pause
mechanism. The redesign must not change reply payloads or default-answer
behavior.

### Terminal States

- `FAILED`: show a strong failure line, the available error when present, and
  the `/retry` and `/quit` actions. Continue polling for commands.
- `CANCELLED`: show one terminal cancellation line and exit successfully.
- `COMPLETED`: show one completion line with the final phase track and exit
  successfully.

## Component Boundaries

Keep `src/tui.py` as the entry point, but separate pure rendering decisions from
I/O:

- Theme constants define semantic Rich styles.
- Pure render helpers build header, execution summary, phase track, status
  transition, command help, and terminal-state renderables.
- Existing async functions retain stdin ownership, runtime dispatch, polling,
  and lifecycle control.
- Existing `athena.agent_messages` remains responsible for rollout parsing and
  message-part summarization.

Pure render helpers must accept plain dictionaries or scalar values so they can
be tested without a real terminal or runtime.

## Responsive And Accessibility Behavior

- Rely on Rich wrapping; do not truncate task descriptions or failure reasons.
- Keep fixed-width constructs limited to short phase labels and symbols.
- Avoid tables or panels that become unreadable below 80 columns.
- Never encode status by color alone.
- Respect `Console` color-system fallback and non-interactive captured output.
- Maintain keyboard-only operation; no mouse interaction is introduced.

## Error Handling

Rendering failures must not stop the research execution. Missing fields use
stable fallbacks such as `IDLE`, `-`, or `unknown`. Command dispatch exceptions
remain visible and recoverable. Malformed rollout records continue to be
ignored by the existing message reader.

This work does not repair the Supervisor's current loss of detailed worker
errors. The TUI will render an error if STATUS supplies one, but changing that
backend contract is outside this design.

## Testing Strategy

Use test-driven development in `test/unit/test_tui.py`:

- Header contains product, project, and restrained supporting text.
- Phase track marks past, current, and future phases correctly.
- Status transition contains semantic text and remains useful in plain output.
- Execution start replaces raw protocol lines with a concise summary.
- Agent activity includes message number and short agent identity.
- Help output lists every supported command.
- Failure, retry success, retry rejection, cancellation, completion, and
  HumanRequest retain their existing control behavior.
- Existing task collection, message expansion, polling deduplication, and stdin
  pause tests continue to pass.

Run the focused TUI test module first, then the broader CLI/main tests that share
terminal behavior.

## Scope Boundaries

In scope:

- `src/tui.py` rendering and composition.
- Focused updates to `test/unit/test_tui.py`.
- Small shared message-rendering adjustments only if required for consistent
  activity formatting and covered by its own tests.

Out of scope:

- Supervisor state-machine changes.
- Retry policy or worker-error persistence changes.
- Rollout storage format changes.
- Full-screen or alternate-screen operation.
- New terminal UI dependencies.
- GUI changes under `athena-gui`.
