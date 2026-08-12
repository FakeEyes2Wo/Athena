# Athena TUI Codex-Inspired Beautification TDD

> **Status:** APPROVED DESIGN, WAITING FOR FILE OWNERSHIP
>
> **Execution gate:** Do not start this packet while
> `codex_docs/CURRENT.md` names the active Supervisor plan. At the time of this
> revision, that plan's T13-P2 owns the PREPARE display projection and concurrent
> work also touches `src/athena_tui/`. The coordinator must first complete and
> verify T13-P2, release all overlapping files, then link this packet from
> `codex_docs/CURRENT.md` or copy it into a newly approved implementation plan
> and assign one owner.
>
> **For agentic workers:** Use `test-driven-development` for every task. Run
> each focused test before production edits, retain the expected RED output,
> implement only enough for GREEN, then run the stated regression slice.

**Goal:** Beautify the existing prompt-toolkit TUI in a restrained Codex-like
style and make its output/composer interaction complete: runtime output stays
in an upper pane scrollable by keyboard or mouse wheel, Enter submits,
Shift+Enter inserts a newline when the terminal reports it distinctly, and
Ctrl+J always inserts a newline.

**Mouse-wheel design:**
`codex_docs/2026-08-12-athena-tui-output-wheel-design.md`

**Architecture:** Keep the current `AthenaApp -> TuiController ->
ResearchRuntime` structure. Continue deriving the display exclusively from
`OutputEvent` and `StateEvent`, and continue sending all user text through
`runtime.message(text)`. Changes are limited to local immutable display state,
pure rendering helpers, prompt-toolkit layout/key bindings, user-facing copy,
and their tests. The active Supervisor plan's T13-P2 is a required upstream
baseline: it converts PREPARE's private structured stream into one validated,
human-readable output record before this packet starts. This packet verifies
that baseline but does not reimplement stream semantics in the TUI.

**Tech Stack:** Python 3.11+, prompt-toolkit 3.0.52, dataclasses, pytest,
pytest-asyncio.

## Global Constraints

- Do not modify `ResearchRuntime`, `OutputEvent`, `StateEvent`, event payloads,
  `TuiController.send_message`, subscribe/unsubscribe behavior, or slash-command
  semantics.
- The only runtime input remains `await runtime.message(text)`. The only
  runtime output remains `output` and `state` subscriber records.
- Build on the existing full-screen `prompt_toolkit` application. Do not add
  Rich, Textual, curses, a second event loop, a second input widget, or a new
  terminal dependency.
- Keep the existing top/header, upper output, bottom composer/overlay, and
  footer structure. This is an incremental visual and interaction refinement,
  not a rewrite.
- Use Chinese for operation hints and English for technical state names such
  as `SEARCH`, `RUNNING`, `WAITING`, `COMPLETED`, and `STOPPED`.
- Use color only as reinforcement. Every state remains identifiable by text or
  a symbol in monochrome terminals.
- New runtime output is appended to the upper output pane. It must never be
  inserted into or replace the composer draft.
- One `OutputEvent` is one semantic runtime display record. `apply_output`
  appends it exactly once and never concatenates adjacent records based on
  `source`, `channel`, or `plan`; none of those fields identifies an Agent turn.
- Raw provider token deltas and private structured `PlanDecision` JSON are not
  TUI display records. PREPARE must publish one validated readable reason per
  decision upstream, preserving punctuation exactly and without inserting or
  trimming spaces. Tool completion records remain independently visible.
- When output follows the tail, new records remain visible. When the user has
  scrolled upward, new records do not force the viewport to the tail; instead,
  an unseen-output count is shown until PageDown or wheel-down reaches the tail,
  or End is pressed.
- Mouse-wheel events affect output history only while the pointer is inside the
  upper output window. One wheel notch moves three rendered visual lines;
  wheel input over the composer, overlays, header, or footer has no output
  scrolling effect.
- Enter submits the complete composer text. Shift+Enter inserts `\n` only when
  the terminal supplies a distinct modified-Enter sequence. Ctrl+J always
  inserts `\n` as the portable fallback.
- prompt-toolkit 3.0.52 maps the common xterm Shift+Enter sequence
  `ESC [ 27 ; 2 ; 13 ~` to `Keys.ControlM`, but preserves the raw sequence in
  `KeyPress.data`. The implementation may inspect that raw data locally; it
  must not patch files under `.venv` or mutate prompt-toolkit's global ANSI
  mapping.
- The composer grows from one to six visual rows. Additional draft content
  scrolls inside the composer so the output pane always retains at least three
  rows.
- Help and confirmation replace only the bottom pane. They do not erase or
  obscure the upper output history, and closing them preserves the draft.
- At narrow widths, hide shortcuts and secondary search details before
  truncating the project path. Phase, control status, and errors remain
  visible.
- Preserve unrelated worktree changes. Stage only files owned by the assigned
  TDD task. Never use `git add .` or `git add -A`.

## Owned Files

- Modify: `src/athena_tui/state.py`
- Modify: `src/athena_tui/render.py`
- Modify: `src/athena_tui/app.py`
- Modify: `src/athena_tui/keybindings.py`
- Modify: `src/athena_tui/entrypoint.py` only for user-visible mojibake
- Test: `test/unit/athena_tui/test_state.py`
- Test: `test/unit/athena_tui/test_render.py`
- Test: `test/unit/athena_tui/test_app.py`
- Test: `test/unit/athena_tui/test_keybindings.py`
- Test: `test/unit/athena_tui/test_entrypoint.py`

Explicitly unowned:

- `src/athena_tui/controller.py`
- `src/athena/research/**`
- `src/athena/core/**`
- runtime, event, provider, persistence, and Supervisor tests

The unowned list is a hard file boundary for this packet. Task 0 below is a
read-only prerequisite audit of the active plan's T13-P2 result. If its tests
fail, stop this packet and return the defect to the T13-P2 owner; do not edit an
unowned file to make the beautification packet pass.

## Target Data Flow

The finished TUI keeps one directional data path. No task may add a side
channel:

```text
ResearchRuntime.subscribe(callback)
  -> TuiController._on_runtime_event(kind, payload)
  -> OutputEvent.model_validate(payload) | StateEvent.model_validate(payload)
  -> AthenaApp._on_event(event)
  -> apply_output(state, event) | apply_snapshot(state, event)
  -> immutable TuiState
  -> pure render_* functions
  -> prompt-toolkit FormattedTextControl

TextArea draft
  -> Enter
  -> AthenaApp._submit_composer()
  -> append_user_message(state, draft) for immediate local visibility
  -> TuiController.send_message(text)
  -> ResearchRuntime.message(text)
```

## Upstream Output Atomicity Prerequisite

The malformed display reported during Human acceptance was:

```text
* agent  Baseline
* agent ,
* agent  predictions
* agent ,
* agent  report
* agent ,
* agent  and
* agent  manifest
* agent  are
* agent  all
* agent  present
* agent  and
* agent  validated
* agent .
* agent ","
* agent s
* agent uggest
* agent ions
* agent ":"
* agent []
* agent }
```

The intended visible PREPARE message is one semantic record:

```text
Baseline, predictions, report, and manifest are all present and validated.
```

The trailing `","`, `s`, `uggestions`, `":"`, `[]`, and `}` fragments belong
to PREPARE's private structured `PlanDecision` envelope and must not be shown.
This is not a wrapping or marker-style defect. The established data path is:

```text
provider StreamEvent(kind="text_delta")
  data["delta"]        = one provider fragment
  data["accumulated"]  = exact response accumulated so far
        |
        v
AgentRuntime emits "agent/text_delta"
  event_ref = f"event:{turn_id}"      # stable within one turn
        |
        v
run_prepare_plan._forward_run_events
  previously forwarded every fragment
        |
        v
ResearchRuntime._project_agent_event
  previously created one OutputEvent per fragment
        |
        v
TuiState.apply_output
  correctly treated every OutputEvent as a separate display record
```

Root-cause hypothesis, confirmed by source tracing: the semantic projection
boundary forwarded a serialization stream as though every provider fragment
were a complete Human-readable message. Repeated `agent` markers are the
downstream manifestation, not the cause.

T13-P2 owns the minimal correction before this packet begins:

1. `_forward_run_events` continues forwarding `command/completed` records but
   does not expose PREPARE's raw `agent/text_delta` serialization fragments.
2. After `_decision_from_summary` validates the response as `PlanDecision`,
   `run_prepare_plan` publishes `decision.reason` once through the existing
   `EmitEvent` callback. It does not parse or rebuild JSON with string slicing.
3. The callback still uses the existing internal Agent event path understood by
   `ResearchRuntime._project_agent_event`; no public event kind or payload schema
   is added. Subscribers still receive only `output` and `state`.
4. `EventProjector` normalizes CRLF and bare CR for display. It does not strip
   meaningful spaces, alter punctuation, or modify artifact content.
5. A failed or interrupted PREPARE turn has no validated `PlanDecision`, so its
   malformed partial JSON is not published as prose. Already completed tool
   records remain visible, and normal Supervisor error/state handling reports
   the failure.

The expected private callback shape can remain the existing one:

```python
await publish(
    "agent/text_delta",
    f"event:{run_id}",
    {
        "delta": decision.reason,
        "accumulated": decision.reason,
    },
)
```

This example constrains behavior, not a new public API. If T13-P2 implements an
equivalent call through an existing helper, its tests and the observable
`output`/`state` contract are authoritative.

Do not solve this in `TuiState.apply_output` by joining adjacent records with
matching `(source, plan)`. Two independent turns can share both values, and
`OutputEvent` deliberately has no turn identifier. Such a join corrupts event
boundaries, can join text across intervening lifecycle events, and still leaves
the upstream protocol emitting non-semantic records.

The first path is runtime-to-display only. The second path is user-to-runtime
only. Runtime output can change `history`, `last_output_seq`, and runtime-owned
snapshot fields, but never `composer`. Composer edits can change `composer` and
local history, but never runtime-owned snapshot fields.

## Target Local API

The implementation must converge on these signatures. Private helpers may be
added only when they remove duplication and remain inside `src/athena_tui`.

### `src/athena_tui/state.py`

```python
from dataclasses import dataclass
from typing import Any, Literal

HistoryKind = Literal["user", "runtime"]
OutputSource = Literal["supervisor", "agent", "tool"]
OutputChannel = Literal["text", "stdout", "stderr", "error"]


@dataclass(frozen=True)
class HistoryEntry:
    """One local display record; never serialized or sent to the runtime."""

    kind: HistoryKind
    text: str
    source: OutputSource | None = None
    channel: OutputChannel = "text"
    plan: str | None = None
    tool: str | None = None
    truncated: bool = False


@dataclass(frozen=True)
class TuiState:
    project_root: str = "."
    mode: str = COMPOSER
    status: str = "RUNNING"
    phase: str = "SEARCH"
    plans: tuple[dict[str, Any], ...] = ()
    search: dict[str, Any] | None = None
    sota: dict[str, Any] | None = None
    waiting: dict[str, Any] | None = None
    history: tuple[HistoryEntry, ...] = ()
    last_output_seq: int = 0
    history_follow_tail: bool = True
    unseen_output_count: int = 0
    composer: str = ""
    overlay: str | None = None
    last_error: str | None = None


def apply_snapshot(state: TuiState, event: object) -> TuiState:
    """Replace only runtime-owned fields from one complete StateEvent."""


def apply_output(state: TuiState, event: object) -> TuiState:
    """Append one new sequenced OutputEvent to local history."""


def append_user_message(state: TuiState, text: str) -> TuiState:
    """Append an immediate local echo of one submitted user message."""


def set_history_follow(state: TuiState, follow: bool) -> TuiState:
    """Toggle tail following and clear unseen count when resuming."""


def set_error(state: TuiState, message: str | None) -> TuiState:
    """Replace the transient local error."""


def open_overlay(state: TuiState, text: str) -> TuiState:
    """Open local help while retaining composer/history."""


def close_overlay(state: TuiState) -> TuiState:
    """Close local help while retaining composer/history."""


def open_confirmation(state: TuiState, text: str) -> TuiState:
    """Enter confirmation mode with local prompt text."""


def close_confirmation(state: TuiState) -> TuiState:
    """Return to composer mode without issuing a runtime command."""
```

The signatures above are declarations of responsibility. Task 1 and the
existing module provide the concrete function bodies; no function is committed
with a docstring-only body.

`append_history` is replaced by the more specific `append_user_message`; no
generic string-append API remains. `HistoryEntry(kind="user", ...)` requires
`source is None`. `HistoryEntry(kind="runtime", ...)` requires a non-None
source. Enforce these invariants in `HistoryEntry.__post_init__` with
`ValueError`, and cover both invalid combinations with tests.

### `src/athena_tui/render.py`

Use prompt-toolkit's canonical formatted-text representation:

