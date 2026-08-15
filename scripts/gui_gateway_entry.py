"""PyInstaller 入口：等价于 ``python -m gui_gateway``。

冻结环境里 logfire 用 ``inspect.getsource`` 读取源码打补丁，源文件不可用时抛
OSError 导致启动失败；这里让 getsource 失败时返回空串，跳过该补丁（补丁是
pydantic 的 bug 规避，非必需）。
"""

import inspect

_orig_getsource = inspect.getsource


def _safe_getsource(obj):
    try:
        return _orig_getsource(obj)
    except (OSError, TypeError):
        return ""


inspect.getsource = _safe_getsource

import asyncio

from gui_gateway.__main__ import main

asyncio.run(main())
