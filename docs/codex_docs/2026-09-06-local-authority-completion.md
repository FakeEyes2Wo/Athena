# Local baseline authority completion

## Delivered

- GUI now creates `LocalBaselineAuthorityStore` by default when no host
  `ATHENA_CONTROLLER_FACTORY` is configured.
- The store is controller-private, keyed by hashed project/session identity,
  and persists exact baseline bytes, verification and PREPARE attestation with
  an OS lock, atomic replace and generation compare-and-exchange.
- `ATHENA_AUTHORITY_MODE=ssh` fails explicitly because SSH support is not yet
  implemented; it never falls back silently. `ATHENA_AUTHORITY_LOCAL_ROOT`
  is an optional controller-side local-root override.
- GUI settings expose only `authority_mode` and a plain-language security
  boundary. Local mode is reported as `same-user local filesystem`; no path,
  credential, callback or SSH setting is projected.
- Host factories still take precedence and retain their authority plus bounded
  repair callback behavior. Local authority has no repair callbacks, so local
  record errors fail closed without dispatching an Agent.

## Verification

2026-09-06:

```text
.venv\\Scripts\\python.exe -m pytest \
  test/unit/research/prepare/test_authority_local.py \
  tests/test_gui_gateway_main.py \
  test/unit/research/test_runtime_settings.py \
  test/unit/research/test_environment_repair.py \
  test/unit/research/test_controller_startup.py -q
42 passed, 1 warning in 2.77s

.venv\\Scripts\\python.exe -m ruff check [7 owned source/test files]
All checks passed!

.venv\\Scripts\\python.exe -m black --check [7 owned source/test files]
7 files would be left unchanged.

.venv\\Scripts\\pre-commit.exe run athena-code-style --files [7 owned source/test files]
Passed

git diff --check
no diff errors
```

The single pytest warning is the pre-existing workspace permission denial for
`.pytest_cache`; it does not affect test execution.
