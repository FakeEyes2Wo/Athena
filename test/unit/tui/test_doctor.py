"""配置自检测试 — 用假 client 覆盖成功与各类失败路径。"""

import pytest

from athena.tui import doctor as doctor_module
from athena.tui.doctor import Doctor, mask
from athena.tui.runner import MODEL_ENV


class FakeChoice:
    def __init__(self, content=None, tool_calls=None) -> None:
        self.message = type("Msg", (), {"content": content})()
        self.delta = type("Delta", (), {"content": content, "tool_calls": tool_calls})()


class FakeStream:
    def __init__(self, chunks) -> None:
        self._chunks = chunks

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for chunk in self._chunks:
            yield chunk


class FakeCompletions:
    def __init__(self, *, reply="收到", tool_calls=True, error=None) -> None:
        self._reply = reply
        self._tool_calls = tool_calls
        self._error = error

    async def create(self, **kwargs):
        if self._error is not None:
            raise self._error
        if not kwargs.get("stream"):
            return type("Resp", (), {"choices": [FakeChoice(content=self._reply)]})()
        calls = [{"index": 0}] if self._tool_calls else None
        chunk = type("Chunk", (), {"choices": [FakeChoice(tool_calls=calls)]})()
        return FakeStream([chunk])


class FakeProvider:
    completions = FakeCompletions()

    def __init__(self, **kwargs) -> None:
        pass

    @property
    def client(self):
        completions = type(self).completions
        chat = type("Chat", (), {"completions": completions})()
        return type("Client", (), {"chat": chat})()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-abcdefghijklmnop")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv(MODEL_ENV, "qwen3.7-plus")


def use_provider(monkeypatch, **kwargs) -> None:
    FakeProvider.completions = FakeCompletions(**kwargs)
    monkeypatch.setattr(doctor_module, "ResponsesProvider", FakeProvider)


def test_mask_keeps_head_and_tail():
    masked = mask("sk-test-abcdefghijklmnop")
    assert masked.startswith("sk-tes") and masked.endswith("（24 字符）")
    assert "abcdefghij" not in masked


def test_mask_handles_missing_and_short():
    assert mask("") == "（未设置）"
    assert mask("short") == "sh…"


def test_env_check_flags_missing_key(monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    doctor = Doctor()
    doctor.check_env()
    assert doctor.failures == 1
    assert "（未设置）" in capsys.readouterr().out


def test_blank_base_url_is_not_a_failure(monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_BASE_URL", "")
    doctor = Doctor()
    doctor.check_env()
    assert doctor.failures == 0
    assert "走 OpenAI 官方地址" in capsys.readouterr().out


def test_runtime_check_reports_resolved_model(capsys):
    assert Doctor().check_runtime() == "qwen3.7-plus"
    assert "qwen3.7-plus" in capsys.readouterr().out


async def test_full_run_passes(monkeypatch, capsys):
    use_provider(monkeypatch)
    assert await Doctor().run() == 0
    assert "全部通过" in capsys.readouterr().out


async def test_chat_failure_is_reported(monkeypatch, capsys):
    use_provider(monkeypatch, error=ConnectionError("dns 解析失败"))
    doctor = Doctor()
    assert await doctor.run() == 1
    out = capsys.readouterr().out
    assert "ConnectionError" in out and "dns 解析失败" in out
    assert doctor.failures == 2


async def test_endpoint_without_tool_calls_fails(monkeypatch, capsys):
    use_provider(monkeypatch, tool_calls=False)
    doctor = Doctor()
    assert await doctor.run() == 1
    assert "没有返回 tool_calls" in capsys.readouterr().out
    assert doctor.failures == 1