```python
from prompt_toolkit.formatted_text import StyleAndTextTuples

HELP_TEXT: str

def wrap_fragments(
    fragments: StyleAndTextTuples, width: int
) -> tuple[StyleAndTextTuples, ...]:
    """Wrap styled fragments without losing style or Unicode cell width."""

def render_header(state: TuiState, width: int) -> StyleAndTextTuples:
    """Render one prioritized product/project/phase/status row."""

def render_history_lines(
    state: TuiState, width: int
) -> tuple[StyleAndTextTuples, ...]:
    """Project entries to immutable visual lines used by scrolling."""

def render_history(state: TuiState, width: int) -> StyleAndTextTuples:
    """Join visual history lines for a FormattedTextControl."""

def render_bottom_pane(state: TuiState, width: int) -> StyleAndTextTuples:
    """Render composer hints, help, or confirmation."""

def render_status(state: TuiState, width: int) -> StyleAndTextTuples:
    """Render prioritized error/waiting/unseen/progress status."""

def composer_height(text: str, width: int) -> int:
    """Return the draft's visual height clamped to 1..6 rows."""
```

These are API declarations, not copy-paste function bodies. The Reference
Rendering Blueprint below supplies the algorithms and the task RED tests define
the observable results.

`render_history_lines` is the authoritative visual-line projection and is used
by scrolling. `render_history` joins those lines with newline fragments for
tests and direct controls. `wrap_fragments` must preserve each fragment's style
when wrapping; converting styled fragments to one plain string and then trying
to reconstruct styles is forbidden.

All width-taking renderers obey:

```text
width <= 0    return content without width-based truncation
width >= 1    no emitted visual line exceeds width terminal cells
```

Use `prompt_toolkit.utils.get_cwidth`, not Python `len`, for width accounting.
Never split a Unicode code point. A wide Chinese character consumes two cells.

### `src/athena_tui/keybindings.py`

```python
KeyIntent = tuple[
    Literal[
        "scroll_up",
        "scroll_down",
        "scroll_end",
        "open_overlay",
        "close_overlay",
        "quit",
        "submit",
        "insert_newline",
        "confirm",
        "cancel",
    ],
    object | None,
]


def map_key(mode: str, key: str) -> KeyIntent | None:
    """Map one normalized key token to a local-only intent."""
```

This remains a pure mapping helper. It does not inspect prompt-toolkit events,
buffers, terminal capabilities, runtime state, or controller objects.

### `src/athena_tui/app.py`

Preserve the public constructor and application lifecycle:

```python
class AthenaApp:
    def __init__(
        self,
        runtime: object,
        project_root: object,
        *,
        input=None,
        output=None,
    ) -> None:
        """Attach one controller and initialize immutable local state."""

    async def handle_key(self, token: str) -> None:
        """Apply one normalized local key intent."""

    async def run(self) -> int:
        """Run full screen and always close the owned controller."""
```

Add or retain these private helpers with one responsibility each:

```python
def _render_width(self) -> int:
    """Read terminal columns with a minimum of 20."""

def _render_height(self) -> int:
    """Read terminal rows with the existing 24-row fallback."""

def _history_height(self) -> int:
    """Reserve dynamic composer/footer rows and retain at least 3 history rows."""

def _history_line_count(self, width: int | None = None) -> int:
    """Count wrapped history lines at the supplied or current width."""

def _clamp_history_scroll(self) -> None:
    """Clamp visual-line scroll offset without changing follow mode."""

def _history_fragments(self) -> StyleAndTextTuples:
    """Return the current visual-line viewport."""

def _composer_height(self) -> int:
    """Delegate current draft and width to render.composer_height."""

def _insert_newline(self) -> None:
    """Insert a newline at the current composer cursor."""

def _is_shift_enter(self, event: KeyPressEvent) -> bool:
    """Recognize raw modified-Enter sequences preserved by prompt-toolkit."""

def _bottom_container(self):
    """Select composer, help, or confirmation for DynamicContainer."""

def _build(self) -> Application:
    """Construct the one-column prompt-toolkit application."""

def _key_bindings(self) -> KeyBindings:
    """Construct local navigation, composer, overlay, and confirmation keys."""

async def _submit_composer(self) -> None:
    """Echo and send one exact draft or execute local slash-command behavior."""

async def _request_quit(self) -> None:
    """Exit terminal states or request local confirmation."""

async def _apply_confirmation(self) -> None:
    """Apply the selected stop/quit action once."""
```

These method declarations document ownership and signatures. The methods must
have the concrete bodies specified below when implemented.

`_insert_newline` inserts at `buffer.cursor_position`, not at the end of the
draft. `_submit_composer` restores both text and cursor position after send
failure by assigning a `prompt_toolkit.document.Document`; assigning only
`TextArea.text` is insufficient because its setter resets the cursor to zero.

## State Transition Contract

| Current local state | Input/event | Required next state | Runtime call |
| --- | --- | --- | --- |
| COMPOSER, following tail | OutputEvent with new seq | append runtime entry; unseen remains 0 | none |
| COMPOSER, scrolled | OutputEvent with new seq | append runtime entry; unseen +1; remain scrolled | none |
| any | duplicate/older OutputEvent seq | exact same object or equal state | none |
| any | StateEvent | replace status/phase/plans/search/sota/waiting only | none |
| COMPOSER | ordinary Enter, nonblank draft | re-enable tail follow; append exact user entry; clear composer; clear local error | `message(exact draft)` once |
| COMPOSER | ordinary Enter, blank/whitespace draft | unchanged | none |
| COMPOSER | Shift+Enter recognized | insert newline at cursor | none |
| COMPOSER | Ctrl+J | insert newline at cursor | none |
| COMPOSER, empty draft | `?` | open help overlay | none |
| COMPOSER, nonempty draft | `?` | insert literal `?` through normal editor path | none |
| overlay open | Esc | close overlay; preserve draft/history | none |
| COMPOSER | `/stop` + Enter | clear command draft; open stop confirmation | none |
| CONFIRMATION(stop) | Y | close confirmation | `message("/stop")` once |
| CONFIRMATION(any) | N or Esc | close confirmation; preserve prior draft/history | none |
| RUNNING/WAITING | Ctrl+C | open quit confirmation | none |
| STOPPED/COMPLETED | Ctrl+C | exit TUI | none |
| CONFIRMATION(quit) | Y | exit TUI; runtime execution continues | none |
| any send failure | exception | restore exact draft/cursor; set readable local error | no retry |

`StateEvent` replacement must not clear `history`, `last_output_seq`,
`history_follow_tail`, `unseen_output_count`, `composer`, `overlay`, or
`last_error`. These are local display fields.

For submission, whitespace has two meanings:

```python
draft = self._composer.text          # exact payload and local history text
command = draft.strip()              # blank check and slash-command recognition
```

If `command` is empty, do nothing and preserve `draft`. If `command` equals a
recognized slash command, use command semantics. Otherwise append and send the
exact `draft`, including embedded newlines and intentional leading/trailing
spaces. Submitting a message returns the history viewport to the tail because
the newly added user entry must be visible immediately.

## Layout Contract

The prompt-toolkit container tree must stay one column:

```text
Application(full_screen=True)
└── Layout(initial_focus=composer)
    └── HSplit
        ├── Window(header, height=1)
        ├── Window(history, flexible, minimum=3)
        ├── DynamicContainer(bottom)
        │   ├── composer mode
        │   │   └── HSplit
        │   │       ├── Frame(TextArea, height=1..6)
        │   │       └── Window(composer hints, height=1 when space permits)
        │   ├── help overlay
        │   │   └── Window(help, bounded height)
        │   └── confirmation
        │       └── Window(prompt + Y/N/Esc choices, bounded height)
        └── Window(status, height=1)
```

The fixed-row budget is computed, not frozen at the old `_FIXED_ROWS = 7`.
`Frame` contributes one top and one bottom border row:

```python
composer_frame_rows = composer_height + 2
fixed = 1 + composer_frame_rows + composer_hint_rows + status_rows
history_height = max(3, terminal_rows - fixed)
```

When help or confirmation is open, use its actual preferred/bounded height in
place of composer rows. The output history must retain at least three rows.
When the entire terminal is shorter than the sum, prompt-toolkit may compress
secondary help/hints first; it must not create a second page or hide history
behind a float.

## Message Rendering Contract

Render each entry using semantic fragments. Literal examples below describe
plain output; styles are separate:

```text
user, first line          › try random forest
user, continuation          with class weights
supervisor/agent, first    ● Testing the next hypothesis
same source continuation    Cross-validation is running
tool stdout                tool · python  accuracy=0.8421
tool stderr                tool · stderr  warning text
error                      ! training failed
truncated tool output      tool · stdout  preview text  [已截断]
```

Rules:

1. User entries always begin a new visual block with `› `.
2. Supervisor and agent text use `● ` on the first logical line.
3. A directly adjacent runtime text entry with the same source suppresses the
   repeated `●` and uses two spaces.
4. Tool entries display `entry.tool` when present; otherwise display `tool`.
5. `channel="error"` uses `! ` and error style regardless of source.
6. `stderr` uses warning/error-adjacent styling but retains the literal
   `stderr` label; it is not silently converted to a generic text message.
7. Embedded newlines are preserved. Continuation lines align beneath the
   first line's body, not beneath its marker.
8. Wrapping occurs after prefixes are chosen. Continuation wrapping uses the
   same body indentation.
9. Dynamic text is plain formatted-text content, never parsed as HTML/ANSI or
   prompt-toolkit style markup.
10. `artifact_ref` is not opened by the TUI. A truncated marker may be shown
    from `OutputEvent.truncated`; no file or artifact read is allowed.

## Responsive Rendering Matrix

| Width | Header | Status | Composer hint |
| --- | --- | --- | --- |
| >= 88 | Athena + full project path + phase + status | attempts, successes, workers, SOTA | Enter, Shift+Enter, Ctrl+J, help |
| 72..87 | Athena + middle-truncated path + phase + status | attempts, successes, workers, SOTA as space allows | Enter, Shift+Enter, Ctrl+J |
| 48..71 | Athena + project basename + phase + status | attempts and SOTA | Enter and Ctrl+J |
| 32..47 | Athena + phase + status | phase + status or prioritized error/waiting/unseen | shortest valid hint |
| 20..31 | Athena + phase/status compact form | prioritized error/waiting/unseen, hard cell truncation | omit hints before input |

The application already clamps `_render_width()` to at least 20; keep that
behavior. Middle truncation uses one Unicode ellipsis only when the remaining
width can still contain both ends. Tests derive expected cell widths with
`get_cwidth` and assert no visual line overflow.

## Keyboard Normalization Contract

prompt-toolkit receives ordinary Enter as `Keys.ControlM` with raw data `\r`.
For common xterm modified Enter, version 3.0.52 also emits `Keys.ControlM`, but
the `KeyPress.data` field remains the complete raw sequence. Therefore:

```python
_SHIFT_ENTER_SEQUENCES = frozenset(
    {
        "\x1b[27;2;13~",  # xterm modifyOtherKeys: Shift+Enter
    }
)


def _is_shift_enter(self, event: KeyPressEvent) -> bool:
    return event.data in _SHIFT_ENTER_SEQUENCES
```

The composer-mode Enter binding must be `eager=True` so the multiline
TextArea's default Enter insertion cannot win. Its handler is:

```python
async def handle_enter(event: KeyPressEvent) -> None:
    if self._is_shift_enter(event):
        self._insert_newline()
    else:
        await self.handle_key("enter")
```

Bind `c-j` eagerly to `_insert_newline`. Bind these only when:

```python
composer_active = Condition(
    lambda: self.state.mode == COMPOSER and self.state.overlay is None
)
```

Bind `?` to help only when the composer is active and its buffer is empty. If
the buffer is nonempty, do not intercept `?`; TextArea inserts it normally.
Confirmation bindings have precedence while `mode == CONFIRMATION`, and no
confirmation key may mutate the composer buffer.

Do not add guessed Kitty keyboard protocol sequences without a pipe-input test
that prompt-toolkit 3.0.52 parses into the intended event. The guaranteed
cross-terminal newline remains Ctrl+J.

## Error And Recovery Contract

- Empty input: no history entry, no runtime call, no error.
- Failed ordinary message: keep the attempted user history entry, restore the
  exact draft and cursor, set `发送失败：<exception>`, and do not retry.
- Failed `/stop`: close confirmation, set `停止失败：<exception>`, and do not
  exit.
- Unknown runtime event kinds remain rejected by `TuiController`; no visual
  fallback weakens validation.
- Unknown objects passed to `apply_event` remain ignored for compatibility;
  validated controller flow still emits only OutputEvent/StateEvent.
- Missing search/SOTA/waiting fields render by omission. Never synthesize a
  metric, success, worker, attempt, or waiting reason.
- Rendering functions are pure and must not raise for empty history, blank
  text, width 0, missing optional state dictionaries, or Unicode content.

## Complete Behavior Matrix

The following matrix is the minimum automated coverage. A row may share setup
with another row, but each named behavior must have an assertion that would
fail for the listed realistic regression.

