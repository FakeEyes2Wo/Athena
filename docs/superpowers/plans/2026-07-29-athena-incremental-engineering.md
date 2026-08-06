# Athena Compatibility-First Engineering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the Python quality gate and classify the highest-risk redundancy findings without breaking Athena's public APIs or wire contracts.

**Architecture:** Apply one isolated runtime-annotation fix, then treat redundancy findings as contract audits rather than deletion targets. Preserve public imports and incompatible implementations, record evidence-backed dispositions, and finish with full Python and frontend gates.

**Tech Stack:** Python 3.11, pytest, Pydantic 2, TypeScript 5, React 18, Vitest, Vite, Git.

## Global Constraints

- Do not revert or rewrite unrelated changes in the existing dirty worktree.
- Keep `BaseAgent`, `create_code_agent`, `Comparator`, compatibility re-export modules, ResearchTree v1, App Server, IDE RPC, and Tauri contracts.
- Preserve field names, defaults, serialized shapes, exception types, and event protocols.
- A deletion requires zero production, test, example, and public-export references proven by `rg`.
- Write and observe a correctly failing regression test before changing production code.
- Do not add dependencies, abstractions, wrapper DTOs, or speculative configuration.
- Minimum final gates are `uv run pytest -q`, `npm test -- --run`, `npm run build`, and `git diff --check`.

---

## File Map

- `src/athena/ide/transport.py`: WebSocket transport and the runtime-safe `IDEHandler` forward annotation.
- `tests/test_ide_transport_import.py`: isolated subprocess regression test proving the transport module imports without runtime access to the type-check-only handler symbol.
- `docs/冗余设计.md`: original 24 findings plus evidence-backed status for the audited compatibility and duplication claims.

### Task 1: Restore IDE Transport Import Safety

**Files:**
- Create: `tests/test_ide_transport_import.py`
- Modify: `src/athena/ide/transport.py:24`

**Interfaces:**
- Consumes: `athena.ide.transport.WebSocketTransport` and the existing `TYPE_CHECKING` import of `athena.ide.handler.IDEHandler`.
- Produces: an importable `athena.ide.transport` module on Python 3.11 while retaining `WebSocketTransport.__init__(handler)` and avoiding a runtime circular import.

- [ ] **Step 1: Write the isolated failing import test**

```python
import subprocess
import sys


def test_transport_import_does_not_resolve_type_checking_names() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import athena.ide.transport"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
```

- [ ] **Step 2: Run the test and verify the current regression**

Run: `uv run pytest tests/test_ide_transport_import.py -q`

Expected: FAIL at the assertion; stderr contains `NameError: name 'IDEHandler' is not defined`.

- [ ] **Step 3: Make the forward annotation runtime-safe**

Change only the annotation in `src/athena/ide/transport.py`:

```python
def __init__(self, handler: "IDEHandler") -> None:
    self._handler = handler
```

Keep the `TYPE_CHECKING` import. Do not add a runtime import or restore `from __future__ import annotations`.

- [ ] **Step 4: Run focused IDE transport tests**

Run: `uv run pytest tests/test_ide_transport_import.py tests/test_ide_transport.py tests/test_ide_e2e.py -q`

Expected: all selected tests PASS with no collection errors.

- [ ] **Step 5: Scan other type-check-only imports for the same failure mode**

Run: `rg -n -U "if TYPE_CHECKING:[\\s\\S]{0,200}import .*" src/athena`

For each imported symbol used in a runtime-evaluated annotation, verify the annotation is quoted. Do not edit unrelated annotations unless importing that module reproduces the same `NameError`.

- [ ] **Step 6: Commit the isolated regression fix**

```powershell
git add -- tests/test_ide_transport_import.py src/athena/ide/transport.py
git diff --cached --check
git commit -m "fix(ide): keep transport annotations runtime-safe"
```

### Task 2: Record Evidence-Backed Redundancy Dispositions

**Files:**
- Modify: `docs/冗余设计.md`

**Interfaces:**
- Consumes: current public exports, tests, examples, protocol fields, and the canonical type-identity tests in `tests/test_evaluation.py` and `tests/test_experiment_types.py`.
- Produces: a status index that distinguishes `已治理`, `兼容保留`, `暂缓`, and `误报` without deleting public contracts.

- [ ] **Step 1: Verify canonical type identity already implemented**

Run: `uv run pytest tests/test_evaluation.py::test_evaluation_types_reexport_existing_core_contract tests/test_experiment_types.py::test_experiment_outcome_reexports_existing_core_type -q`

Expected: both tests PASS, proving `evaluation.types` and `experiment.types.ExperimentOutcome` re-export the canonical core objects.

- [ ] **Step 2: Capture public-API evidence for the retained Agent and evaluation entries**

Run:

```powershell
rg -n "BaseAgent|create_code_agent|Comparator|athena\.core\.evaluation" src test tests examples agent_tool_example.py
```

Expected: direct production, test, example, or package-export references exist. Classify entries 1, 2, 4, and 7 as `兼容保留`; do not delete or rename them.

- [ ] **Step 3: Capture contract differences that block unsafe consolidation**

Run:

```powershell
rg -n "class EvaluatorFactory|def build|def freeze" src/athena/evaluation/factory.py src/athena/workflows/prepare/evaluator_factory.py
rg -n "class ServerRequestReply|error_code|error_message|RpcError" src/athena/app_server test/unit/app_server
rg -n "class AgentMonitor|WatchResult|dict\[str, Any\]" src/athena/code src/athena/execution tests
```

