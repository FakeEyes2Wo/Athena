"""src/tui.py shim 测试（athena-tui-design 验收 #1）。

src/tui.py 已收敛为兼容 shim：加载后 ``main`` 即 :mod:`athena_tui` 的 ``main``，
以 ``__main__`` 运行时委托它并以同一退出码退出。真实 TUI 行为由
``test/unit/athena_tui/`` 覆盖（本文件不再测 legacy 线程/命令队列/Rich 渲染）。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_TUI = Path(__file__).resolve().parents[2] / "src" / "tui.py"


def _load_tui():
    """从文件路径加载 src/tui.py（src/ 非包，用 importlib）。"""
    spec = importlib.util.spec_from_file_location("src.tui", _TUI)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["src.tui"] = module
    spec.loader.exec_module(module)
    return module


def test_tui_shim_exposes_athena_tui_main() -> None:
    """shim 的 main 就是 athena_tui.main（同一实现，非复制）。"""
    import athena_tui

    tui = _load_tui()
    assert tui.main is athena_tui.main


def test_tui_shim_exits_with_main_code(monkeypatch) -> None:
    """以 __main__ 运行 shim → 委托 main 并以其退出码退出。"""
    tui = _load_tui()
    monkeypatch.setattr(tui, "main", lambda: 7)
    with pytest.raises(SystemExit) as exc:
        exec("raise SystemExit(main())", {"main": tui.main})
    assert exc.value.code == 7
