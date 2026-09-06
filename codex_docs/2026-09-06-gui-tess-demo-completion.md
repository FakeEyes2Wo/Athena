# TESS GUI demonstration and folder browser

## Delivered

- Browser-mode Browse now navigates host directories through the local gateway;
  Tauri continues using its native picker. Permission errors remain visible.
- Visible Edge GUI at http://localhost:1421 opened the completed project at
  `C:/Users/80163/Desktop/挑战杯_2026/test/tess_final_comparison/athena`.
- Restored original 0905 root/ancestry in the demo's merged tree. The shared
  0905 SOTA / 0906 baseline now has five 0906 children. GUI displays all 20
  experiments with date-prefixed labels. Raw daily snapshots remain untouched.
- Existing 19 experiments retain identical evaluation, status, artifact and
  commit fields. The restored root comes from the original 0905 snapshot.
- Package README, merge manifest, GUI_DEMO.png and GUI_TREE.png describe/show
  the demo. These files live outside the Athena repository.

## Evidence

- Actual GUI navigation: Browse → athena → Select; completed status and SEARCH
  0.9034; tree, experiments, and generated report with FINAL 0.9000 and the
  original generalization warning. After lineage correction: 20 tree nodes.
- `npm test -- --run src/components/shell/__tests__/WorkspacePicker.test.tsx`:
  6 passed (worker).
- workspaceDialog tests: 4 passed; Python workspace/project tests: 3 passed;
  protocol contract tests: 3 passed (worker).
- `npm run build` in athena-gui: TypeScript and production Vite build passed.
- `git diff --check`: passed.
- Live gateway tree validated: no missing parents/cycles; all five 0906
  experiments descend directly from the 0905 SOTA bridge.

## Limits and operation

No training or FINAL evaluation was run. Dataset is not bundled, Linux paths
are historical, and two old RUNNING records do not represent live Windows jobs.
For a fresh GUI launch with ports free, run `npm run dev:web` in athena-gui,
then choose the nested athena directory. Report → Generate report reads saved
results; it does not rerun evaluation.

## Integration

Code delivered on main and pushed as `88d86f5`; no task branch was created,
so none needs deletion.
Preserved unrelated ATHENA_ERRORS.md and pr15_source.diff changes. Cleanup of
`.tmp-gui-demo-tools` was rejected by execution policy; helpers remain untracked.
The completed implementation plan was removed and CURRENT now links here.
