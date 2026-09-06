# Fresh Verification Evidence

Only results produced from the final worktree belong here.

## Gate commands

```powershell
uv run pytest -q test/unit/research test/integration/research --tb=short
uv run pytest -q test/unit/execution/test_execution_runtime.py
uv run pytest -q tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py tests/test_gui_gateway_e2e.py tests/test_gui_gateway_main.py tests/test_gui_gateway_transport_import.py tests/test_gui_protocol_contract.py --tb=short
uv run python -m compileall -q src/athena/research src/athena/execution src/gui_gateway
git diff --check

Set-Location athena-gui
npm test -- --run
npm run build

Set-Location src-tauri
cargo test
```

Every command above exited with code 0. The Rust test command compiled the native
crate before running its seven tests.

## Python

- Research unit and integration suites: `710 passed in 130.10s`.
- GUI gateway transport, production Runtime WebSocket round trip, handler, main, and
  protocol contract: `53 passed in 6.27s`.
- Execution runtime regression suite: `33 passed in 13.03s`.
- Python compile check for research, execution, and GUI gateway modules: passed.
- Global `git diff --check`: passed.
- The gateway suite emitted one known Windows asyncio event-loop-closed teardown
  warning; it did not fail a test.

## Frontend and native bridge

- Vitest: 14 files, 78 tests passed.
- TypeScript production build (`tsc && vite build`): passed; 2759 modules transformed.
- Rust/Tauri: 7 tests passed with six dead-code warnings for validation-only request
  structures and helpers.
- Native Rust bridge -> spawned Python gateway -> WebSocket round trip: passed.
- `resources/gui_gateway.exe` is currently a zero-byte test placeholder. The Rust tests
  prove the bridge protocol, not a distributable bundled Windows sidecar.

## JW-SSD production smoke

- Project: `../task3/athena-jw-ssd-run-final`.
- Terminal state: `COMPLETED / COMPLETED`.
- Formal SEARCH budget: 2/2 attempts, one success.
- Selected experiment: `exp_hyp_532038e014e8`.
- Frozen SEARCH macro-F1: `0.562185`.
- Selected SEARCH commit: `57812ae9976203b39e1ff49e9f58ccb7ef80f9a6`.
- FINAL macro-F1: `0.20987654320987656`.
- Generalization gap: `0.35230845679012346`; warning: `true`.
- VALIDATE commit: `fe7e98dc7ae53320fc613c624590037904caf10a`.
- Evidence ref: `sha256:9ab5a5d2ba978c569bf1f07d7a34af30ceea25a9b564e5e9aa969de5156050fc`.
- Report ref: `sha256:27738313bed87f8b738131df34a75d951dafd3421f9862f4aa8e42ba7ef5e599`.
- Predictions ref: `sha256:a277eb1b6a1658e52ffea6701e7b72fc78c53b5727f8c44824213e3a145e2fa7`.
- TUI opened the saved project in a PTY, displayed the terminal state and 2/2 budget,
  and exited normally without starting another SEARCH attempt.
- A second final TUI audit exited with code 0; CLI status before and after remained
  `COMPLETED / COMPLETED`, attempts 2/2, successes 1, and SOTA `0.562185`.

The project state, final evidence, report, and prediction manifests pass their SHA256
self-checks. An earlier FAILED result artifact is an intermediate pre-final-evaluator
snapshot; terminal status comes from the final state and evidence for the same result.
The tracked `REPORT.md` at the selected experiment commit describes SEARCH. FINAL
metrics are authoritative in the content-addressed report/evidence refs above.

## Dataset contract

- `images/continuum`: 520 PNG files across five classes.
- `images/magnetogram`: 520 PNG files across five classes.
- All 520 HARPNUM/timestamp observations have exactly two modalities; there are 21
  active-region groups.
- Group-disjoint TRAIN/SEARCH/FINAL observations: 383/50/87.

## Known limits

- SEARCH reports five-class macro-F1, while the frozen FINAL evaluator covers the three
  classes present in its split; these values are recorded faithfully but estimate
  different label supports.
- The agent workflow provides process isolation and frozen artifacts, not a hostile
  sandbox or blind benchmark: an agent can inspect raw class paths and evaluator files.
- The frozen classifier is reproducible on this machine. Its generated `solution/data.py`
  contains the current dataset's absolute path and an unsorted directory enumeration,
  so it is not claimed as a bit-for-bit portable cross-machine package.
