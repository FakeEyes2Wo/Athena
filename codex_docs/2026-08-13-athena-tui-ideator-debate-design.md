# Athena TUI Ideator Debate Board Design

**Status:** APPROVED FOR AUTONOMOUS INCREMENTAL DEVELOPMENT

**Goal:** Show live output from the actual Ideators in an adaptive debate
board: one Ideator uses one lane, two use two equal lanes, three use three
equal lanes, and no more than three Ideators are launched or displayed.

## Runtime Contract

`ResearchRuntime._run_ideator_turn(count)` launches
`min(3, max(1, count))` independent Ideator Agents concurrently. The requested
hypothesis count is distributed as evenly as possible across them. For
example, `count=4` produces targets `2, 1, 1`; `count=2` produces `1, 1`.

Each Agent keeps its own AgentRuntime thread and receives the stable display
label `ideator-1`, `ideator-2`, or `ideator-3`. Existing `run_events()` output
is projected through the existing two-event TUI protocol with that label in
`OutputEvent.plan`; no event kind or schema field is added.

Successful structured `HypothesisBatch` results are merged in lane order and
truncated to the Supervisor's requested count. A failed Ideator publishes a
readable error in its lane while successful peers still contribute results.
The turn fails only if every Ideator fails or all successful batches are empty.

## TUI Contract

The normal output history remains the only scroll owner. Entries whose plan is
`ideator-N` are grouped into a debate board rendered after ordinary user,
Supervisor, and non-Ideator tool output. This avoids duplicating stream chunks
and preserves all non-debate information.

The board uses exactly the number of distinct Ideator labels present, capped
at three:

- one Ideator: one full-width lane;
- two Ideators: two equal-width lanes;
- three Ideators: three equal-width lanes;
- more than three labels: only `ideator-1` through `ideator-3` are selected.

At widths where every lane can retain at least 24 terminal cells, lanes render
side by side with a one-cell separator. At narrower widths, the same selected
lanes render vertically at full width. There are no empty placeholder lanes.

Each lane has a stable `Ideator N` title and contains that Agent's text, tool
output, and error records in arrival order. Dynamic text remains plain text;
it is never parsed as style markup. Cell-width-aware padding and wrapping keep
every rendered row within the supplied terminal width, including Chinese and
wide Unicode characters.

## Scrolling And State

The debate board returns ordinary immutable visual lines from
`render_history_lines`. Existing history slicing therefore owns wheel,
PageUp/PageDown, End, tail following, unseen output, and resize behavior.
No lane has a second scroll offset and mouse input does not need a new route.

## Verification

- Runtime tests prove requested counts 1, 2, and 4 launch 1, 2, and 3 Agents,
  distribute targets deterministically, project stable lane labels, merge
  successful batches, and tolerate one failed peer.
- Renderer tests prove exact one/two/three-lane selection, equal widths,
  cap-at-three behavior, narrow stacking, Unicode width bounds, and retention
  of ordinary output.
- Application regression proves board lines use the existing output viewport
  and its wheel/tail state rather than a separate layout path.
- Protocol regression proves subscriber records remain only `output` and
  `state`.

## Owned Files

- Modify: `src/athena/research/runtime.py`
- Modify: `src/athena_tui/render.py`
- Test: `test/unit/research/test_runtime_ideators.py`
- Test: `test/unit/athena_tui/test_render.py`
- Test: `test/unit/athena_tui/test_app.py` only if viewport integration needs
  additional evidence

The Agent kernel, `OutputEvent`, `StateEvent`, controller, Supervisor policy,
and persistence schemas remain unchanged.
