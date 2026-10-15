# Root module review (2026-10-15)

Read the active plan and code standard, then trace each changed boundary through
its callers and tests. The pre-optimization copies under `baseline/` include the
previous hypothesis/GUI repairs; they are the comparison point for this task.
No real provider request, Kaggle submission, or new SEARCH session was used.

## File decisions

| File | Decision and verification |
| --- | --- |
| `src/athena/__init__.py` | Retain the one-line package description; no runtime layer to simplify. |
| `src/athena/cli.py` | Retain composition of runtime, compute, data, and command options. Reject NaN/Inf as well as negative timeout values, document public entrypoints and exception scenarios. CLI tests cover argument validation and runtime dispatch. |
| `src/athena/core/agent/tools/user_input.py` | Replace an invalid isinstance check against an Annotated union with concrete reply classes. Preserve typed HumanOutcome and legacy callback behavior. Dedicated contract tests cover choice/text/skip and fallback paths. |
| `src/athena/core/human_request.py` | Validate malformed kind values before membership checks so list/dict kinds produce HumanContractError, not an unhashable TypeError. Keep Pydantic errors inside the canonical contract boundary and document public reply records. |
| `src/athena/core/project_lock.py` | Preserve OS single-writer ownership. A rejected contender must never delete the live holder's PID file; the owner removes its PID before unlocking. Cross-process tests reproduce and verify this sequence. Move platform imports to module scope and type binary lock handles. |
| `src/athena/memory/__init__.py` | Retain public memory exports; verified cold import of memory before core. |
| `src/athena/memory/context_manager.py` | Own nested messages on writes and reads using deepcopy; caller mutation can no longer change history without updating tokens/version. Retain compaction/snapshot/rollback behavior. A documented local import avoids the demonstrated memory -> core facade -> agent.models -> memory cycle. |
| `src/athena/memory/compaction.py` | Retain shared summary/recent-message and snapshot/version boundaries; memory callers and compaction tests do not depend on message identity. |
| `src/athena/memory/rollout.py` | Retain append-log parsing, terminal replay and history reconstruction; fork/resume/rollout tests cover its durable contract. |
| `src/athena/retrieval/__init__.py` | Retain minimal package boundary. |
| `src/athena/retrieval/web_search.py` | Decode DuckDuckGo redirect destinations once. Enforce each fetch caller's character limit on cached pages without mutating the cached larger view, and refetch when an earlier cached truncation cannot satisfy a larger request. Retain shared session/cache/rate-limit ownership and document public APIs. |
| `src/athena/kaggle/__init__.py` | Retain canonical exports used by CLI and Agent tools. |
| `src/athena/kaggle/auth.py` | Retain credential discovery/encoding boundary; document public responsibilities. Credential tests use local temporary input. |
| `src/athena/kaggle/client.py` | Retain HTTP/download/submission and API-shape normalization boundaries. Flatten discussion reply extraction and ordered archive source lookup; preserve ipynb-before-py selection. Add scenario-specific exception comments. No live Kaggle request was issued. |
| `src/athena/kaggle/schemas.py` | Retain request and evidence records shared by CLI/pipeline/tools; document public schemas. |
| `src/athena/kaggle/pipeline.py` | Retain Kaggle workflow composition rather than duplicating provider/Agent ownership; existing pipeline tests cover evidence and artifacts. |
| `src/athena/kaggle/tool.py` | Retain tool-level workspace paths, typed API inputs and evidence contract; wiring/gate/tool tests cover this seam. |
| `src/athena/serving/__init__.py` | Retain package boundary after prior removal of duplicate serving entrypoint. |
| `src/athena/serving/model.py` | Keep optional LightGBM availability at module scope and fail clearly only when its backend is selected without the dependency. Preserve other model adapters and document public methods. Stub/missing-module tests verify both branches. |
| `src/athena/serving/predictions_api.py` | Reject malformed Content-Length with HTTP 400 rather than closing a request through an uncaught ValueError. A real local HTTP test verifies the response; retain existing bounded input/model dispatch. |
| `scripts/check_code_style.py` | Make CLI parsing, strict docstrings, target validation, repeated calls, UTF-8/BOM and AST/token checks deterministic. Add R6 for a comment on each specific exception handler; comment meaning remains a review responsibility. 29 checker tests pass, including observed red cases for the new handler rule. |
| `scripts/build.cjs` | Pass executable/argv separately, preserving checkout paths with spaces. Restrict cleanup to the PyInstaller work directory; retain failed-build evidence. Five stubbed command-composition tests pass; no installer is built. |
| `scripts/run_headless.py` | Acquire the same project lock as other writers, explicitly enable noninteractive confirmation, report failed/non-completed phases as failure, validate budgets, and close runtime in finally. Six focused tests cover failures and cancellation cleanup. |
| `scripts/gui_gateway_entry.py` | Retain the frozen bootstrap: inspect fallback must be installed before importing dependencies that inspect source during model setup. Move stdlib import to the header and protect executable startup with __main__. This intentional module-level import order is documented, not silently moved across initialization. |
| `scripts/tui_entry.py` | Retain the same frozen-source startup requirement, with explicit failure condition comments; no application imports inside functions. |
| `scripts/prepare_examples_article.py` | Close PDFs on extraction failure, remove repeated whitespace normalization and document dependency failure. The standalone script remains an explicit CLI. |
| `athena-gui/src/components/conversation/HumanRequestPanel.tsx` | Fence completion by request epoch so an old promise cannot change the new question's local error/submission state. Two new tests first failed on late success and failure, then passed; the full component selection has 12 passing tests. Hook-level fencing is separately reviewed in gui-tui-review.md. |
| `athena-gui/src/components/conversation/__tests__/HumanRequestPanel.test.tsx` | Await asynchronous replies inside act, keeping the assertions and double-click contract while removing late test updates. Add request-switch race cases. |
| `athena-gui/src-tauri/build.rs` | Retain the three-line canonical Tauri build-script entry; the native bridge check/test pass exercises its build integration. |
| `athena-gui/vite.config.ts` | Retain React plugin, explicit dev port override and strict binding, and ignored native build watcher path; GUI build and launcher tests cover this composition. |
| `athena-gui/vitest.config.ts` | Retain jsdom/setup and source-test glob, keeping node:test launcher/build scripts in their separate runner. Root npm test runs both kinds. |

