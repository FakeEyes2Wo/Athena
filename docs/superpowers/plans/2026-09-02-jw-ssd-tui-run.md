# JW-SSD TUI Search-3 Run Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add minimal TUI budget/validation flags, persist a dataset-specific JW-SSD task prompt, and start a fresh interactive SEARCH run with budget three and VALIDATE disabled.

**Architecture:** `athena_tui.entrypoint` remains the sole TUI composition root and forwards two parsed values directly to `ResearchRuntime`. Dataset semantics stay in `../task3/init_prompt.md`, not in Athena production code. The run uses a fresh sibling project directory so existing JW-SSD experiments are not resumed.

**Tech Stack:** Python 3.11+, argparse, pytest, prompt_toolkit TUI, Athena `ResearchRuntime`.

## Global Constraints

- Follow `docs/代码规范.md`.
- Preserve unrelated dirty worktree changes.
- Add no configuration layer, framework, compatibility shim, or dataset-specific production module.
- SEARCH budget for this run is exactly `3`.
- `--validate` is optional and defaults to false.
- Split by HARPNUM; never split adjacent frames independently.
- Primary metric is five-class macro-F1.
- Do not delete a partial run directory if startup or model execution fails.
- Write `../task3/init_prompt.md` before launching the TUI.

---

### Task 1: TUI runtime options

**Files:**
- Modify: `src/athena_tui/entrypoint.py`
- Modify: `test/unit/athena_tui/test_entrypoint.py`

**Interfaces:**
- Consumes: `ResearchRuntime(project_root=..., search_limit: int | None, auto_validate: bool, ...)`
- Produces: `_parse(argv)` fields `search_limit: int` and `validate: bool`.

- [x] **Step 1: Add failing parser and wiring tests**

Extend the default parser test and add explicit option coverage:

```python
def test_parse_default_project() -> None:
    args = entrypoint._parse([])
    assert args.project == ".athena/tui-run"
    assert args.search_limit == 10
    assert args.validate is False


def test_parse_search_limit_and_validate() -> None:
    args = entrypoint._parse(["--search-limit", "3", "--validate"])
    assert args.search_limit == 3
    assert args.validate is True


def test_parse_rejects_non_positive_search_limit() -> None:
    with pytest.raises(SystemExit):
        entrypoint._parse(["--search-limit", "0"])
```

Parse `--search-limit 3` in the runtime wiring test and assert:

```python
assert seen.get("search_limit") == 3
assert seen.get("auto_validate") is False
```

- [x] **Step 2: Run the focused test and confirm it fails**

Run `.venv\Scripts\python.exe -m pytest -q test/unit/athena_tui/test_entrypoint.py`.
Expected: missing parsed fields and auto-validation still enabled.

- [x] **Step 3: Implement direct argparse forwarding**

Add:

```python
def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("search limit must be at least 1")
    return parsed
```

Register:

```python
parser.add_argument("--search-limit", type=_positive_int, default=10)
parser.add_argument("--validate", action="store_true", help="SEARCH 后自动运行 VALIDATE")
```

Forward:

```python
search_limit=args.search_limit,
auto_validate=args.validate,
```

- [x] **Step 4: Run focused and integration tests**

Run `.venv\Scripts\python.exe -m pytest -q test/unit/athena_tui/test_entrypoint.py test/integration/test_tui_protocol.py`.
Expected: all pass.

- [x] **Step 5: Format and commit only Task 1 files**

Run Black on the two files, run `git diff --check` on them, then commit only those two paths with message `feat: expose TUI search and validation options`.

---

### Task 2: Reusable JW-SSD task prompt

**Files:**
- Create: `../task3/init_prompt.md`

**Interfaces:**
- Consumes: `../task3/JW-SSD_Dataset`, competition metric and prediction-format documents.
- Produces: one complete Human task message suitable for TUI paste and repeated runs.

- [ ] **Step 1: Write the task prompt**

The file must name the absolute dataset path; paired continuum/magnetogram PNG inputs;
the five labels; HARPNUM-grouped train/SEARCH/FINAL isolation; five-class macro-F1;
balanced accuracy, per-class recall, and confusion matrix; the complex-class binary
fold and TSS/HSS metrics; `evaluate/predictions__JWSSD_MW5.csv` with class probability
columns; `evaluate/metrics_public_test.csv`; transfer learning instead of a toy CNN;
and persistent files for data manifests, EDA, papers, experiments, metrics, and errors.

- [ ] **Step 2: Verify the prompt contract**

Run:

```powershell
rg -n "JW-SSD_Dataset|HARPNUM|macro-F1|TSS|HSS|predictions__JWSSD_MW5.csv|metrics_public_test.csv|迁移学习|写入文件" ..\task3\init_prompt.md
```

Expected: every required contract appears.

---

### Task 3: Fresh interactive TUI run

**Files:**
- Runtime output: `../task3/athena-jw-ssd-tui-search3/`
- Input: `../task3/init_prompt.md`

**Interfaces:**
- Consumes: `Athena-tui --project --search-limit [--validate]` and the prompt file.
- Produces: a live TUI session plus persisted Athena state, artifacts, logs, and workspaces.

- [ ] **Step 1: Confirm the target is safe and fresh**

Resolve `../task3/JW-SSD_Dataset` and test
`../task3/athena-jw-ssd-tui-search3/.athena/state.json`. The dataset must exist and the
state file must not. If state exists, use the next unused suffixed run directory; do
not delete it.

- [ ] **Step 2: Start the interactive TUI**

Run in a PTY:

```powershell
.venv\Scripts\Athena-tui.exe --project ..\task3\athena-jw-ssd-tui-search3 --search-limit 3
```

Do not pass `--validate`.

- [ ] **Step 3: Submit the reusable prompt**

Paste the complete UTF-8 content of `../task3/init_prompt.md` into the TUI input and
press Enter once. Do not send a shortened paraphrase.

- [ ] **Step 4: Verify the run started with the requested contract**

Required evidence: phase is PREPARE or SEARCH, `search.limit=3`, auto-validation is
false from the tested runtime wiring, and persisted task text contains `JW-SSD_Dataset`
and `HARPNUM`.

- [ ] **Step 5: Record handoff status**

Report the prompt path, run project path, process/session status, current phase,
SEARCH budget, and validation setting. Do not claim SEARCH completed until three
experiments have actually settled.