Expected evidence:

- The Evaluator factories expose incompatible stateful and static APIs and produce different secondary metrics.
- The protocol reply uses `error: RpcError | None`; the transport reply exposes `error_code` and `error_message` used by client/server tests.
- The code monitor returns `WatchResult`; the execution monitor returns a dictionary.

Classify entries 5, 6, 9, and 12 as `暂缓`, noting that consolidation requires consumer migration and contract tests.

- [ ] **Step 4: Prove the reported dead code and CSS duplication claims are inaccurate**

Run:

```powershell
rg -n "StreamEmitter" src tests
rg -n "TransportControl|TransportEOF|send_transport_control" src test tests
rg -n "EmitEvent" src/athena
rg -n "import .*styles\.css|import .*App\.css" athena-ide/src
```

Expected evidence:

- `StreamEmitter` has direct behavioral tests, so entry 15 is `误报`.
- Transport control types are part of transport annotations and a send method, so entry 14 is `暂缓`, not safe dead-code deletion.
- `EmitEvent` aliases describe different callback contracts, so entry 22 is `误报`; a future rename may improve clarity but a shared alias would be incorrect.
- `styles.css` owns global tokens/reset and `App.css` owns component layout; both are imported, so entry 24 is `误报`.

- [ ] **Step 5: Add the audit status index to the redundancy document**

Insert immediately after the document introduction:

```markdown
## 工程化审计状态（2026-07-29）

| 条目 | 状态 | 依据 |
|---|---|---|
| 1 BaseAgent | 兼容保留 | 测试通过子类扩展，且由 core 公共入口导出。 |
| 2 create_code_agent | 兼容保留 | 已确认 Agent 接口设计要求保留该公共构造入口。 |
| 3 evaluation/types.py | 已治理 | 兼容路径重导出 core 中的同一类型对象，类型身份测试通过。 |
| 4 core/evaluation.py | 兼容保留 | 测试、示例和搜索流程仍从该路径导入。 |
| 5 双份 EvaluatorFactory | 暂缓 | 两者 API、状态和次要指标行为不同，不能直接合并。 |
| 6 freeze() | 暂缓 | 方法属于两套现有公共调用方式，需随工厂迁移处理。 |
| 7 Comparator | 兼容保留 | 搜索流程、示例和测试依赖该公开类。 |
| 9 ServerRequestReply | 暂缓 | 协议模型与内部传输回复具有不同错误合同。 |
| 11 ExperimentOutcome | 已治理 | experiment 路径重导出 core 中的同一类型对象。 |
| 12 AgentMonitor | 暂缓 | 两个实现返回 `WatchResult` 和 `dict`，职责合同不等价。 |
| 14 TransportControl / TransportEOF | 暂缓 | 仍属于 Transport 的注解和发送接口，删除需要接口迁移。 |
| 15 StreamEmitter | 误报 | 存在直接单元测试并定义 IDE 事件适配行为。 |
| 22 EmitEvent | 误报 | 同名别名对应不同参数和载荷合同，强行统一会掩盖差异。 |
| 24 App.css / styles.css | 误报 | 两文件分别负责布局与全局 token/reset，且均由入口导入。 |
```

Do not rewrite the original 24 findings. The index records the current audit result while preserving their historical context.

- [ ] **Step 6: Validate and commit the documentation audit**

Run: `git diff --check -- docs/冗余设计.md`

Expected: no whitespace errors.

```powershell
git add -- docs/冗余设计.md
git diff --cached --check
git commit -m "docs: classify redundancy findings by compatibility risk"
```

### Task 3: Run the Complete Python Gate

**Files:**
- No file changes expected.

**Interfaces:**
- Consumes: the complete Python package and all tests under `test/` and `tests/`.
- Produces: evidence that the annotation fix and existing normalization changes collect and execute successfully together.

- [ ] **Step 1: Run the complete Python suite**

Run: `uv run pytest -q`

Expected: pytest exits 0 with no failures or collection errors.

- [ ] **Step 2: Handle any newly exposed failure as a separate defect**

If Step 1 fails, stop this plan at the first failure. Invoke `systematic-debugging`, identify the root cause, and add a new TDD task with exact files and expected behavior before changing more production code. Do not bundle an unknown failure into the annotation fix or redundancy documentation commit.

### Task 4: Run Frontend and Repository Gates

**Files:**
- No file changes expected.

**Interfaces:**
- Consumes: the current `athena-ide` application and the complete working-tree diff.
- Produces: frontend test/build evidence and a whitespace-clean repository diff.

- [ ] **Step 1: Run the frontend unit suite**

Run: `npm test -- --run` in `athena-ide`.

Expected: 9 test files and 20 tests PASS.

- [ ] **Step 2: Build the frontend production bundle**

Run: `npm run build` in `athena-ide`.

Expected: TypeScript and Vite complete with exit code 0 and produce the normal `dist/` build output.

- [ ] **Step 3: Check all staged and unstaged diffs**

Run:

```powershell
git diff --check
git diff --cached --check
git status --short
```

Expected: both diff checks exit 0. `git status` may still show pre-existing user changes, but must not show an uncommitted implementation-plan task file or an unexplained change created by this plan.

- [ ] **Step 4: Report the evidence**

Report the exact pytest summary, the frontend `9 passed / 20 passed` summary, build status, commits created, audited redundancy statuses, and any remaining pre-existing dirty-worktree risk. Do not claim unrun Tauri or Rust gates; this plan does not modify either subsystem.