| Area | Case | Required assertion | Regression caught |
| --- | --- | --- | --- |
| state | new OutputEvent | structured runtime entry + seq update | output discarded or flattened |
| state | duplicate OutputEvent | state equality and unseen unchanged | replay duplicated in history |
| state | adjacent Agent events with same plan | two `HistoryEntry` values | independent turns silently concatenated |
| state | StateEvent | runtime fields replaced, local fields retained | snapshot erases draft/history |
| state | user append | user entry, no fake source | user rendered as Agent |
| state | scrolled output | unseen increments | user misses background output |
| state | tail resume | unseen resets | stale new-output badge |
| history | user multiline | one `›`, aligned continuation | marker repeated per line |
| history | same-source adjacent text | second marker suppressed | noisy output stream |
| history | source changes | new marker restored | speakers become ambiguous |
| history | stdout/stderr/error | labels and semantic styles differ | failures look like prose |
| history | truncated | `[已截断]`, no artifact read | hidden truncation or file polling |
| history | Chinese wrap | every line <= width cells | `len()` breaks CJK layout |
| header | >=88 columns | full path + phase + status | wide layout wastes information |
| header | 40 columns | Athena + phase + status, no overflow | path hides operational state |
| status | normal wide | attempts/successes/workers/SOTA | research progress absent |
| status | missing optional fields | absent, not fake zero/default | misleading metrics |
| status | waiting | waiting reason wins | action request hidden |
| status | local error | error wins | failure hidden by progress |
| status | scrolled | unseen count and End hint | new output not discoverable |
| composer | blank Enter | no send, draft preserved | empty runtime turn |
| composer | exact text | exact whitespace/newlines sent once | payload silently stripped |
| composer | plain Enter | submit | multiline widget captures Enter |
| composer | xterm Shift+Enter | newline at cursor, no send | modified Enter submits early |
| composer | Ctrl+J | newline at cursor, no send | fallback missing |
| composer | >6 rows | height exactly 6 | output pane squeezed away |
| composer | failed send | exact text + cursor restored | user loses editing position |
| help | `?` on empty draft | overlay opens | help inaccessible |
| help | `?` in nonempty draft | literal question mark inserted | normal punctuation intercepted |
| help | Esc | overlay closes, draft survives | help destroys input |
| confirmation | `/stop` | no runtime call before Y | destructive command fires early |
| confirmation | N/Esc | close, no command | cancel leaks action |
| confirmation | Y | one `/stop` message | duplicate stop |
| quit | running Ctrl+C | confirmation | accidental TUI exit |
| quit | completed Ctrl+C | immediate TUI exit | needless confirmation |
| protocol | output/state only | existing integration tests pass | UI adds polling/event type |
| upstream projection | PREPARE chunked JSON | one readable reason output | token/JSON fragments get one marker each |
| upstream projection | exact punctuation | literal validated reason unchanged | spaces inserted around punctuation |
| upstream projection | private decision fields | `decision`/`suggestions` JSON absent | internal envelope leaks into history |
| upstream projection | completed tool event | separate tool output retained | readable-reason fix hides useful tools |
| upstream projection | failed/interrupted turn | no partial JSON prose | malformed partial response shown as answer |
| protocol | message only | controller test passes | UI calls dispatch/API variant |
| lifecycle | run exit | unsubscribe + runtime close | leaked subscription/runtime |

## Style API

Define one immutable module-level prompt-toolkit style in `app.py`. Renderer
fragments reference semantic class names; they never embed color hex values:

```python
_STYLE = Style.from_dict(
    {
        "header": "",
        "header.brand": "bold",
        "header.path": "#8b949e",
        "phase.running": "#2dd4bf bold",
        "phase.waiting": "#fbbf24 bold",
        "phase.done": "#22c55e bold",
        "phase.stopped": "#f87171 bold",
        "history.user.marker": "#7dd3fc bold",
        "history.user": "",
        "history.agent.marker": "#2dd4bf bold",
        "history.agent": "",
        "history.tool": "#8b949e",
        "history.error": "#f87171",
        "composer": "",
        "composer.prompt": "#7dd3fc bold",
        "frame.border": "#475569",
        "frame.label": "#8b949e",
        "status.primary": "",
        "status.secondary": "#8b949e",
        "status.warning": "#fbbf24",
        "status.error": "#f87171",
        "status.unseen": "#7dd3fc",
        "help.heading": "bold",
        "help.key": "#7dd3fc",
        "confirmation": "#fbbf24",
    }
)
```

The root, header, history, composer, and status classes intentionally omit
background colors. Focused composer borders may use a dynamic style callable:

```python
style=lambda: (
    "class:frame.border class:frame.border.focused"
    if self._app is not None
    and self._app.layout.has_focus(self._composer)
    else "class:frame.border"
)
```

If prompt-toolkit's `Frame` does not expose a supported dynamic border-style
surface, keep the stable border color; do not reach into private children to
force focus styling. Add `frame.border.focused: "#94a3b8"` only when a public
container-level style can apply it.

## Reference Rendering Blueprint

This section defines the implementation shape for the pure renderer. Minor
name changes are acceptable only if every task and test is updated together.

### Fragment types and cell helpers

The TUI emits only two-item style/text tuples. Mouse-handler tuples are not
needed:

```python
from collections.abc import Iterable
from pathlib import Path

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.utils import get_cwidth

Fragment = tuple[str, str]
Fragments = list[Fragment]


def _cell_width(text: str) -> int:
    return sum(max(0, get_cwidth(char)) for char in text)


def _plain(fragments: Iterable[Fragment]) -> str:
    return "".join(text for _style, text in fragments)
```

The application supplies width >= 20. Pure helper behavior at width <= 0 is
unbounded. At width 1, a two-cell glyph is emitted intact on its own line; this
is the only mathematically unavoidable exception to the no-overflow rule.
Automated responsive acceptance uses widths >= 20.

### Styled wrapping

Use one low-level consumer that removes at most `limit` terminal cells from
the front of styled fragments while preserving style:

```python
def _take_cells(
    fragments: list[Fragment], limit: int
) -> tuple[Fragments, list[Fragment]]:
    taken: Fragments = []
    remaining: Fragments = []
    used = 0
    stopped = False

    for style, text in fragments:
        if stopped:
            remaining.append((style, text))
            continue

        split_at = 0
        for index, char in enumerate(text):
            cells = max(0, get_cwidth(char))
            if used and used + cells > limit:
                stopped = True
                break
            if not used and cells > limit:
                # Preserve an indivisible wide glyph rather than corrupt it.
                split_at = index + 1
                used += cells
                stopped = True
                break
            split_at = index + 1
            used += cells
            if used >= limit:
                stopped = True
                break

        if split_at:
            taken.append((style, text[:split_at]))
        if split_at < len(text):
            remaining.append((style, text[split_at:]))

    return taken, remaining
```

Before calling `_take_cells`, split explicit `\n` boundaries into logical
fragment lines. Do not pass newline characters through cell wrapping. For each
logical line, wrap with a first-line prefix and a continuation prefix:

```python
def _wrap_block(
    first_prefix: Fragments,
    continuation_prefix: Fragments,
    body: Fragments,
    width: int,
) -> tuple[StyleAndTextTuples, ...]:
    logical_lines: list[Fragments] = [[]]
    for style, text in body:
        pieces = text.split("\n")
        for index, piece in enumerate(pieces):
            if piece:
                logical_lines[-1].append((style, piece))
            if index < len(pieces) - 1:
                logical_lines.append([])

    output: list[StyleAndTextTuples] = []
    first_visual_line = True
    unbounded = width <= 0

    for logical_line in logical_lines:
        remaining = logical_line
        emitted_for_logical_line = False
        while remaining or not emitted_for_logical_line:
            prefix = first_prefix if first_visual_line else continuation_prefix
            if unbounded:
                chunk, remaining = remaining, []
            else:
                prefix_width = _cell_width(_plain(prefix))
                available = max(1, width - prefix_width)
                chunk, remaining = _take_cells(remaining, available)

            output.append([*prefix, *chunk])
            first_visual_line = False
            emitted_for_logical_line = True

    return tuple(output)
```

The implementation must not word-wrap by `textwrap.wrap`: it counts code
points rather than terminal cells, may collapse whitespace, and cannot retain
fragment styles. Long unbroken paths, hashes, trace fragments, Chinese text,
and ordinary prose all use the same cell-safe algorithm.

### History entry projection

Use a pure prefix/body selector:

```python
def _history_block(
    entry: HistoryEntry,
    previous: HistoryEntry | None,
) -> tuple[Fragments, Fragments, Fragments]:
    if entry.kind == "user":
        return (
            [("class:history.user.marker", "› ")],
            [("class:history.user.marker", "  ")],
            [("class:history.user", entry.text)],
        )

    if entry.channel == "error":
        return (
            [("class:history.error", "! ")],
            [("class:history.error", "  ")],
            [("class:history.error", entry.text)],
        )

    if entry.source == "tool":
        label = entry.tool or "tool"
        prefix_text = f"{label} · {entry.channel}  "
        suffix = "  [已截断]" if entry.truncated else ""
        return (
            [("class:history.tool", prefix_text)],
            [("class:history.tool", " " * _cell_width(prefix_text))],
            [("class:history.tool", entry.text + suffix)],
        )

    repeat = (
        previous is not None
        and previous.kind == "runtime"
        and previous.source == entry.source
        and previous.channel == "text"
        and entry.channel == "text"
    )
    marker = "  " if repeat else "● "
    return (
        [("class:history.agent.marker", marker)],
        [("class:history.agent.marker", "  ")],
        [("class:history.agent", entry.text)],
    )
```

`render_history_lines` iterates in order, calls `_history_block`, wraps each
block, and inserts one blank visual line only between a user block and the next
runtime response when terminal height allows. The default implementation
should omit blank separator lines to maximize information density; do not add
them unless a focused visual acceptance demonstrates a readability problem.

### Header truncation

Use cell-safe right and middle truncation helpers:

```python
def _truncate_right(text: str, width: int) -> str:
    if width <= 0 or _cell_width(text) <= width:
        return text
    if width == 1:
        return "…"
    head, _rest = _take_cells([("", text)], width - 1)
    return _plain(head) + "…"


def _truncate_middle(text: str, width: int) -> str:
    if width <= 0 or _cell_width(text) <= width:
        return text
    if width <= 3:
        return _truncate_right(text, width)
    left_cells = (width - 1) // 2
    right_cells = width - 1 - left_cells
    left, _ = _take_cells([("", text)], left_cells)
    reversed_right, _ = _take_cells([("", text[::-1])], right_cells)
    return _plain(left) + "…" + _plain(reversed_right)[::-1]
```

If reverse-by-code-point proves unsafe for a combining-character fixture,
replace it with a grapheme-aware local helper using only the standard library
and prompt-toolkit; do not add a dependency solely for truncation. Add the
fixture before changing the implementation.

Header assembly reserves cells from right to left:

```text
mandatory right: "SEARCH · RUNNING"
mandatory left:  "Athena"
optional middle: project path
separators:      two spaces
```

At width >= 88, render the full path if it fits. At 48..87, middle-truncate it.
Below 48, use `Path(project_root).name`; if that still does not fit, omit the
project before truncating mandatory phase/status. Unknown statuses use
`status.primary`, not an invented semantic color.

### Status assembly

Build status fragments from ordered tokens, measuring after every optional
append. Never create one long string and cut through a semantic token:

```python
def _append_if_fits(
    output: Fragments,
    token: Fragment,
    width: int,
    *,
    separator: Fragment = ("class:status.secondary", "  "),
) -> bool:
    candidate = [*output, *([separator] if output else []), token]
    if width > 0 and _cell_width(_plain(candidate)) > width:
        return False
    output[:] = candidate
    return True
```

Priority branches are mutually exclusive:

```python
if state.last_error:
    return error_line(state.last_error, width)
if state.status == "WAITING" and state.waiting:
    return waiting_line(state.waiting, width)
if not state.history_follow_tail and state.unseen_output_count:
    return unseen_line(state.unseen_output_count, width)
return progress_line(state, width)
```

`waiting["reason"]` may be a machine code such as `turn_limit_exhausted`.
Map only known display codes locally:

```python
_WAITING_REASON_LABELS = {
    "turn_limit_exhausted": "计划轮次已用尽，需要指导",
}
```

Unknown reasons are displayed verbatim. The TUI does not reinterpret or send
them back to the runtime.

Progress token construction uses existing keys exactly:

```python
search = state.search or {}
attempts = search.get("attempts")
limit = search.get("limit")
successes = search.get("successes")
workers = search.get("concurrency")
metric = (state.sota or {}).get("metric")
```

Only render a token when all values it needs are non-None. Do not use truthy
checks because zero attempts, zero successes, and metric `0.0` are valid. SOTA
formatting is deterministic:

```python
def _format_metric(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return f"{float(value):.4f}".rstrip("0").rstrip(".")
```

The ordinary first token is always `<phase> · <status>`. Wide optional tokens
are `attempts/limit attempts`, `successes successful`, `concurrency workers`,
and `SOTA metric`, in that order. Medium width may skip successes/workers to
retain SOTA.

### Composer height

Composer height counts visual rows, including explicit blank lines:

```python
def composer_height(text: str, width: int) -> int:
    body_width = max(1, width - 4)  # frame borders + "› " prompt
    rows = 0
    for logical_line in text.split("\n"):
        cells = _cell_width(logical_line)
        rows += max(1, (cells + body_width - 1) // body_width)
    return max(1, min(6, rows))
```

If actual Frame/prompt measurements differ during a real 40-column test,
adjust the subtraction constant through a focused failing layout test. Never
let the result escape 1..6.

## Reference Application Blueprint

### Exact draft and cursor restoration

Import `Document` from prompt-toolkit and capture the buffer document before
clearing:

