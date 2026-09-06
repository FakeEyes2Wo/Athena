# Controller authority wiring and bounded environment repair — 2026-09-06

## Delivered

- `ControllerCapabilities` binds an external `BaselineAuthorityStore` and an
  allowlisted mapping of no-argument environment callbacks per resolved project
  root and GUI session. GUI RPC requests, prompts and Agent tools cannot supply
  those values.
- GUI runtime construction accepts the explicit controller factory. The regular
  gateway can load one only by the host-owned import reference
  `ATHENA_CONTROLLER_FACTORY=package.module:callable`; it never creates a local
  authority fallback when absent.
- Research startup preflights the authority before initializing Git, agents,
  survey or phase work. Missing capability therefore fails before EDA/API work.
- The new `environment_repair` role has exactly one tool: `perform_operation`
  over one pre-registered callback. It contains no workspace file tools, shell,
  arbitrary command, path or data access. It receives only a safe transient
  failure category. Transport timeout/connection failures get at most two agent
  attempts; each is followed by a controller `load()` recheck. Authority,
  integrity and programming failures do not invoke repair and their details are
  not published to the Agent/UI.

## Evidence

- `pytest test/unit/research/test_environment_repair.py
  test/unit/research/test_controller_startup.py tests/test_gui_gateway_main.py`:
  15 passed.
- `ruff check` over the changed controller, runtime and tests: passed.
- `git diff --check`: passed.
- Tests are synthetic: no LLM/API call, real dataset, FINAL read, service restart
  or credentials. They cover project/session binding, absent authority, no local
  fallback, factory import validation, startup order, bounded repairs, rejection
  of nonrepairable errors and redacted callback failure.

## Deployment boundary

This completes Athena's minimal code mechanism, not the external service itself.
A deployment owner must provide the module named by `ATHENA_CONTROLLER_FACTORY`,
with a real independently protected `BaselineAuthorityStore` and any safe
repair callbacks. `docs/controller-environment-repair.md` defines the boundary.
Without it, GUI research safely fails at preflight. The existing live TESS session
was not restarted with a fake authority and no completed SEARCH run is claimed.

Committed and pushed on main as `be9e311fdbbf241f81f074d39e7bd651e5894072`
(`feat(gui): wire authority preflight and bounded repair`). Unrelated worktree
changes, including the active GUI closeout work, remain preserved.
