"""Suite-wide test isolation."""

import pytest

from gui_gateway import state_store


@pytest.fixture(autouse=True)
def isolated_gui_state(tmp_path, monkeypatch):
    """把 GuiStateStore 的默认路径挪进 tmp_path。

    默认是 ``~/.athena/gui_state.json``：没有这层隔离，任何构造 handler 而不注入
    store 的用例（例如 gateway e2e）都会覆盖开发机上真实的 GUI 配置（活动工作区、
    每个工作区上次会话）。
    """
    monkeypatch.setattr(state_store, "DEFAULT_STATE_PATH", tmp_path / "gui_state.json")