```python
from prompt_toolkit.document import Document


async def _submit_composer(self) -> None:
    buffer = self._composer.buffer
    original = buffer.document
    draft = original.text
    command = draft.strip()

    if not command:
        return

    if command == "/help":
        buffer.set_document(Document("", 0), bypass_readonly=True)
        self.state = open_overlay(self.state, HELP_TEXT)
        return

    if command == "/quit":
        buffer.set_document(Document("", 0), bypass_readonly=True)
        await self._request_quit()
        return

    if command == "/stop":
        buffer.set_document(Document("", 0), bypass_readonly=True)
        self._confirm_action = "stop"
        self.state = open_confirmation(self.state, "停止当前研究执行？")
        return

    buffer.set_document(Document("", 0), bypass_readonly=True)
    self._history_scroll = 0
    self.state = set_history_follow(self.state, True)
    self.state = set_error(append_user_message(self.state, draft), None)
    if self._app is not None:
        self._app.invalidate()

    try:
        await self._controller.send_message(draft)
    except Exception as exc:
        buffer.set_document(original, bypass_readonly=True)
        self.state = replace(self.state, composer=original.text)
        self.state = set_error(self.state, f"发送失败：{exc}")
        if self._app is not None:
            self._app.invalidate()
```

Use `Buffer.set_document` because it updates text and cursor atomically. The
current `TextArea.text` setter creates `Document(value, 0)`, which places the
cursor at the start and fails the recovery contract.

The `on_text_changed` callback remains the owner of normal composer-to-state
synchronization. Explicitly setting `state.composer` in the exception branch
makes restoration deterministic even with a test stub or delayed callback.

### Cursor-local newline insertion

```python
def _insert_newline(self) -> None:
    if self._composer is None:
        return
    self._composer.buffer.insert_text("\n")
    if self._app is not None:
        self._app.invalidate()
```

`Buffer.insert_text` inserts at the current cursor and advances it. Do not
rebuild text with `draft + "\n"`; that breaks editing in the middle of a
message and discards selection behavior.

### Key binding construction

Use distinct filters so keys have one owner:

```python
def _key_bindings(self) -> KeyBindings:
    keys = KeyBindings()
    composer_active = Condition(
        lambda: self.state.mode == COMPOSER and self.state.overlay is None
    )
    empty_composer = Condition(
        lambda: self._composer is not None and not self._composer.text
    )
    confirmation = Condition(lambda: self.state.mode == CONFIRMATION)
    overlay_open = Condition(
        lambda: self.state.mode == COMPOSER and self.state.overlay is not None
    )

    @keys.add("enter", filter=composer_active, eager=True)
    async def submit_or_newline(event) -> None:
        if self._is_shift_enter(event):
            self._insert_newline()
        else:
            await self.handle_key("enter")

    @keys.add("c-j", filter=composer_active, eager=True)
    def insert_newline(_event) -> None:
        self._insert_newline()

    @keys.add("?", filter=composer_active & empty_composer, eager=True)
    async def open_help(_event) -> None:
        await self.handle_key("?")

    @keys.add("escape", filter=overlay_open | confirmation, eager=True)
    async def close_bottom_mode(_event) -> None:
        await self.handle_key("esc")

    @keys.add("y", filter=confirmation, eager=True)
    async def confirm(_event) -> None:
        await self.handle_key("y")

    @keys.add("n", filter=confirmation, eager=True)
    async def cancel(_event) -> None:
        await self.handle_key("n")

    for key, token in (
        ("pageup", "pageup"),
        ("pagedown", "pagedown"),
        ("end", "end"),
        ("c-c", "c-c"),
    ):
        # Use a small local factory to capture token by value.
        def register(bound_key: str, bound_token: str) -> None:
            @keys.add(bound_key)
            async def dispatch(_event) -> None:
                await self.handle_key(bound_token)

        register(key, token)

    return keys
```

Import `KeyPressEvent` from `prompt_toolkit.key_binding.key_processor`. Do not
compare event repr strings; use `event.data` and normalized local tokens.

Do not add a global `Keys.Any` modal catcher: it can compete with concrete
confirmation bindings. Instead, make the composer read-only whenever its pane
is not active, as shown in the composer construction below. The real-key test
must type ordinary characters while help and confirmation are open and assert
the draft is unchanged.

During implementation, confirm that prompt-toolkit accepts the composed filter
and `eager=True` signatures in version 3.0.52 by running the real pipe-input
tests. If a binding conflicts with a default TextArea binding, the RED test
must remain failing until the local binding demonstrably wins.

### Composer construction

```python
self._composer = TextArea(
    height=lambda: self._composer_height(),
    prompt=[("class:composer.prompt", "› ")],
    multiline=True,
    wrap_lines=True,
    read_only=Condition(
        lambda: self.state.mode != COMPOSER or self.state.overlay is not None
    ),
    scrollbar=False,
    style="class:composer",
)
self._composer.buffer.on_text_changed += self._on_composer_changed
```

Use a named callback so it can synchronize state and invalidate dynamic height:

```python
def _on_composer_changed(self, _buffer) -> None:
    self.state = replace(self.state, composer=self._composer.text)
    if self._app is not None:
        self._app.invalidate()
```

`TextArea` receives no `accept_handler`. Submission belongs solely to the
explicit app key binding. `scrollbar=False` avoids consuming one column; the
composer Window scrolls vertically once its preferred content exceeds six
rows.

### Container construction

Use prompt-toolkit public containers:

```python
from prompt_toolkit.layout import Dimension, HSplit, Layout, Window
from prompt_toolkit.widgets import Frame, TextArea

header = Window(
    FormattedTextControl(lambda: render_header(self.state, self._render_width())),
    height=1,
    dont_extend_height=True,
    style="class:header",
)

self._history_window = Window(
    FormattedTextControl(self._history_fragments),
    height=Dimension(min=3),
    wrap_lines=False,
    always_hide_cursor=True,
)

composer_frame = Frame(
    self._composer,
    title="输入研究指导",
    style="class:composer-frame",
)

composer_hint = Window(
    FormattedTextControl(
        lambda: render_bottom_pane(self.state, self._render_width())
    ),
    height=1,
    dont_extend_height=True,
)

self._composer_pane = HSplit([composer_frame, composer_hint])

self._confirmation_pane = Window(
    FormattedTextControl(
        lambda: render_bottom_pane(self.state, self._render_width())
    ),
    height=Dimension(min=2, max=4),
    dont_extend_height=True,
)

self._overlay_pane = Window(
    FormattedTextControl(
        lambda: render_bottom_pane(self.state, self._render_width())
    ),
    height=Dimension(min=3, max=10),
    dont_extend_height=True,
)

self._bottom = DynamicContainer(self._bottom_container)

status = Window(
    FormattedTextControl(lambda: render_status(self.state, self._render_width())),
    height=1,
    dont_extend_height=True,
)

root = HSplit([header, self._history_window, self._bottom, status])
self._app = Application(
    layout=Layout(root, focused_element=self._composer),
    key_bindings=self._key_bindings(),
    full_screen=True,
    input=self._input,
    output=self._output,
    style=_STYLE,
)
```

Validate exact `Frame` row accounting using the 24x80 and 12x40 DummyOutput
layout tests. If `Frame` and callable height interact differently than expected,
keep the same visible contract but use a `DynamicContainer` around one of six
pre-sized composer windows. Do not change the 1..6 behavior or add another
input buffer.

### Event application and viewport freezing

```python
def _on_event(self, event: OutputEvent | StateEvent) -> None:
    width = self._render_width()
    before = self._history_line_count(width)
    was_following = self.state.history_follow_tail
    self.state = apply_event(self.state, event, width)
    after = self._history_line_count(width)

    if isinstance(event, OutputEvent) and not was_following:
        self._history_scroll += max(0, after - before)
        self._clamp_history_scroll()

    if self._app is not None:
        if self.state.mode == COMPOSER and self.state.overlay is None:
            self._app.layout.focus(self._composer)
        self._app.invalidate()
```

Do not force focus onto the composer while help or confirmation is open.
Runtime output may arrive in every local mode; history and unseen count still
update while the bottom overlay remains active.

### Help and footer copy

The local help constant is exact enough to test but may wrap by width:

```python
HELP_TEXT = """输入
Enter 发送    Shift+Enter 换行    Ctrl+J 换行

导航
PageUp/PageDown 滚动输出    End 回到最新    Esc 返回

控制
/pause    /resume    /stop    /quit"""
```

Normal composer hint priority:

```text
wide:   Enter 发送 · Shift+Enter 换行 · Ctrl+J 换行 · ? 帮助
medium: Enter 发送 · Shift+Enter/Ctrl+J 换行
narrow: Enter 发送 · Ctrl+J 换行
tiny:   empty hint
```

Because terminal capability negotiation is not being added, always show
Ctrl+J alongside Shift+Enter at wide widths. This makes the fallback
discoverable even when a terminal collapses modified Enter into plain Enter.

Confirmation copy is action-specific:

```text
stop: 停止当前研究执行？  Y 确认 · N/Esc 取消
quit: 退出 TUI？研究执行将在后台继续。  Y 确认 · N/Esc 取消
```

The TUI does not claim that quitting stops research. Stopping and quitting
remain distinct local actions.

## Test Fixtures And Imports

Use the current `FakeRuntime`, `BlockingRuntime`, `StubComposer`,
`create_pipe_input`, `DummyInput`, and `DummyOutput` patterns. Extend them
locally rather than adding test-only behavior to production classes.

Because the target `_submit_composer` uses the real Buffer document/cursor API,
replace the old `.text`-only `StubComposer` with this narrow real-Buffer wrapper:

```python
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document


class StubComposer:
    def __init__(self, text: str, cursor_position: int | None = None) -> None:
        cursor = len(text) if cursor_position is None else cursor_position
        self.buffer = Buffer(document=Document(text, cursor))

    @property
    def text(self) -> str:
        return self.buffer.text

    @text.setter
    def text(self, value: str) -> None:
        self.buffer.set_document(Document(value, len(value)), bypass_readonly=True)
```

This wrapper uses prompt-toolkit's real text/cursor behavior; it does not mock
Buffer methods. Add a send-failure fixture with the cursor between words and
assert the same cursor position after restoration.

Each changed test file should import its direct production API. Tests must not
reach through `AthenaApp` to test a pure renderer or state transition. The only
allowed runtime double mirrors the complete runtime methods used by the TUI:

```python
class FakeRuntime:
    def __init__(self) -> None:
        self.callback = None
        self.messages: list[str] = []
        self.closed = False

    def subscribe(self, callback) -> str:
        self.callback = callback
        callback(
            "state",
            StateEvent(
                status="RUNNING",
                phase="SEARCH",
                plans=[],
                search={
                    "attempts": 0,
                    "limit": 10,
                    "successes": 0,
                    "concurrency": 4,
                },
                sota=None,
                waiting=None,
            ).model_dump(mode="json"),
        )
        return "sub-1"

    def unsubscribe(self, subscription_id: str) -> None:
        if subscription_id == "sub-1":
            self.callback = None

    async def message(self, text: str) -> str:
        self.messages.append(text)
        return "ok"

    async def aclose(self) -> None:
        self.closed = True
```

Renderer assertions convert fragments to plain text only when checking text.
Style assertions inspect fragment style names directly, for example:

```python
fragments = render_header(state, 80)
assert ("class:header.brand", "Athena") in fragments
assert "SEARCH" in fragment_list_to_text(fragments)
```

Do not assert private prompt-toolkit cache fields, mock call existence, exact
container repr strings, or ANSI escape bytes emitted by `DummyOutput`.

## Visual-Line Scroll Algorithm

`_history_scroll` is a nonnegative count of visual lines below the frozen
viewport, not a message index. The rendering algorithm is exact:

```python
def _join_lines(lines: tuple[StyleAndTextTuples, ...]) -> StyleAndTextTuples:
    output: StyleAndTextTuples = []
    for index, line in enumerate(lines):
        if index:
            output.append(("", "\n"))
        output.extend(line)
    return output


def _history_line_count(self, width: int | None = None) -> int:
    render_width = self._render_width() if width is None else width
    return len(render_history_lines(self.state, render_width))


def _clamp_history_scroll(self) -> None:
    max_scroll = max(0, self._history_line_count() - self._history_height())
    self._history_scroll = min(max_scroll, max(0, self._history_scroll))


def _history_fragments(self) -> StyleAndTextTuples:
    lines = render_history_lines(self.state, self._render_width())
    height = self._history_height()
    total = len(lines)
    end = total if self.state.history_follow_tail else max(
        0, total - self._history_scroll
    )
    start = max(0, end - height)
    return _join_lines(lines[start:end])
```

`_join_lines` inserts one unstyled newline fragment between visual lines and no
trailing newline. PageUp/PageDown move by `max(1, history_height - 1)` visual
lines, clamped to `0..max(0, total - history_height)`:

```python
page = max(1, self._history_height() - 1)
max_scroll = max(0, self._history_line_count() - self._history_height())

if token == "pageup":
    self._history_scroll = min(max_scroll, self._history_scroll + page)
    self.state = set_history_follow(self.state, False)
    return
if token == "pagedown":
    self._history_scroll = max(0, self._history_scroll - page)
    self.state = set_history_follow(self.state, self._history_scroll == 0)
    return
if token == "end":
    self._history_scroll = 0
    self.state = set_history_follow(self.state, True)
    return
```

