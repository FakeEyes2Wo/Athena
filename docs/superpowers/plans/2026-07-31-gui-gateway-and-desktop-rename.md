# GUI Gateway And Desktop Rename Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the Python desktop adapter to the top-level `gui_gateway` package and rename the Tauri/React application from `athena-ide` to `athena-gui` without changing runtime behavior.

**Architecture:** `gui_gateway` remains a thin local WebSocket boundary over `athena.research.ResearchRuntime`; it owns no research state. The renamed `athena-gui` Tauri application starts that package with `python -m gui_gateway` and otherwise preserves the existing RPC and event flow.

**Tech Stack:** Python 3.11, Hatchling, pytest, websockets, React 18, TypeScript, Vite, Tauri 2, Rust 2021, Cargo.

## Global Constraints

- Move `src/athena/ide/` to `src/gui_gateway/`; do not retain an `athena.ide` compatibility package.
- Rename `IDEHandler` to `GuiRequestHandler`; keep `WebSocketTransport` unchanged.
- Move `athena-ide/` to `athena-gui/` and update npm, Cargo, Rust library, and Tauri identifiers.
- Rename `scripts/dev-ide.*` to `scripts/dev-gui.*`.
- Preserve RPC payloads, `ResearchRuntime` ownership, event behavior, and UI behavior.
- Do not rewrite historical files under `docs/superpowers/` other than this plan and its approved design spec.
- Preserve unrelated staged and unstaged work; every task commit must use explicit pathspecs.

---

### Task 1: Rename The Python Adapter To `gui_gateway`

**Files:**
- Move: `src/athena/ide/__init__.py` -> `src/gui_gateway/__init__.py`
- Move: `src/athena/ide/__main__.py` -> `src/gui_gateway/__main__.py`
- Move: `src/athena/ide/handler.py` -> `src/gui_gateway/handler.py`
- Move: `src/athena/ide/transport.py` -> `src/gui_gateway/transport.py`
- Modify: `pyproject.toml`
- Move: `tests/test_ide_handler.py` -> `tests/test_gui_gateway_handler.py`
- Move: `tests/test_ide_transport.py` -> `tests/test_gui_gateway_transport.py`
- Move: `tests/test_ide_e2e.py` -> `tests/test_gui_gateway_e2e.py`
- Move: `tests/test_ide_transport_import.py` -> `tests/test_gui_gateway_transport_import.py`

**Interfaces:**
- Consumes: `athena.research.ResearchRuntime` and `athena.app_server.protocol` envelopes.
- Produces: `gui_gateway.GuiRequestHandler`, `gui_gateway.__main__.start_server(test_mode: bool = False, runtime: ResearchRuntime | None = None)`, and `python -m gui_gateway`.

- [ ] **Step 1: Rename the tests and change them to the target imports**

Use `git mv` for the four test files. Replace imports as follows:

```python
from gui_gateway.handler import GuiRequestHandler
from gui_gateway.__main__ import start_server
```

In the subprocess import test, replace `import athena.ide.transport` with `import gui_gateway.transport`. Replace `IDEHandler(...)` constructions with `GuiRequestHandler(...)` and update test names that contain `ide_handler`.

- [ ] **Step 2: Run the target tests and verify the new package is absent**

Run:

```powershell
uv run pytest tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py tests/test_gui_gateway_e2e.py tests/test_gui_gateway_transport_import.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'gui_gateway'`.

- [ ] **Step 3: Move the package and update its public names**

Use `git mv src/athena/ide src/gui_gateway`. In `handler.py`, expose:

```python
class GuiRequestHandler:
    def __init__(self, runtime: ResearchRuntime) -> None:
        self._runtime = runtime

    async def dispatch(self, method: str, params: dict) -> dict:
        if method == "ping":
            return {"pong": True}
        return await self._runtime.dispatch(method, params)
```

Update `__main__.py` to import `GuiRequestHandler` and `WebSocketTransport` from `gui_gateway`, instantiate `GuiRequestHandler`, document `python -m gui_gateway`, and preserve the current `start_server` signature. Update the `TYPE_CHECKING` import and annotation in `transport.py` to `GuiRequestHandler`.

- [ ] **Step 4: Include both top-level packages in the wheel**

