# Core and app-server review

Scope: `src/athena/core/` and `src/athena/app_server/`, excluding the files
owned by the parallel human-request/project-lock/user-input work. The review
read the active engineering plan, the linked module-simplification inventory,
and the code standard; it traced the core agent facade through
`RuntimeThreadManager`/`ThreadRuntime`, and the app-server client, protocol,
dispatch, subscription, lifecycle, and transport paths against their tests.

## File decisions

| File | Decision and evidence |
| --- | --- |
| `src/athena/core/__init__.py` | Retain the existing public facade; covered by `tests/test_core_public_api.py`. |
| `src/athena/core/artifact_store.py` | Retain the content-addressed storage boundary and explicit integrity errors; covered by core/agent artifact tests. |
| `src/athena/core/contracts.py` | Retain the shared artifact/event/error contracts used by both core and app-server. |
| `src/athena/core/git_workspace.py` | Retain the workspace and Git ownership boundary; exercised by relevant research and workspace suites. |
| `src/athena/core/persistence.py` | Retain atomic replacement and platform-specific directory-sync fallback; it represents a real filesystem policy boundary. |
| `src/athena/core/research_models.py` | Retain typed research state records; no duplicate serialization adapter was found in the traced callers. |
| `src/athena/core/research_tree.py` | Retain the tree's state-transition/ownership logic and `KeyError` translation; research tests cover these transitions. |
| `src/athena/core/retry.py` | Retain bounded retry policy as a shared boundary; callers use it for transient provider failures. |
| `src/athena/core/thread_models.py` | Retain the compact thread/turn data contracts consumed by core and app-server. |
| `src/athena/core/tool.py` | Retain the canonical tool registry/dispatch mapping; this file was already Reviewed in the inventory and its callers rely on one mapping. |
| `src/athena/core/tool_types.py` | Retain shared tool result/context contracts; previously Reviewed, with unused `max_result_chars` already removed. |
| `src/athena/core/workspace.py` | Retain the workspace protocol and local implementation; callers depend on this seam. |
| `src/athena/core/agent/__init__.py` | Retain canonical exports; previously Reviewed and validated by public API tests. |
| `src/athena/core/agent/agent_runtime.py` | Change: add informative docstrings to public facade methods and runner entry points (25 previously missing); preserve command/status and cancellation behavior. Covered by the agent/core suites. |
| `src/athena/core/agent/chat.py` | Retain the merged text/structured call context; previously Reviewed, with behavior covered by combined agent tests. |
| `src/athena/core/agent/models.py` | Retain the compact typed agent inputs/results/context contracts; no duplicated adapter was found. |
| `src/athena/core/agent/provider.py` | Change: document the intentionally unimplemented Anthropic surface (3 API members); Black also normalized one existing wrapped call. Provider behavior remains unchanged. |
| `src/athena/core/agent/registry.py` | Retain the one-argument factory registration boundary; previously Reviewed and used by runtime tests. |
| `src/athena/core/agent/runtime.py` | Retain the event/tool execution flow; previously Reviewed for redundant task plumbing and covered by runtime tests. |
| `src/athena/core/agent/session.py` | Retain the run-scoped session/checkpoint owner; previously Reviewed and needed by runner contracts. |
| `src/athena/core/agent/settings.py` | Retain explicit configuration coercion and defaults; no safe duplicate policy was found. |
| `src/athena/core/agent/tools/__init__.py` | Retain the package marker; no implementation layer to simplify. |
| `src/athena/core/agent/types.py` | Change: document the public runner/codec protocol methods and control-message property (6 previously missing); type contracts remain unchanged. |
| `src/athena/app_server/__init__.py` | Retain the public server/client exports; covered by `test_public_api.py`. |
| `src/athena/app_server/__main__.py` | Change: document the public `main` smoke-test entry point; Black normalized existing module-level spacing. Self-test cases remain unchanged. |
| `src/athena/app_server/client.py` | Retain request sequencing, dual-lane event/control buffering, and cancellation-safe getter cleanup; covered by client-worker, race, subscription, and e2e tests. |
| `src/athena/app_server/events.py` | Retain journal replay and fair multiplexing; the journal and slow-subscriber behavior have explicit tests. |
| `src/athena/app_server/exceptions.py` | Retain small typed transport/runtime exceptions; no extra wrapper layer exists. |
| `src/athena/app_server/execution.py` | Retain protocol-method dispatch and observer wiring; covered by execution and message-processing tests. |
| `src/athena/app_server/lifecycle.py` | Retain coordinated server/client/manager shutdown ownership; cancellation and cleanup paths are directly tested. |
| `src/athena/app_server/observability.py` | Retain event projection and monitor integration; covered by observability/execution tests. |
| `src/athena/app_server/protocol.py` | Retain the canonical protocol models, method constants, and error mapping; public protocol tests exercise serialization. |
| `src/athena/app_server/server.py` | Retain dispatch, inflight-request accounting, and pending server-call shutdown; request races and shutdown behavior have focused tests. |
| `src/athena/app_server/submissions.py` | Retain discriminated submission/runtime-signal records and terminal state; these are the shared state-machine contract. |
| `src/athena/app_server/thread_manager.py` | Retain manager ownership of runtime construction, fork snapshots, and shutdown; rollout/resume tests cover the boundary. |
| `src/athena/app_server/thread_runtime.py` | Retain serialized submission transitions and turn/fork snapshots; tests cover completion, failure, interruption, close, and history boundaries. |
| `src/athena/app_server/transport.py` | Retain bounded in-memory control/event channels and backpressure behavior; transport tests cover queue and close semantics. |