Mouse-wheel scrolling reuses the same visual-line offset and clamping. Extract
the state transition into synchronous helpers and use a fixed three-line wheel
step:

```python
_WHEEL_SCROLL_LINES = 3


def _scroll_history_up(self, lines: int) -> None:
    max_scroll = max(0, self._history_line_count() - self._history_height())
    self._history_scroll = min(max_scroll, self._history_scroll + lines)
    self.state = set_history_follow(self.state, self._history_scroll == 0)


def _scroll_history_down(self, lines: int) -> None:
    self._history_scroll = max(0, self._history_scroll - lines)
    self.state = set_history_follow(self.state, self._history_scroll == 0)
```

PageUp/PageDown call these helpers with the page size. The output window uses a
local `FormattedTextControl` subclass whose `mouse_handler` calls them with
`_WHEEL_SCROLL_LINES` for `MouseEventType.SCROLL_UP` and
`MouseEventType.SCROLL_DOWN`. It returns `NotImplemented` for all other mouse
events. Build `Application(..., mouse_support=True)` so supported terminals
report wheel events. Because the handler belongs to the output control, the
composer and every other pane remain outside its scope.

When a new OutputEvent arrives while scrolled, freeze the visible text by
measuring rendered line counts before and after applying the event at the
current width:

```python
before = self._history_line_count()
self.state = apply_event(self.state, event, width)
after = self._history_line_count()
if isinstance(event, OutputEvent) and not self.state.history_follow_tail:
    self._history_scroll += max(0, after - before)
```

Clamp after updating. A StateEvent never changes scroll offset because it does
not append history. A duplicate OutputEvent has no line delta and no unseen
increment. Resizing recomputes wrapping and clamps the offset; it may change
which exact wrapped line is at the top, but it must not re-enable tail follow.

## Task 0: Verify Upstream PREPARE Output Atomicity And Ownership Release

Task 0 is read-only. It makes the active Supervisor plan's T13-P2 a measured
baseline rather than silently assuming that presentation code can repair a
broken event stream.

**Files:**

- Read: `codex_docs/CURRENT.md`
- Read: the active implementation plan linked from `codex_docs/CURRENT.md`
- Read: `src/athena/research/supervisor/prepare.py`
- Read: `src/athena/research/supervisor/events.py`
- Run: `test/integration/research/test_prepare_agent_contract.py`
- Run: `test/unit/research/supervisor/test_events.py`
- Modify: none

**Interfaces:**

- Consumes T13-P2's existing `EmitEvent(kind, event_ref, data)` projection.
- Requires unchanged public `OutputEvent`, `StateEvent`, and subscriber kinds
  `output`/`state`.
- Produces only recorded baseline evidence: starting commit, owner release,
  commands, counts, and observed semantic payloads.

**Required upstream fixture:** T13-P2's tests must exercise provider fragments
equivalent to this literal sequence; a one-chunk fake does not reproduce the
reported bug:

```python
chunks = [
    '{"decision":"submit","reason":"',
    "Baseline",
    ",",
    " predictions",
    ",",
    " report",
    ",",
    " and",
    " manifest",
    " are",
    " all",
    " present",
    " and",
    " validated",
    ".",
    '","suggestions":[]}',
]
reason = "Baseline, predictions, report, and manifest are all present and validated."
```

For every emitted provider chunk, the fake must set `delta` to that exact
chunk and `accumulated` to the concatenation through that chunk. This catches
both implementations that misuse `delta` and implementations that repeatedly
publish growing `accumulated` snapshots.

- [ ] **Step 1: Confirm T13-P2 is complete and all overlapping ownership is released**

  Run:

  ```powershell
  Get-Content codex_docs/CURRENT.md
  git status --short
  git log -5 --oneline -- src/athena/research/supervisor/prepare.py src/athena/research/supervisor/events.py src/athena_tui
  ```

  Expected: `CURRENT.md` no longer blocks this packet, no Agent claim owns any
  file listed under this packet's **Owned Files**, and the T13-P2 completion
  evidence names its commit and focused RED/GREEN commands. Existing unrelated
  worktree changes are not ownership release; stop if ownership is ambiguous.

- [ ] **Step 2: Run the complete upstream projection regression**

  Run:

  ```powershell
  uv run pytest test/integration/research/test_prepare_agent_contract.py test/unit/research/supervisor/test_events.py -q -p no:cacheprovider
  ```

  Expected: exit 0. The suite must prove all of these observable outcomes:

  ```text
  exactly one Agent text output contains the validated reason
  reason == "Baseline, predictions, report, and manifest are all present and validated."
  no visible Agent text contains '"decision"' or '"suggestions"'
  no visible Agent text equals an individual provider token/chunk
  punctuation and spaces equal the validated reason byte-for-byte in UTF-8
  a completed command still produces its own tool output record
  CRLF and bare CR become display-safe LF without visible ^M
  subscriber event kinds remain exactly {"output", "state"}
  ```

  If any outcome lacks an assertion, this is not a passing prerequisite even
  if the current test files exit 0. Return the gap to T13-P2; Task 0 cannot add
  runtime tests because those files are unowned here.

- [ ] **Step 3: Confirm failed and interrupted turns cannot leak partial JSON**

  Inspect the T13-P2 tests for both `turn_failed` and `turn_interrupted` after
  at least one partial `agent/text_delta`. Each test must assert that no Agent
  prose output was published from the partial response. Tool outputs completed
  before the terminal event may remain visible. Failure status/error reporting
  is owned by the existing Supervisor path and must not be synthesized by TUI.

  If T13-P2 intentionally proves this through one parameterized test, record
  both parameter IDs in the evidence. If the active plan does not cover these
  terminal cases, record an upstream acceptance gap and stop. The `/root`
  coordinator decides whether to extend T13-P2 or assign a separate non-TUI
  task before activating this packet; this packet cannot claim that authority
  or weaken the prerequisite.

- [ ] **Step 4: Verify the public protocol stayed unchanged**

  Run:

  ```powershell
  uv run pytest test/integration/test_tui_protocol.py test/unit/test_tui.py -q -p no:cacheprovider
  ```

  Expected: exit 0; only `output` and `state` cross the runtime subscription,
  and user text still enters through `runtime.message(text)`.

- [ ] **Step 5: Record the immutable baseline before Task 1 edits**

  Record:

  ```text
  T13-P2 commit:
  Beautification starting commit:
  Ownership release evidence:
  Upstream focused command + result/count:
  Terminal partial-output test IDs:
  Protocol command + result/count:
  Exact observed readable reason:
  Observed public event kinds:
  ```

  Do not create a commit for Task 0.

## Task 1: Structured Local History, Event Atomicity, And Unseen Output

**Files:**

- Modify: `src/athena_tui/state.py`
- Test: `test/unit/athena_tui/test_state.py`

**Interfaces:**

- Consumes unchanged `OutputEvent` fields: `seq`, `source`, `channel`, `text`,
  `plan`, `tool`, `artifact_ref`, and `truncated`.
- Produces local-only `HistoryEntry`, `append_user_message`, and
  `TuiState.unseen_output_count`. These types are not exported through the
  runtime or controller.
- Preserves a one-to-one mapping between accepted `OutputEvent` instances and
  runtime `HistoryEntry` instances. Same source/plan affects only marker styling
  in Task 2; it never changes state/event identity.

**Exact test imports:**

```python
import pytest

from athena.research.supervisor.events import OutputEvent
from athena_tui.state import (
    HistoryEntry,
    TuiState,
    append_user_message,
    apply_output,
    apply_snapshot,
    set_history_follow,
)
```

**Migration:** Replace every `history == ("...",)` assertion in
`test_state.py` and `test_app.py` with `HistoryEntry` assertions in the task
that owns that test file. Remove `append_history` imports only after all its
consumers have migrated; `rg -n "append_history" src/athena_tui
test/unit/athena_tui` must return no matches at the end of Task 3.

- [ ] **Step 1: Add focused failing state tests**

  Adopt the structured state introduced by commit `7ffb469` if it is still
  present, replace remaining string-history assertions with observable entry
  behavior, and add the following tests. The first test is the regression for
  the unsafe adjacent-record merge currently visible in the working tree:

  ```python
  from athena_tui.state import (
      HistoryEntry,
      TuiState,
      append_user_message,
      apply_output,
      set_history_follow,
  )


  def test_adjacent_agent_outputs_with_same_plan_remain_distinct_events() -> None:
      state = apply_output(
          TuiState(),
          OutputEvent(
              seq=1,
              source="agent",
              channel="text",
              text="first validated turn",
              plan="prepare",
          ),
      )

      updated = apply_output(
          state,
          OutputEvent(
              seq=2,
              source="agent",
              channel="text",
              text="second validated turn",
              plan="prepare",
          ),
      )

      assert updated.history == (
          HistoryEntry(
              kind="runtime",
              text="first validated turn",
              source="agent",
              channel="text",
              plan="prepare",
          ),
          HistoryEntry(
              kind="runtime",
              text="second validated turn",
              source="agent",
              channel="text",
              plan="prepare",
          ),
      )


  def test_output_text_preserves_validated_punctuation_and_spacing_exactly() -> None:
      reason = (
          "Baseline, predictions, report, and manifest are all present "
          "and validated."
      )

      updated = apply_output(
          TuiState(),
          OutputEvent(
              seq=1,
              source="agent",
              channel="text",
              text=reason,
              plan="prepare",
          ),
      )

      assert updated.history[0].text == reason


  def test_output_appends_a_structured_runtime_entry_without_touching_draft() -> None:
      state = TuiState(composer="keep this draft")
      event = OutputEvent(
          seq=1,
          source="agent",
          channel="text",
          text="first result",
          plan="plan-1",
      )

      updated = apply_output(state, event)

      assert updated.history == (
          HistoryEntry(
              kind="runtime",
              text="first result",
              source="agent",
              channel="text",
              plan="plan-1",
          ),
      )
      assert updated.composer == "keep this draft"


  def test_user_submission_is_a_distinct_history_entry() -> None:
      updated = append_user_message(TuiState(), "line one\nline two")

      assert updated.history == (
          HistoryEntry(kind="user", text="line one\nline two"),
      )


  def test_scrolled_view_counts_new_output_until_tail_follow_resumes() -> None:
      state = set_history_follow(TuiState(), False)
      state = apply_output(
          state,
          OutputEvent(seq=1, source="supervisor", channel="text", text="one"),
      )
      state = apply_output(
          state,
          OutputEvent(seq=2, source="tool", channel="stdout", text="two"),
      )

      assert state.history_follow_tail is False
      assert state.unseen_output_count == 2
      assert set_history_follow(state, True).unseen_output_count == 0
  ```

  Implement the invariant test as two literal constructor calls:

  ```python
  def test_history_entry_rejects_inconsistent_kind_and_source() -> None:
      with pytest.raises(ValueError, match="user history has no runtime source"):
          HistoryEntry(kind="user", source="agent", text="invalid")

      with pytest.raises(ValueError, match="runtime history requires source"):
          HistoryEntry(kind="runtime", source=None, text="invalid")
  ```

- [ ] **Step 2: Verify RED**

  Run:

  ```powershell
  uv run pytest test/unit/athena_tui/test_state.py -q -p no:cacheprovider
  ```

  Expected RED on the current implementation:
  `test_adjacent_agent_outputs_with_same_plan_remain_distinct_events` receives
  one concatenated entry instead of two. The exact-text test should already
  pass; it is a characterization guard. If the event-atomicity test passes,
  inspect whether concurrent work already removed the merge, record that fact,
  and add no redundant production change. A RED test must fail for the named
  behavior, not because an import or fixture is broken.

