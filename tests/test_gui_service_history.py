"""GUI parse_intent persistence (session management moved to the gateway handler)."""

from pathlib import Path

import pytest

from athena.gui.service import GuiService


class HistoryRuntime:
    """Minimal runtime for GuiService construction; only transcript persistence."""

    def __init__(self) -> None:
        self.tree_path = Path("/tmp/athena-runtime")
        self.persisted: list[str] = []

    def persist_user_message(self, text: str) -> None:
        self.persisted.append(text)


@pytest.mark.asyncio
async def test_parse_intent_persists_user_message(monkeypatch) -> None:
    runtime = HistoryRuntime()
    service = GuiService(runtime)

    import athena.gui.service as svc

    def no_client():
        raise RuntimeError("no client")

    monkeypatch.setattr(svc.settings, "get_client", no_client)
    monkeypatch.setattr(svc.settings, "model_name", lambda: "deepseek-v4-flash")

    result = await service.parse_intent("用图像分类数据集优化准确率")

    # 客户端不可用 → 启发式回退，但用户消息仍被写入会话日志。
    assert "task_type" in result
    assert runtime.persisted == ["用图像分类数据集优化准确率"]
