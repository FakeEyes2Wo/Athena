# TUI ASCII logo completion

Completed on 2026-10-13, against main at `3f3dc535`.

## Reference and behavior

The user clarified that the intended reference is PR #35's owl and branch,
stored in `athena-gui/public/athena-logo-{light,dark}.png`. That PR removed the
previous SVG. The TUI now renders an ASCII adaptation of the owl and branch
with gold eyes in its empty history pane.

- Normal terminals show the full mark; smaller panes use a compact owl and
  branch. Short panes show the Athena wordmark.
- The welcome mark disappears as soon as history output arrives. It is not
  part of the history model or copied history selection.
- The implementation changes only `src/athena_tui/render.py` and
  `src/athena_tui/app.py`, plus this report and `codex_docs/CURRENT.md`.
- Rendering uses static ASCII and existing terminal styles, without loading
  image files or adding dependencies.

## Verification

- Baseline: `tests/unit/athena_tui` passed, 78 tests.
- Final regression run: `.venv\Scripts\python.exe -m pytest -q
  tests/unit/athena_tui tests/integration/test_tui_resume.py
  tests/integration/test_tui_protocol.py` passed, 98 tests in 4.83 seconds.
- An ignored local headless harness rendered the actual application layout
  at 80x24, 40x16, 24x10 and 80x9. It verified sizing, composer focus, empty
  history selection, and transition from welcome to conversation output.
- Resizing from 80x24 to 24x10 and back selected the appropriate welcome
  layout. A sweep of widths 1–80 and heights 1–24 verified ASCII-only output
  fitting the supplied dimensions.
- Visually inspected the generated full, compact, and short-pane previews.
  Local preview files are under ignored `build/tui-logo-*.png`.
- Black and `git diff --check` passed. The repository style checker reported
  only two pre-existing nested-callback docstring warnings in `render.py`;
  comparison with HEAD confirmed no new findings.
- Independent read-only subagent review found no concrete integration,
  resizing, or selection defect.

The completed implementation plan was removed and CURRENT now links here.
Unrelated worktree changes were preserved.