- [ ] **Step 3: Implement the smallest local state model**

  Add the literal aliases, immutable display record, and local transition
  implementation below. Preserve the existing overlay/confirmation helpers
  exactly unless their named behavior is changed by a later RED test:

  ```python
  from dataclasses import dataclass, replace
  from typing import Any, Literal

  HistoryKind = Literal["user", "runtime"]
  OutputSource = Literal["supervisor", "agent", "tool"]
  OutputChannel = Literal["text", "stdout", "stderr", "error"]


  @dataclass(frozen=True)
  class HistoryEntry:
      kind: HistoryKind
      text: str
      source: OutputSource | None = None
      channel: OutputChannel = "text"
      plan: str | None = None
      tool: str | None = None
      truncated: bool = False

      def __post_init__(self) -> None:
          if self.kind == "user" and self.source is not None:
              raise ValueError("user history has no runtime source")
          if self.kind == "runtime" and self.source is None:
              raise ValueError("runtime history requires source")


  @dataclass(frozen=True)
  class TuiState:
      project_root: str = "."
      mode: str = COMPOSER
      status: str = "RUNNING"
      phase: str = "SEARCH"
      plans: tuple[dict[str, Any], ...] = ()
      search: dict[str, Any] | None = None
      sota: dict[str, Any] | None = None
      waiting: dict[str, Any] | None = None
      history: tuple[HistoryEntry, ...] = ()
      last_output_seq: int = 0
      history_follow_tail: bool = True
      unseen_output_count: int = 0
      composer: str = ""
      overlay: str | None = None
      last_error: str | None = None

      @property
      def control_status(self) -> str:
          return self.status


  def apply_snapshot(state: TuiState, event: object) -> TuiState:
      search = dict(getattr(event, "search"))
      sota = getattr(event, "sota")
      waiting = getattr(event, "waiting")
      return replace(
          state,
          status=str(getattr(event, "status")),
          phase=str(getattr(event, "phase")),
          plans=tuple(dict(plan) for plan in getattr(event, "plans")),
          search=search,
          sota=None if sota is None else dict(sota),
          waiting=None if waiting is None else dict(waiting),
      )


  def apply_output(state: TuiState, event: object) -> TuiState:
      sequence = int(getattr(event, "seq"))
      if sequence <= state.last_output_seq:
          return state

      entry = HistoryEntry(
          kind="runtime",
          text=str(getattr(event, "text")),
          source=getattr(event, "source"),
          channel=getattr(event, "channel"),
          plan=getattr(event, "plan", None),
          tool=getattr(event, "tool", None),
          truncated=bool(getattr(event, "truncated", False)),
      )
      return replace(
          state,
          history=(*state.history, entry)[-1000:],
          last_output_seq=sequence,
          unseen_output_count=(
              state.unseen_output_count
              if state.history_follow_tail
              else state.unseen_output_count + 1
          ),
      )


  def append_user_message(state: TuiState, text: str) -> TuiState:
      entry = HistoryEntry(kind="user", text=text)
      return replace(state, history=(*state.history, entry)[-1000:])


  def set_history_follow(state: TuiState, follow: bool) -> TuiState:
      return replace(
          state,
          history_follow_tail=follow,
          unseen_output_count=0 if follow else state.unseen_output_count,
      )
  ```

  The code above deliberately copies runtime dictionaries so later mutation of
  a Pydantic payload cannot mutate local immutable state indirectly.
  `apply_output` rejects duplicate/replayed sequence numbers, appends at most
  1000 entries, preserves `composer`, and increments `unseen_output_count`
  only when `history_follow_tail` is false. `append_user_message` appends a
  `kind="user"` entry without changing the unseen count.

  In particular, remove any branch shaped like this from `apply_output`:

  ```python
  if last.source == entry.source and last.plan == entry.plan:
      history = (*history[:-1], replace(last, text=last.text + entry.text))
  ```

  Do not replace it with a time window, channel heuristic, punctuation check,
  or look-behind buffer. `OutputEvent` has no turn identity, so every such
  heuristic can merge independent turns. Task 2 may visually suppress a
  repeated marker while retaining two entries.

- [ ] **Step 4: Verify GREEN and state regression**

  Run:

  ```powershell
  uv run pytest test/unit/athena_tui/test_state.py -q -p no:cacheprovider
  ```

  Expected: all state tests pass, including one-entry-per-event atomicity,
  exact text preservation, sequence de-duplication, unseen counts, and the
  1000-entry bound.

- [ ] **Step 5: Commit only Task 1 files**

  ```powershell
  git add src/athena_tui/state.py test/unit/athena_tui/test_state.py
  git commit -m "refactor(tui): structure local output history"
  ```

## Task 2: Codex-Like Pure Rendering And Responsive Priority

**Files:**

- Modify: `src/athena_tui/render.py`
- Test: `test/unit/athena_tui/test_render.py`

**Interfaces:**

- Consumes `TuiState` and `HistoryEntry` from Task 1.
- Preserves the existing helper names `render_header`, `render_history`,
  `render_bottom_pane`, and `render_status`; return values become
  prompt-toolkit formatted-text fragments where styling is needed.
- Produces `composer_height(text: str, width: int) -> int`, constrained to the
  inclusive range 1..6.
- Produces readable UTF-8 `HELP_TEXT` and all pure composer/help/confirmation
  bottom-pane text consumed by Tasks 4 and 5.
- Uses the existing `StateEvent.sota["metric"]` projection directly. It does
  not rename, derive, or request another metric field.

**Exact production imports:**

```python
from pathlib import Path

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.utils import get_cwidth

from athena_tui.state import CONFIRMATION, HistoryEntry, TuiState
```

**Exact test imports:**

```python
import pytest
from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.utils import get_cwidth

from athena_tui.render import (
    HELP_TEXT,
    composer_height,
    render_bottom_pane,
    render_header,
    render_history,
    render_history_lines,
    render_status,
)
from athena_tui.state import (
    HistoryEntry,
    TuiState,
    close_overlay,
    open_confirmation,
    open_overlay,
)
```

**Migration:** Delete the old `textwrap`-based `wrap` helper after all four
renderers use styled cell wrapping. No test should retain a dependency on
Python character count for terminal width.

- [ ] **Step 1: Write failing renderer behavior tests**

  Add a local test helper that uses prompt-toolkit's public conversion API:

  ```python
  from prompt_toolkit.formatted_text import fragment_list_to_text


  def plain(value) -> str:
      return fragment_list_to_text(value) if not isinstance(value, str) else value
  ```

  Add these tests with literal expectations:

  ```python
  def test_header_prioritizes_product_phase_and_status_at_narrow_width() -> None:
      state = TuiState(
          project_root="C:/very/long/project/path/athena-experiment",
          phase="SEARCH",
          status="RUNNING",
      )

      wide = plain(render_header(state, 100))
      narrow = plain(render_header(state, 40))

      assert "Athena" in wide
      assert "C:/very/long/project/path/athena-experiment" in wide
      assert "SEARCH" in narrow
      assert "RUNNING" in narrow
      assert get_cwidth(narrow) <= 40


  def test_history_renders_user_runtime_tool_and_error_with_distinct_markers() -> None:
      state = TuiState(
          history=(
              HistoryEntry(kind="user", text="try a forest"),
              HistoryEntry(kind="runtime", source="agent", text="working"),
              HistoryEntry(
                  kind="runtime", source="tool", channel="stdout", text="0.84"
              ),
              HistoryEntry(
                  kind="runtime", source="tool", channel="error", text="failed"
              ),
          )
      )

      output = plain(render_history(state, 50))

      assert "› try a forest" in output
      assert "● working" in output
      assert "tool · stdout" in output
      assert "! failed" in output


  def test_consecutive_text_from_the_same_source_suppresses_repeat_marker() -> None:
      state = TuiState(
          history=(
              HistoryEntry(kind="runtime", source="agent", text="first"),
              HistoryEntry(kind="runtime", source="agent", text="second"),
          )
      )

      output = plain(render_history(state, 50))

      assert output.count("●") == 1
      assert "  second" in output


  def test_wrapping_preserves_styles_and_terminal_cell_width() -> None:
      state = TuiState(
          history=(HistoryEntry(kind="user", text="中文中文中文"),)
      )

      lines = render_history_lines(state, 8)

      assert len(lines) >= 2
      assert all(get_cwidth(fragment_list_to_text(line)) <= 8 for line in lines)
      assert any(style == "class:history.user.marker" for style, _ in lines[0])
      assert any(style == "class:history.user" for line in lines for style, _ in line)


  def test_dynamic_text_is_not_interpreted_as_style_markup() -> None:
      state = TuiState(
          history=(HistoryEntry(kind="runtime", source="agent", text="[red]literal"),)
      )

      output = render_history(state, 40)

      assert "[red]literal" in fragment_list_to_text(output)


  def test_status_prioritizes_error_waiting_and_unseen_output() -> None:
      assert "boom" in plain(render_status(TuiState(last_error="boom"), 50))
      assert "需要指导" in plain(
          render_status(
              TuiState(status="WAITING", waiting={"reason": "需要指导"}), 50
          )
      )
      assert "3 条新输出" in plain(
          render_status(
              TuiState(history_follow_tail=False, unseen_output_count=3), 50
          )
      )


  def test_wide_status_shows_search_and_sota_but_narrow_status_keeps_core_state() -> None:
      state = TuiState(
          phase="SEARCH",
          status="RUNNING",
          search={"attempts": 3, "limit": 10, "successes": 2, "concurrency": 4},
          sota={"metric": 0.8421},
      )

      wide = plain(render_status(state, 100))
      narrow = plain(render_status(state, 38))

      assert "3/10" in wide
      assert "2 successful" in wide
      assert "4 workers" in wide
      assert "SOTA 0.8421" in wide
      assert "SEARCH" in narrow
      assert "RUNNING" in narrow
      assert get_cwidth(narrow) <= 38


  @pytest.mark.parametrize(
      ("text", "width", "expected"),
      [
          ("", 80, 1),
          ("one line", 80, 1),
          ("one\ntwo\nthree", 80, 3),
          ("x" * 500, 20, 6),
      ],
  )
  def test_composer_height_is_bounded_by_content(text, width, expected) -> None:
      assert composer_height(text, width) == expected


  def test_bottom_pane_renders_composer_help_overlay_and_confirmation() -> None:
      composer = plain(render_bottom_pane(TuiState(), 80))
      assert "Enter 发送" in composer
      assert "Shift+Enter 换行" in composer
      assert "Ctrl+J" in composer

      help_state = open_overlay(TuiState(composer="keep"), HELP_TEXT)
      help_output = plain(render_bottom_pane(help_state, 80))
      assert "输入" in help_output
      assert "导航" in help_output
      assert "控制" in help_output
      assert close_overlay(help_state).composer == "keep"

      confirmation = open_confirmation(TuiState(), "停止当前研究执行？")
      confirm_output = plain(render_bottom_pane(confirmation, 80))
      assert "停止当前研究执行？" in confirm_output
      assert "Y 确认" in confirm_output
      assert "N/Esc 取消" in confirm_output
  ```

- [ ] **Step 2: Verify RED**

  Run:

  ```powershell
  uv run pytest test/unit/athena_tui/test_render.py -q -p no:cacheprovider
  ```

  Expected RED: `render_header` rejects `width`, history has no structured
  rendering, `composer_height` and `HELP_TEXT` are missing, and current
  bottom-pane copy does not describe multiline input.

- [ ] **Step 3: Implement semantic fragments and width tiers**

  Use prompt-toolkit style classes, not hard-coded ANSI escapes. The required
  semantic classes are:

  ```text
  header.brand, header.path, phase.running, phase.waiting, phase.done,
  phase.stopped, history.user.marker, history.user, history.agent.marker,
  history.agent, history.tool, history.error, status.primary,
  status.secondary, status.warning, status.error, status.unseen
  ```

  Required priority order:

  1. `last_error` replaces the ordinary footer.
  2. `WAITING` reason replaces ordinary progress details.
  3. When not following the tail, unseen output is shown as
     `↓ N 条新输出 · End 回到最新`.
  4. At width >= 72, show attempts, successes, workers, and SOTA when present.
  5. At width 48..71, show phase/status, attempts, and SOTA.
  6. At width < 48, show only phase and status, truncated to the width.

  Missing `search`, `sota`, `waiting`, or nested keys must be omitted rather
  than rendered as fake zero-valued research results. `composer_height` uses
  `prompt_toolkit.utils.get_cwidth` so Chinese and ASCII text consume their
  actual terminal-cell widths. Implement `render_history` as a thin join over
  `render_history_lines`; do not maintain a second rendering branch.

  Add the exact `HELP_TEXT` and responsive normal composer hints from the
  Reference Application Blueprint. Confirmation rendering uses the overlay
  prompt already stored in `TuiState` and appends `Y 确认 · N/Esc 取消`; it
  does not inspect `_confirm_action` or call the runtime.

- [ ] **Step 4: Verify GREEN and renderer regression**

  Run:

  ```powershell
  uv run pytest test/unit/athena_tui/test_render.py -q -p no:cacheprovider
  ```

  Expected: all render tests pass at 100, 50, 40, and 38 columns without any
  rendered line exceeding its supplied width.

- [ ] **Step 5: Commit only Task 2 files**

  ```powershell
  git add src/athena_tui/render.py test/unit/athena_tui/test_render.py
  git commit -m "feat(tui): add responsive codex-inspired rendering"
  ```

## Task 3: Upper Output Pane Tail-Follow Behavior

**Files:**

- Modify: `src/athena_tui/app.py`
- Test: `test/unit/athena_tui/test_app.py`

**Interfaces:**

- Consumes Task 1 `HistoryEntry`, `append_user_message`, and unseen count.
- Consumes Task 2 formatted render helpers.
- Does not alter `AthenaApp.__init__`, `AthenaApp.run`, `apply_event`, or the
  controller/runtime boundary.

**Exact added test imports:**

```python
from dataclasses import replace

from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.data_structures import Point
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

from athena_tui.state import HistoryEntry
```

**Migration:** Rename `_history_text` to `_history_fragments`. Update the
history `FormattedTextControl` callable in the same GREEN edit. Do not leave a
compatibility alias; `_history_text` is private and retaining both paths risks
rendering/scroll divergence.

