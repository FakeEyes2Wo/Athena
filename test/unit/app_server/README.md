# `athena.app_server` implementation tests

This directory tests the API that currently exists in `src/athena/app_server`.
It does not import `app_server_codex` or require interfaces that are absent
from the target package.

Run the suite with:

```bash
.venv/Scripts/python.exe -B -m pytest -p no:cacheprovider -q test/unit/app_server
```

The suite follows the current constructors, DTO shapes, return values, and
module docstrings. Tests with deadlines cover current asynchronous ownership
and shutdown paths without modifying production code.

Known contradictions between current docstrings and current behavior are
marked as strict `xfail` tests with an explicit reason. If such a behavior is
fixed, the unexpected pass makes the suite fail until the marker is removed.
