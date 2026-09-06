"""PyInstaller entry point for the Athena TUI.

Some frozen dependencies inspect their own source during Pydantic model setup.
PyInstaller bundles do not expose source files, so make that optional lookup
return an empty string just as the GUI gateway entry point does.
"""

import inspect

_original_getsource = inspect.getsource


def _safe_getsource(obj):
    try:
        return _original_getsource(obj)
    except (OSError, TypeError):
        return ""


inspect.getsource = _safe_getsource

from athena_tui.entrypoint import main

if __name__ == "__main__":
    raise SystemExit(main())