- [ ] **Step 1: Write failing application state-flow tests**

  ```python
  @pytest.mark.asyncio
  async def test_submit_moves_multiline_draft_to_upper_history_and_clears_composer() -> None:
      runtime = FakeRuntime()
      app = AthenaApp(runtime, Path("/tmp"))
      app._composer = StubComposer("first line\nsecond line")

      await app.handle_key("enter")

      assert runtime.messages == ["first line\nsecond line"]
      assert app._composer.text == ""
      assert app.state.history[-1].kind == "user"
      assert app.state.history[-1].text == "first line\nsecond line"


  def test_new_runtime_output_does_not_replace_existing_composer_draft() -> None:
      runtime = FakeRuntime()
      app = AthenaApp(runtime, Path("/tmp"))
      app.state = replace(app.state, composer="keep draft")

      app._on_event(
          OutputEvent(seq=1, source="agent", channel="text", text="new output")
      )

      assert app.state.composer == "keep draft"
      assert app.state.history[-1].text == "new output"


  @pytest.mark.asyncio
  async def test_pageup_holds_visual_view_and_end_clears_unseen_output() -> None:
      runtime = FakeRuntime()
      app = AthenaApp(runtime, Path("/tmp"))

      for sequence in range(1, 30):
          app._on_event(
              OutputEvent(
                  seq=sequence,
                  source="agent",
                  channel="text",
                  text=f"line {sequence}",
              )
          )

      await app.handle_key("pageup")
      before = fragment_list_to_text(app._history_fragments())
      app._on_event(
          OutputEvent(seq=30, source="supervisor", channel="text", text="late")
      )

      assert app.state.history_follow_tail is False
      assert app.state.unseen_output_count == 1
      assert fragment_list_to_text(app._history_fragments()) == before
      await app.handle_key("end")
      assert app.state.history_follow_tail is True
      assert app.state.unseen_output_count == 0
      assert app._history_scroll == 0


  @pytest.mark.asyncio
  async def test_pagedown_reenables_follow_only_when_scroll_offset_reaches_zero() -> None:
      app = AthenaApp(FakeRuntime(), Path("/tmp"))
      for sequence in range(1, 50):
          app._on_event(
              OutputEvent(
                  seq=sequence,
                  source="agent",
                  channel="text",
                  text=f"line {sequence}",
              )
          )
      await app.handle_key("pageup")
      await app.handle_key("pageup")

      await app.handle_key("pagedown")
      assert app.state.history_follow_tail is False
      await app.handle_key("pagedown")
      assert app.state.history_follow_tail is True


  def test_mouse_wheel_scrolls_output_by_three_visual_lines_and_resumes_tail() -> None:
      app = AthenaApp(
          FakeRuntime(), Path("/tmp"), input=DummyInput(), output=DummyOutput()
      )
      for sequence in range(1, 50):
          app._on_event(
              OutputEvent(
                  seq=sequence,
                  source="agent",
                  channel="text",
                  text=f"line {sequence}",
              )
          )
      app._build()
      wheel_up = MouseEvent(
          position=Point(x=0, y=0),
          event_type=MouseEventType.SCROLL_UP,
          button=MouseButton.NONE,
          modifiers=frozenset(),
      )
      wheel_down = MouseEvent(
          position=Point(x=0, y=0),
          event_type=MouseEventType.SCROLL_DOWN,
          button=MouseButton.NONE,
          modifiers=frozenset(),
      )

      assert app._history_control.mouse_handler(wheel_up) is None
      assert app._history_scroll == 3
      assert app.state.history_follow_tail is False
      assert app._history_control.mouse_handler(wheel_down) is None
      assert app._history_scroll == 0
      assert app.state.history_follow_tail is True


  def test_output_control_ignores_non_scroll_mouse_events() -> None:
      app = AthenaApp(
          FakeRuntime(), Path("/tmp"), input=DummyInput(), output=DummyOutput()
      )
      app._build()
      click = MouseEvent(
          position=Point(x=0, y=0),
          event_type=MouseEventType.MOUSE_DOWN,
          button=MouseButton.LEFT,
          modifiers=frozenset(),
      )

      assert app._history_control.mouse_handler(click) is NotImplemented
      assert app._history_scroll == 0
      assert app._app.mouse_support() is True


  @pytest.mark.asyncio
  async def test_send_failure_restores_exact_multiline_draft() -> None:
      class FailingRuntime(FakeRuntime):
          async def message(self, text: str) -> str:
              self.messages.append(text)
              raise RuntimeError("offline")

      runtime = FailingRuntime()
      app = AthenaApp(runtime, Path("/tmp"))
      draft = "first line\nsecond line"
      app._composer = StubComposer(draft, cursor_position=5)

      await app.handle_key("enter")

      assert runtime.messages == [draft]
      assert app._composer.text == draft
      assert app._composer.buffer.cursor_position == 5
      assert app.state.history[-1].text == draft
      assert "offline" in app.state.last_error
  ```

- [ ] **Step 2: Verify RED**

  Run:

  ```powershell
  uv run pytest test/unit/athena_tui/test_app.py -q -p no:cacheprovider
  ```

  Expected RED: submission still appends a plain string and the new structured
  history assertions fail; the app also has no output-specific mouse control
  and mouse reporting is disabled.

- [ ] **Step 3: Connect structured history and tail state**

  Replace the local `append_history(..., f"> {text}")` call with
  `append_user_message(state, text)`. Keep the existing order: copy the draft,
  clear the visible composer, append the user entry, invalidate, then await the
  controller. On send failure, restore the exact multiline draft and retain the
  user entry as evidence of the attempted message, while `last_error` explains
  the failure.

  Replace `_history_text` with `_history_fragments` and implement the
  Visual-Line Scroll Algorithm above. PageUp/PageDown move by a page of visual
  lines; wheel-up/wheel-down move by three lines only while the pointer is over
  the output control; End resets offset and unseen count. Enable
  `Application(mouse_support=True)`. The complete immutable history remains
  stored while only a visual-line slice is passed to the control.

- [ ] **Step 4: Verify GREEN and output/state application regression**

  Run:

  ```powershell
  uv run pytest test/unit/athena_tui/test_app.py test/unit/athena_tui/test_state.py test/unit/athena_tui/test_render.py -q -p no:cacheprovider
  ```

  Expected: all tests pass; real `MouseEvent` values move only the output
  viewport, duplicate `OutputEvent.seq` still renders once, and `StateEvent`
  still replaces every runtime-owned snapshot field.

- [ ] **Step 5: Commit only Task 3 files**

  ```powershell
  git add src/athena_tui/app.py test/unit/athena_tui/test_app.py
  git commit -m "feat(tui): keep output history independent from composer"
  ```

## Task 4: Multiline Composer, Enter Submit, Shift+Enter And Ctrl+J

**Files:**

- Modify: `src/athena_tui/app.py`
- Modify: `src/athena_tui/keybindings.py`
- Test: `test/unit/athena_tui/test_app.py`
- Test: `test/unit/athena_tui/test_keybindings.py`

**Interfaces:**

- Preserves `handle_key("enter")` as the submission path used by tests and the
  real prompt-toolkit Enter handler.
- Adds only local intent `insert_newline`; it never reaches `TuiController`.
- Uses Ctrl+J as the guaranteed fallback and recognizes raw
  `\x1b[27;2;13~` as modified Enter when prompt-toolkit preserves it.

**Exact production imports:**

```python
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
```

**Exact added test imports:**

```python
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.input import DummyInput, create_pipe_input
from prompt_toolkit.output import DummyOutput
```

**Migration:** Delete the TextArea `accept_handler`, set `multiline=True`, and
remove the old test that calls `buffer.validate_and_handle()`. The explicit
eager Enter binding and pipe-input tests become the single submission owner and
evidence.

- [ ] **Step 1: Add failing intent and real-input tests**

  Delete the old
  `test_prompt_toolkit_enter_submits_text_before_buffer_reset`. It asserts the
  removed single-line `accept_handler` mechanism, not retained user behavior.
  The real pipe-input test below replaces it and proves the retained contract
  through the new explicit Enter binding.

  ```python
  def test_composer_maps_newline_without_changing_submit_intent() -> None:
      assert map_key(COMPOSER, "enter") == ("submit", None)
      assert map_key(COMPOSER, "s-enter") == ("insert_newline", None)
      assert map_key(COMPOSER, "c-j") == ("insert_newline", None)
  ```

  Add an integration-style prompt-toolkit key test using the existing
  `create_pipe_input` and `DummyOutput` setup:

  ```python
  @pytest.mark.asyncio
  async def test_real_shift_enter_and_ctrl_j_insert_newlines_before_enter_submits() -> None:
      runtime = FakeRuntime()

      async def wait_until(predicate) -> None:
          async with asyncio.timeout(1):
              while not predicate():
                  await asyncio.sleep(0.01)

      with create_pipe_input() as pipe_input:
          app = AthenaApp(
              runtime,
              Path("/tmp"),
              input=pipe_input,
              output=DummyOutput(),
          )

          async def drive() -> None:
              pipe_input.send_text("line one")
              pipe_input.send_text("\x1b[27;2;13~")
              pipe_input.send_text("line two")
              pipe_input.send_text("\n")
              pipe_input.send_text("line three\r")
              await wait_until(lambda: len(runtime.messages) == 1)
              pipe_input.send_text("\x03")
              await wait_until(lambda: app.state.mode == CONFIRMATION)
              pipe_input.send_text("y")

          code, _ = await asyncio.wait_for(
              asyncio.gather(app.run(), asyncio.create_task(drive())),
              timeout=5,
          )

      assert code == 0
      assert runtime.messages == ["line one\nline two\nline three"]
  ```

  Add a second real-input test for punctuation and modal isolation:

  ```python
  @pytest.mark.asyncio
  async def test_question_mark_is_text_in_a_draft_and_modal_keys_do_not_change_it() -> None:
      runtime = FakeRuntime()

      async def wait_until(predicate) -> None:
          async with asyncio.timeout(1):
              while not predicate():
                  await asyncio.sleep(0.01)

      with create_pipe_input() as pipe_input:
          app = AthenaApp(
              runtime,
              Path("/tmp"),
              input=pipe_input,
              output=DummyOutput(),
          )

          async def drive() -> None:
              pipe_input.send_text("why?")
              await wait_until(lambda: app._composer.text == "why?")
              assert app.state.overlay is None

              # Empty the real buffer, then open help with ?.
              app._composer.buffer.set_document(
                  Document("", 0), bypass_readonly=True
              )
              pipe_input.send_text("?")
              await wait_until(lambda: app.state.overlay is not None)
              pipe_input.send_text("ignored")
              await asyncio.sleep(0.05)
              assert app._composer.text == ""
              pipe_input.send_text("\x1b")
              await wait_until(lambda: app.state.overlay is None)

              pipe_input.send_text("/stop\r")
              await wait_until(lambda: app.state.mode == CONFIRMATION)
              pipe_input.send_text("ignored")
              await asyncio.sleep(0.05)
              assert app._composer.text == ""
              pipe_input.send_text("n")
              await wait_until(lambda: app.state.mode == COMPOSER)

              pipe_input.send_text("\x03")
              await wait_until(lambda: app.state.mode == CONFIRMATION)
              pipe_input.send_text("y")

          code, _ = await asyncio.wait_for(
              asyncio.gather(app.run(), asyncio.create_task(drive())),
              timeout=5,
          )

      assert code == 0
      assert runtime.messages == []
  ```

  This test may manipulate the real Buffer from the test driver because the
  Buffer is the component under integration; it does not mock its behavior.

  Also add a build assertion:

  ```python
  def test_composer_is_multiline_and_height_is_bounded() -> None:
      app = AthenaApp(FakeRuntime(), Path("/tmp"), input=DummyInput(), output=DummyOutput())
      prompt_app = app._build()

      assert app._composer.buffer.multiline() is True
      assert app._composer_height() == 1
      app._composer.text = "1\n2\n3\n4\n5\n6\n7"
      assert app._composer_height() == 6
  ```

  Add an exact-payload test to prevent accidental `.strip()` of ordinary
  guidance:

  ```python
  @pytest.mark.asyncio
  async def test_submit_uses_strip_only_for_detection_and_sends_exact_payload() -> None:
      runtime = FakeRuntime()
      app = AthenaApp(runtime, Path("/tmp"))
      draft = "  first line\nsecond line  "
      app._composer = StubComposer(draft)

      await app.handle_key("enter")

      assert runtime.messages == [draft]
      assert app.state.history[-1].text == draft
  ```

- [ ] **Step 2: Verify RED**

  Run:

  ```powershell
  uv run pytest test/unit/athena_tui/test_keybindings.py test/unit/athena_tui/test_app.py -q -p no:cacheprovider
  ```

  Expected RED: the composer is single-line, `insert_newline` is unmapped,
  the explicit Shift+Enter/Ctrl+J behavior is absent, and ordinary payloads are
  currently stripped before send.

- [ ] **Step 3: Implement local key discrimination and bounded height**

  Build the `TextArea` with `multiline=True`, `wrap_lines=True`, and dynamic
  height from Task 2's `composer_height`. Remove the single-line
  `accept_handler`; bind submission explicitly so Enter has one owner.

  Add a single private constant in `app.py`:

  ```python
  _SHIFT_ENTER_SEQUENCES = {"\x1b[27;2;13~"}
  ```

  The app-level Enter handler inspects `event.key_sequence[-1].data`:

  ```python
  if event.key_sequence[-1].data in _SHIFT_ENTER_SEQUENCES:
      self._composer.buffer.insert_text("\n")
  else:
      await self.handle_key("enter")
  ```

  Bind `c-j` to `self._composer.buffer.insert_text("\n")` in composer mode.
  Keep confirmation-mode Y/N and overlay Esc precedence unchanged. Do not
  modify prompt-toolkit's `ANSI_SEQUENCES` global dictionary because that
  would change input parsing for every application in the process.