No additional behavior change was justified: the apparent dynamic runner/result
checks are compatibility boundaries used by the legacy runner and custom
manager fixtures, while the state and cancellation branches have dedicated
regressions. The historical `module-simplification-plan.md` marks many of these
files Pending; this review records the current retain/change decision without
editing that historical inventory.

## Verification

- Initial core/agent/app-server baseline and post-change suite: `339 passed,
  24 subtests passed`.
- Expanded direct core-boundary regression suite (including artifact store, Git
  workspace, retry, tool, and research-tree tests): `431 passed, 27 subtests
  passed`.
- `python scripts/check_code_style.py --enforce-docstrings src/athena/core src/athena/app_server`: passed; the initial scan had 35 R3 missing-docstring findings in owned files, now zero.
- `python -m black --check` on the four changed files: passed after formatting.
- `git diff --check` on changed files: passed.

Tests were run with `PYTHONPATH=src`, required for this checkout's `gui_gateway`
package import during pytest collection. This review does not claim the entire
repository inventory is complete, nor does it alter runtime behavior.

## Manual norm cleanup (2026-10-15)

Applied the scoped findings from `manual-norm-inventory.json` without changing
public contracts or resource/cancellation ownership:

- Added trigger-specific comments directly inside the inventory-listed catches
  in `core/git_workspace.py`, `core/persistence.py`, `core/research_tree.py`,
  `core/agent/agent_runtime.py`, `core/agent/provider.py`,
  `core/agent/runtime.py`, `core/agent/settings.py`,
  `app_server/thread_runtime.py`, and `app_server/__main__.py`. The comments
  identify concrete fallback, translation, retry, cleanup, or cancellation
  behavior rather than repeating the exception type.
- Simplified `LocalGitWorkspace._find_worktree` with an early skip for unrelated
  worktree records. The registered-state consistency check and checkpoint
  recovery path remain unchanged.
- Simplified `Agent.run` by handling error and nonterminal outcomes before the
  successful structured-output path. Invalid structured output still receives
  the same bounded feedback and artifact write behavior.
- Flattened provider stream text emission and accumulated tool-call field
  updates. Empty filtered text and absent fragments retain their previous
  behavior; provider/DSML/truncated-call tests cover serialization boundaries.
- Retained `RuntimeThreadManager._make_runtime`'s `rollout_dir`/history branch:
  its deepest path is three levels (directory mode, history presence, record
  loop); reducing it would obscure the separate append-versus-resume cases.
- Retained the nested request-part mapping in `provider._to_api`: it preserves
  the original interleaving order of text and tool-return parts. Splitting it
  into separate comprehensions or a one-use adapter would either reorder the
  serialized conversation or add indirection without improving the contract.

Verification for this pass:

- Focused core/app-server run: 425 passed and 27 subtests passed. One unrelated
  test failed while importing the concurrently edited
  `src/athena/research/evaluation/evaluator.py`, which currently raises a UTF-8
  decode `SyntaxError` at line 5; the failure is in
  `tests/unit/app_server/test_thread_manager.py::test_research_runtime_writes_agent_recovery_log`.
- Provider stream follow-up: 65 passed (`test_agent.py`, DeepSeek DSML, and
  truncated-tool-call suites).
- Black check for all touched Python files: passed (9 files unchanged).
- `python scripts/check_code_style.py --enforce-docstrings src/athena/core src/athena/app_server`: passed.
- `python -m compileall -q src/athena/core src/athena/app_server`: passed.
