# Tooling review

Scope: `scripts/` except `check_code_style.py` (owned by root), with an independent review of root's `run_headless.py` and `build.cjs` changes. No packaging operation, model/API request, service, or live session was run.

## Per-file decisions

| File | Decision | Evidence / reason |
| --- | --- | --- |
| `scripts/build.cjs` | Retain root changes | Executables and argv stay separate (including paths with spaces); Windows shell use is limited to fixed `npm.cmd`; failure stops later steps; cleanup targets only `build/pyinstaller` and happens after PyInstaller succeeds. Stub tests cover these contracts. |
| `scripts/check_code_style.py` | Retain; not edited | Root owns the checker. The strict check passed against `scripts/`. |
| `scripts/check_release_secrets.py` | Add API docs and explain decode skip | `main` is documented; Unicode decode failure skips non-text bytes without printing content. Scan output redacts matched values. |
| `scripts/export_rust_contract_fixtures.py` | Add API docs and explain expected invalid fixture | Documented `default_output`/`main`; its `ValueError` catch records an intentionally invalid DTO fixture, not a swallowed runtime failure. |
| `scripts/gui_gateway_entry.py` | Retain initialization order | `inspect.getsource` is patched before importing `gui_gateway.__main__`, which can import source-inspecting dependencies. The tiny `safe_getsource` implementation is intentionally local so no helper import can precede the frozen-runtime patch. |
| `scripts/prepare_examples_article.py` | Fix cleanup and document public helpers | Removed the second whitespace normalization after `strip_tags`; use `with fitz.open(...)` so exceptions during page extraction close the PDF. A failure-path test verifies closure. |
| `scripts/probe_max_tokens.py` | Add `main` API doc; no probe run | The script deliberately makes live model requests in `main`; it was not executed. Its broad catch remains limited to the probe where provider rejection may have varying exception types and only known validation-like messages count as rejection. |
| `scripts/release_dependency_inventory.py` | Add `main` API doc | Retained read-only installed metadata inventory behavior. |
| `scripts/run_headless.py` | Retain root behavior; Black-format | Lifecycle closes the runtime in `finally`, cancellation propagates, project lock prevents concurrent writers, and unsuccessful/nonterminal state returns nonzero. Tests cover these boundaries. |
| `scripts/tui_entry.py` | Retain initialization order | Same freeze constraint as GUI entry: patch source lookup before importing the TUI entrypoint. The two safe wrappers are only a few lines; a helper module would add an import that must itself be available before application imports in the frozen bundle without reducing meaningful complexity. |
| `scripts/__tests__/build.test.cjs` | Retain root tests | Five stubbed Node tests exercise argv boundaries, cleanup ownership, failed-step behavior, backend validation, and skipped build steps. No actual packaging/removal occurred. |
| `tests/unit/test_headless_runner.py` | Retain root tests; Black-format | Covers terminal-success semantics, cleanup under cancellation, lock contention, and invalid negative budget. |
| `tests/unit/test_prepare_examples_article.py` | Add | Verifies PyMuPDF document closure when extraction raises. |

## Verification

- `python scripts/check_code_style.py --enforce-docstrings scripts` — passed.
- `python -m black --check scripts --exclude 'check_code_style\\.py' tests/unit/test_prepare_examples_article.py tests/unit/test_headless_runner.py` — passed (10 files unchanged).
- `PYTHONPATH=src;athena-gui/src python -m pytest -q tests/unit/test_prepare_examples_article.py tests/unit/test_headless_runner.py` — 7 passed.
- `node --test scripts/__tests__/build.test.cjs` — 5 passed.
- An initial pytest invocation without the repository's `src` and `athena-gui/src` paths failed during conftest import (`gui_gateway` not found); rerunning with the project import paths passed.

No remaining scripts-scope correctness blocker was found in the reviewed changes. Frozen entrypoint behavior is source-order reviewed; a real frozen build was intentionally not run because it invokes packaging and filesystem cleanup.