- [ ] **Step 4: Verify GREEN and keyboard regression**

  Run:

  ```powershell
  uv run pytest test/unit/athena_tui/test_keybindings.py test/unit/athena_tui/test_app.py -q -p no:cacheprovider
  ```

  Expected: real pipe-input tests prove Shift+Enter and Ctrl+J insert newlines,
  plain Enter sends exactly one complete message, confirmation keys do not
  enter the draft, and Ctrl+C behavior remains unchanged.

- [ ] **Step 5: Commit only Task 4 files**

  ```powershell
  git add src/athena_tui/app.py src/athena_tui/keybindings.py test/unit/athena_tui/test_app.py test/unit/athena_tui/test_keybindings.py
  git commit -m "feat(tui): add multiline composer key handling"
  ```

## Task 5: Layout, Theme, And Remaining Mojibake Repair

**Files:**

- Modify: `src/athena_tui/app.py`
- Modify: `src/athena_tui/render.py`
- Modify: `src/athena_tui/entrypoint.py`
- Test: `test/unit/athena_tui/test_app.py`
- Test: `test/unit/athena_tui/test_render.py`
- Test: `test/unit/athena_tui/test_entrypoint.py`

**Interfaces:**

- Preserves all key tokens, commands, modes, and runtime calls.
- Changes only prompt-toolkit containers, style classes, and displayed copy.
- Consumes Task 2 `HELP_TEXT` in `app.py`; removes the old `_HELP` duplicate.

**Exact production import changes:**

```python
from prompt_toolkit.layout import Dimension, HSplit, Layout, Window
from prompt_toolkit.layout.containers import DynamicContainer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import Frame, TextArea

from athena_tui.render import (
    HELP_TEXT,
    render_bottom_pane,
    render_header,
    render_status,
)
```

**Migration:** Remove `_HELP` and `_FIXED_ROWS`. Replace all calls to
`render_header(self.state)` with `render_header(self.state,
self._render_width())`. Keep one module-level `_STYLE`; do not create separate
styles in renderer or entrypoint modules.

- [ ] **Step 1: Write failing layout and remaining copy tests**

  ```python
  def test_non_tty_hint_is_readable_utf8_chinese(monkeypatch, capsys) -> None:
      class NotTTY:
          def isatty(self) -> bool:
              return False

      monkeypatch.setattr(entrypoint.sys, "stdin", NotTTY())
      monkeypatch.setattr(entrypoint.sys, "stdout", NotTTY())

      assert entrypoint.main([]) == 1
      error = capsys.readouterr().err
      assert "Athena TUI 需要交互式终端" in error
      assert "自动化请使用 Athena-cli" in error
  ```

  Add a layout construction test asserting that `_build()` creates one history
  window and one framed composer, keeps the composer as the initial focus, and
  gives the header/footer fixed height while history remains flexible. Assert
  public container behavior or concrete child count, not private
  prompt-toolkit rendering internals.

- [ ] **Step 2: Verify RED**

  Run:

  ```powershell
  uv run pytest test/unit/athena_tui/test_render.py test/unit/athena_tui/test_entrypoint.py test/unit/athena_tui/test_app.py -q -p no:cacheprovider
  ```

  Expected RED: the current app is not a one-row-header/Frame-composer layout,
  still owns duplicate mojibake help/confirmation strings, and the entrypoint
  Chinese literals do not equal readable UTF-8 copy.

- [ ] **Step 3: Apply the restrained theme and readable copy**

  Keep one full-width column. Use a one-row header, flexible upper history,
  one thin `Frame` around the composer, and a one-row status/footer. The bottom
  overlay or confirmation replaces the composer area through the existing
  `DynamicContainer`; it must not replace history.

  Required semantic colors:

  ```text
  neutral text     terminal default foreground
  muted text       #8b949e
  running          #2dd4bf
  waiting          #fbbf24
  completed        #22c55e
  stopped/error    #f87171
  user marker      #7dd3fc
  composer border  #475569 (focused: #94a3b8)
  ```

  Do not set a full-screen background color. This preserves compatibility with
  user terminal themes. Do not use gradients, filled cards, or multiple nested
  borders.

  Import Task 2's `HELP_TEXT` into `app.py` and remove `_HELP`. Replace every
  remaining mojibake literal in owned files with valid UTF-8 Chinese, including
  stop/quit confirmations, send/stop errors, tail-return hint, module
  docstrings, argument help, and the non-TTY automation hint.

- [ ] **Step 4: Verify GREEN and focused visual behavior**

  Run:

  ```powershell
  uv run pytest test/unit/athena_tui/test_render.py test/unit/athena_tui/test_entrypoint.py test/unit/athena_tui/test_app.py -q -p no:cacheprovider
  ```

  Expected: readable UTF-8 copy, one composer, one output pane, preserved draft
  across help/confirmation cancellation, and no line-overflow failures at 40
  and 80 columns.

- [ ] **Step 5: Commit only Task 5 files**

  ```powershell
  git add src/athena_tui/app.py src/athena_tui/render.py src/athena_tui/entrypoint.py test/unit/athena_tui/test_app.py test/unit/athena_tui/test_render.py test/unit/athena_tui/test_entrypoint.py
  git commit -m "style(tui): refine codex-inspired terminal layout"
  ```

## Task 6: Protocol Regression And Terminal Acceptance

**Files:**

- Modify product/test files only if a newly discovered defect receives a fresh
  failing test and follows another RED/GREEN cycle.
- Update this packet's checkboxes and evidence only through the assigned plan
  coordinator.

**Interfaces:**

- Verifies that presentation changes did not alter the two-event protocol or
  the single message command surface.

- [ ] **Step 1: Run formatting**

  ```powershell
  uv run black --check src/athena_tui test/unit/athena_tui
  ```

  Expected: exit 0 with no reformatted files required.

- [ ] **Step 2: Run the complete TUI and protocol regression**

  ```powershell
  uv run pytest test/unit/athena_tui test/unit/test_tui.py test/integration/test_tui_protocol.py -q -p no:cacheprovider
  ```

  Expected: all tests pass. `test_tui_protocol.py` continues proving that the
  only event kinds are `output` and `state`, and controller tests continue
  proving that messages use the single runtime command surface.

- [ ] **Step 3: Run the Supervisor-facing regression slice**

  ```powershell
  uv run pytest test/integration/research/test_prepare_agent_contract.py test/unit/research/supervisor/test_events.py test/unit/athena_tui test/integration/test_tui_protocol.py -q -p no:cacheprovider
  ```

  Expected: all tests pass without skips in the target TUI protocol slice.
  PREPARE's chunked structured-response fixture still yields exactly one
  readable reason, and local state still retains a distinct entry for every
  accepted `OutputEvent`.

- [ ] **Step 4: Verify the API boundary by changed-file inventory**

  ```powershell
  git diff --name-only HEAD~5..HEAD
  git diff -- src/athena_tui/controller.py src/athena/research src/athena/core
  ```

  Expected: commits from this packet contain only the owned TUI presentation
  and test files. The second command is empty relative to the packet's starting
  commit. If Task 12 changed these files before this packet started, record the
  packet starting commit and compare against that exact commit instead of
  assuming `HEAD~5`.

- [ ] **Step 5: Interactive terminal acceptance**

  In an actual interactive terminal, run the existing entrypoint against a
  disposable Athena project:

  ```powershell
  uv run Athena-tui --project examples/tui-visual-acceptance
  ```

  Verify all of the following without changing runtime data or interfaces:

  ```text
  80+ columns: full path, search attempts/successes/workers, and SOTA fit.
  40 columns: phase and status remain visible; secondary details disappear.
  Runtime output appears only in the upper pane.
  PREPARE shows exactly one readable line/block containing:
  "Baseline, predictions, report, and manifest are all present and validated."
  PREPARE does not show token rows or private JSON keys such as "suggestions".
  Two successive Agent turns for plan "prepare" remain separate history entries;
  marker suppression may make them visually continuous but text is never joined.
  Mouse wheel over the upper pane scrolls three visual lines per notch.
  Mouse wheel over the composer does not move output history.
  Enter submits one complete message.
  Shift+Enter inserts a newline when the terminal emits modified Enter.
  Ctrl+J inserts a newline on every supported terminal.
  PageUp stops tail follow without freezing runtime output ingestion.
  New output count increases while scrolled up.
  End returns to the newest output and clears the count.
  Help and confirmation preserve the draft and keep history visible.
  RUNNING, WAITING, COMPLETED, STOPPED, and error states remain readable
  without relying on color.
  No Chinese mojibake is visible.
  ```

  If the local terminal does not distinguish Shift+Enter from Enter, record
  that terminal limitation and verify that the footer presents Ctrl+J as the
  working fallback. Do not reinterpret plain Enter as newline.

- [ ] **Step 6: Run the repository deterministic suite required by the active plan**

  Use the exact full-suite command in `codex_docs/CURRENT.md` after this packet
  becomes active. At the time this packet was written, the expected command is:

  ```powershell
  uv run pytest -q -p no:cacheprovider
  ```

  Expected: exit 0. Any failure caused by these changes receives its own
  focused RED test before a fix.

- [ ] **Step 7: Commit verification-only fixes if required**

  If no fix was required, do not create an empty commit. If a TDD fix was
  required, stage only its owned files and use:

  ```powershell
  git commit -m "fix(tui): address terminal acceptance regression"
  ```

## Acceptance Criteria

- [ ] Current `AthenaApp`, `TuiController`, and `ResearchRuntime` composition is
  preserved.
- [ ] No interface, event schema, subscription behavior, or runtime command
  surface changes.
- [ ] Every new runtime output record is appended to the upper output pane and
  never mutates the composer draft.
- [ ] The reported PREPARE reproduction emits exactly one visible readable
  reason: `Baseline, predictions, report, and manifest are all present and
  validated.` No provider token, partial JSON, `decision`, or `suggestions`
  fragment appears as a separate Agent row.
- [ ] Punctuation, spaces, Unicode, and line endings are preserved or normalized
  only by the documented projection rule; no spaces are invented around commas,
  periods, braces, or subword chunks.
- [ ] Failed and interrupted PREPARE turns do not publish partial structured
  responses as prose; already completed tool records remain independently
  visible.
- [ ] `apply_output` maps each accepted `OutputEvent` to exactly one runtime
  `HistoryEntry`; adjacent Agent events sharing source/channel/plan are not
  concatenated because those fields do not identify a turn.
- [ ] User submission immediately appears in upper history with a `›` marker.
- [ ] Agent/supervisor text, tool output, and errors have distinct text/symbol
  treatment; repeated source markers are suppressed for consecutive text.
- [ ] Tail follow, PageUp/PageDown, output-pane-only mouse wheel, unseen count,
  and End behavior have fresh automated evidence.
- [ ] Composer accepts multiline text, grows from one to six rows, and scrolls
  internally beyond six rows.
- [ ] Enter submits; modified Shift+Enter inserts a newline where detectable;
  Ctrl+J always inserts a newline.
- [ ] Send failure restores the exact multiline draft and surfaces a readable
  local error.
- [ ] Header/footer preserve phase and status at 40 columns and add progress,
  workers, and SOTA at wider widths only when those fields exist.
- [ ] Help and confirmation preserve draft/history and cannot leak decision
  keystrokes into the composer.
- [ ] Chinese operation text is valid UTF-8 and technical states remain English.
- [ ] Focused, protocol, Supervisor-event, formatter, and full deterministic
  regressions have fresh passing evidence.

## TDD Evidence Template

The assigned implementer returns this exact evidence per task:

```text
Task:
Starting commit:
Owned files:
RED command:
RED failure and why it proves the missing behavior:
GREEN command:
GREEN result/count:
Regression command:
Regression result/count:
Commit:
Interface audit: unchanged output/state + message(text), yes/no
```

## Plan Self-Review

- Spec coverage: output separation, tail following, unseen output, multiline
  composer, Enter/Shift+Enter/Ctrl+J, responsive layout, semantic styling,
  status/search/SOTA, overlays, confirmation, mojibake repair, PREPARE semantic
  output atomicity, and protocol preservation each map to an explicit gate or
  RED/GREEN task.
- Scope: implementation remains one subsystem (`src/athena_tui`). Task 0 only
  verifies the separately owned runtime projection completed by active-plan
  T13-P2; it never edits runtime or provider code.
- Type consistency: Task 1's `HistoryEntry` and `unseen_output_count` are the
  exact names consumed by Tasks 2 and 3; Task 2's `composer_height` is consumed
  by Task 4.
- Placeholder scan: no unresolved choice, deferred implementation step, or
  unnamed test command remains. Ellipses that remain inside
  `tuple[Type, ...]` are Python's required variable-length tuple syntax, not
  omitted work.
- Ownership: this packet creates no claim and cannot run until the active plan
  finishes T13-P2, all overlapping owners release the files, and the coordinator
  makes this packet active through `codex_docs/CURRENT.md`.
