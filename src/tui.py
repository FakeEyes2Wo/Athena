"""Athena 对话式 TUI 兼容 shim（athena-tui-design 验收 #1）。

生产入口统一为 :mod:`athena_tui`（``Athena-tui`` / ``python -m athena_tui``）；
本模块仅作兼容薄包装：``python -m src.tui`` 时委托 ``athena_tui.main``。
"""

from athena_tui import main

if __name__ == "__main__":
    raise SystemExit(main())