## Verification to date

- Core/human-input/lock/serving selections: 97 passed before the final small
  manual-comment pass; core/app-server owner supplied the wider follow-up.
- Retrieval/CLI/Kaggle selection: 78 passed. Serving/Kaggle: 60 passed.
- Memory/compaction/rollout selection: 47 passed; independent caller review and
  tests: 109 passed. Final checker/memory/Kaggle selection: 92 passed, including
  nested ToolReturn input ownership for append and replace_range.
- Root npm test: five build tests, 252 GUI tests, one launcher test passed before
  adding two HumanRequestPanel races. Component rerun: 12 passed without prior
  act warnings. Final frontend/hook verification is recorded in completion.
- Full `src scripts` strict checker passes; Black checks 245 Python files.
- Final integrated backend/frontend evidence is owned by the root completion
  report; the counts above are overlapping focused selections, not additive.

## Encoding recovery

A manual-comment batch mistakenly round-tripped nine UTF-8 research/Agent files
through Windows PowerShell's default GB2312 encoding. The damaged bytes were
saved before recovery. To preserve all uncommitted work, the root reproduced the
same transform on CRLF copies of the pre-optimization baseline, matched unchanged
lines to the original UTF-8 text, and retained every current ASCII increment.
All non-baseline inserted/replacement lines were verified ASCII. Reapplying the
same transform to each recovered file exactly reproduced its incident bytes
(normalizing only line endings), and full source compilation passed. No HEAD
restore was used. Specific handler comments were then rewritten from generic
templates to actual input/state/operation conditions. Final tests run afterward.

The snapshots and byte-comparison inventory are retained under this ignored
evidence directory; no session artifacts or user changes were discarded.
