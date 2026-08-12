# Athena TUI Output Mouse-Wheel Design

**Status:** APPROVED

**Goal:** Let users scroll the upper output pane with the mouse wheel while
preserving the existing PageUp/PageDown/End behavior, tail following, unseen
output count, composer editing, and two-event runtime protocol.

## Interaction Contract

- Mouse-wheel input is handled only while the pointer is inside the upper
  output window. The composer, help overlay, confirmation pane, header, and
  footer do not scroll output history.
- One wheel notch moves three rendered visual lines. Keyboard PageUp and
  PageDown continue moving one visible page, and End continues returning
  directly to the newest output.
- Scrolling upward disables tail following. Runtime output still appends to
  immutable history, the visible viewport stays frozen, and the unseen-output
  count increases for each new unique output event.
- Scrolling downward clamps at the newest output. Reaching offset zero
  re-enables tail following and clears the unseen-output count through the
  existing `set_history_follow` state transition.
- Wheel input at the oldest or newest boundary is idempotent and never creates
  a negative or out-of-range scroll offset.

## Architecture

Keep `_history_scroll` as the single visual-line offset owned by `AthenaApp`.
Extract small synchronous `_scroll_history_up(lines)` and
`_scroll_history_down(lines)` methods so keyboard and mouse paths share the
same clamping and follow-state behavior.

The output `FormattedTextControl` will use a local subclass whose
`mouse_handler(MouseEvent)` handles only `MouseEventType.SCROLL_UP` and
`MouseEventType.SCROLL_DOWN`, invokes the supplied callbacks with a three-line
step, and returns `NotImplemented` for all other mouse events. Attaching the
handler at control level makes the whole output window responsive, including
blank cells that contain no formatted-text fragment. The `Application` enables
prompt-toolkit mouse support; no global ANSI mapping or dependency is changed.

## Error And Boundary Behavior

Mouse events cannot alter runtime data, the composer draft, focus, overlays,
or confirmation state. If output does not exceed the viewport, either wheel
direction leaves offset zero and tail following enabled. Resize behavior keeps
using the existing visual-line clamp.

## Verification

Automated tests construct real prompt-toolkit `MouseEvent` values and call the
actual output control. They prove that wheel-up moves exactly three visual
lines and disables tail following, wheel-down reaches zero and resumes tail
following, non-scroll mouse events return `NotImplemented`, and composer-area
controls do not receive the output handler. The focused TUI regression and
interactive terminal acceptance then verify mouse reporting and rendering in
the complete application.

## Scope

Owned implementation files are `src/athena_tui/app.py` and
`test/unit/athena_tui/test_app.py`, as part of Task 3 in
`codex_docs/2026-08-12-athena-tui-beautification-tdd.md`. Runtime interfaces,
event schemas, controller behavior, persistence, and provider code are out of
scope.