Change the Hatch configuration in `pyproject.toml` to:

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/athena", "src/gui_gateway"]
```

- [ ] **Step 5: Run the Python gateway tests**

Run:

```powershell
uv run pytest tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py tests/test_gui_gateway_e2e.py tests/test_gui_gateway_transport_import.py -q
uv run python -c "import gui_gateway; from gui_gateway.__main__ import start_server"
```

Expected: all four test files pass and the import command exits with code 0.

- [ ] **Step 6: Commit only the Python package migration**

```powershell
git commit --only -m "refactor(gui): rename Python gateway package" -- pyproject.toml src/athena/ide src/gui_gateway tests/test_ide_handler.py tests/test_ide_transport.py tests/test_ide_e2e.py tests/test_ide_transport_import.py tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py tests/test_gui_gateway_e2e.py tests/test_gui_gateway_transport_import.py
```

---

### Task 2: Rename The Desktop Application And Startup Chain

**Files:**
- Move: `athena-ide/` -> `athena-gui/`
- Modify: `athena-gui/package.json`
- Modify: `athena-gui/package-lock.json`
- Modify: `athena-gui/src-tauri/Cargo.toml`
- Modify: `athena-gui/src-tauri/Cargo.lock`
- Modify: `athena-gui/src-tauri/tauri.conf.json`
- Modify: `athena-gui/src-tauri/src/lib.rs`
- Modify: `athena-gui/src-tauri/src/main.rs`
- Modify: `athena-gui/src-tauri/src/python/bridge.rs`
- Move: `scripts/dev-ide.bat` -> `scripts/dev-gui.bat`
- Move: `scripts/dev-ide.sh` -> `scripts/dev-gui.sh`

**Interfaces:**
- Consumes: the `python -m gui_gateway` entry point from Task 1.
- Produces: the `athena-gui` npm/Cargo/Tauri application and `scripts/dev-gui.*` launchers.

- [ ] **Step 1: Record the current desktop validation baseline**

Run in `athena-ide/`:

```powershell
npm test
npm run build
```

Run in `athena-ide/src-tauri/`:

```powershell
cargo check
```

Expected: record the exact pass/fail baseline before any move; do not attribute pre-existing failures to the rename.

- [ ] **Step 2: Move the application and launch scripts**

Use:

```powershell
git mv athena-ide athena-gui
git mv scripts/dev-ide.bat scripts/dev-gui.bat
git mv scripts/dev-ide.sh scripts/dev-gui.sh
```

- [ ] **Step 3: Update application metadata and Rust library names**

Apply these exact mappings in package manifests and lockfiles:

```text
athena-ide      -> athena-gui
athena_ide_lib  -> athena_gui_lib
com.athena.ide  -> com.athena.gui
```

Update Rust calls in `src/lib.rs` and `src/main.rs` to the new library identifier. Keep the user-visible product name and window title as `Athena`.

- [ ] **Step 4: Update the Python bridge**

In `athena-gui/src-tauri/src/python/bridge.rs`, change the child process arguments to:

```rust
.args(["run", "python", "-m", "gui_gateway"])
```

Keep the two-level repository-root calculation and update comments to `src-tauri -> athena-gui -> repository root`.

- [ ] **Step 5: Update the launch scripts**

In both scripts, change the preflight import to `from gui_gateway.__main__ import start_server`, change the application directory to `athena-gui`, update displayed text from `IDE` to `GUI`, and update referenced test names to `test_gui_gateway_*`.

- [ ] **Step 6: Validate the renamed desktop application**

Run in `athena-gui/`:

```powershell
npm test
npm run build
```

Run in `athena-gui/src-tauri/`:

```powershell
cargo check
```

Expected: results are at least as good as the recorded baseline, with no path, crate, or module resolution failures caused by the rename.

- [ ] **Step 7: Commit only the desktop migration**

```powershell
git commit --only -m "refactor(gui): rename desktop application" -- athena-ide athena-gui scripts/dev-ide.bat scripts/dev-ide.sh scripts/dev-gui.bat scripts/dev-gui.sh
```

---

### Task 3: Update Current Documentation And Verify The Migration

**Files:**
- Modify: `README.md`
- Modify: `docs/README.md`
- Modify: `docs/architecture/current.md`
- Modify: `docs/architecture/target.md`
- Modify: `docs/architecture/research-runtime-v2.md`
- Modify: `docs/冗余设计.md`

**Interfaces:**
- Consumes: final paths and names from Tasks 1 and 2.
- Produces: current documentation with no live references to the removed names.

- [ ] **Step 1: Update current documentation**

Replace live architecture and command references using these mappings:

```text
src/athena/ide/  -> src/gui_gateway/
athena.ide       -> gui_gateway
IDEHandler       -> GuiRequestHandler
athena-ide/      -> athena-gui/
dev-ide          -> dev-gui
```

Do not modify earlier plans or specs under `docs/superpowers/`.

- [ ] **Step 2: Search active files for stale names**

Run:

```powershell
rg -n --hidden --glob '!.git/**' --glob '!.claude/worktrees/**' --glob '!docs/superpowers/**' --glob '!node_modules/**' --glob '!target/**' "athena\.ide|src[/\\]athena[/\\]ide|athena-ide|IDEHandler|dev-ide"
```

Expected: no matches in active source, tests, scripts, manifests, lockfiles, or current documentation.

- [ ] **Step 3: Run the full migration validation**

Run:

```powershell
uv run pytest tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py tests/test_gui_gateway_e2e.py tests/test_gui_gateway_transport_import.py -q
uv run python -c "import gui_gateway; from gui_gateway.__main__ import start_server"
git diff --check
```

Then run `npm test` and `npm run build` in `athena-gui/`, followed by `cargo check` in `athena-gui/src-tauri/`.

Expected: all commands exit with code 0, subject only to explicitly recorded pre-existing baseline failures.

- [ ] **Step 4: Commit only current documentation**

```powershell
git commit --only -m "docs(gui): update gateway and desktop paths" -- README.md docs/README.md docs/architecture/current.md docs/architecture/target.md docs/architecture/research-runtime-v2.md docs/冗余设计.md
```

- [ ] **Step 5: Inspect the final task diff and status**

Run:

```powershell
git status --short
git log --oneline -n 5
```

Expected: unrelated pre-existing changes remain untouched; all migration files are committed, and no migration-specific untracked or modified paths remain.
