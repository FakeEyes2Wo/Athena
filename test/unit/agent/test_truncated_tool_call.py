"""工具实参被 ``max_tokens`` 截断时，必须报成截断，而不是"缺必填参数"。

回归自 2026-08-30 的 TESS 轮：``_assemble_function_calls`` 在 ``json.loads``
抛错时把实参吞成 ``{}``，于是写 ``EDA_REPORT_04_RELATIONSHIPS.md`` 的
``write_file`` 调用被输出上限切断后，模型收到的是"缺 path/content"。模型据此
判断自己参数写错了，原样重发同一个超长调用，在同一处再次被切断——三次重试全废，
连带把整个 PREPARE 打成降级模式。

关键不是"别截断"（那是 max_tokens 配置问题），而是**截断必须可归因**：模型只有
知道自己是被长度切的，才会改成分块写；只报 schema 错，它永远只会原样重试。
"""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelRequest

from athena.core.agent.models import AgentConfig, AgentContext
from athena.core.agent.provider import ResponsesProvider, StreamEvent
from athena.core.agent.runtime import Agent, _truncated_tool_result
from athena.core.thread_models import AthenaThread, AthenaTurn
from athena.core.tool import ToolRegistry, tool
from athena.memory.context_manager import ContextManager

# 一个被从中间切断的 write_file 实参：JSON 没有收尾，content 字段停在半截。
TRUNCATED_ARGS = '{"path": "EDA_REPORT_04_RELATIONSHIPS.md", "content": "# Relat'
COMPLETE_ARGS = '{"path": "a.md", "content": "hello"}'


def _tool_delta(arguments, *, name="write_file", index=0, call_id="call_1"):
    return [
        SimpleNamespace(
            index=index,
            id=call_id,
            function=SimpleNamespace(name=name, arguments=arguments),
        )
    ]


def _chunk(content=None, tool_calls=None, finish_reason=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice])


class _StreamClient:
    def __init__(self, chunks):
        self._chunks = chunks
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        async def gen():
            for chunk in self._chunks:
                yield chunk

        return gen()


async def _collect(chunks):
    provider = ResponsesProvider("test", client=_StreamClient(chunks), provider_kind="openai")
    events = []
    async for event in provider.stream(
        SimpleNamespace(max_tokens=512, temperature=0.0, tool_choice="auto", seed=None),
        SimpleNamespace(specs=[]),
        [],
        asyncio.Event(),
        output_type=None,
    ):
        events.append(event)
    return events


@pytest.mark.asyncio
async def test_length_truncated_tool_call_is_flagged_not_silently_emptied():
    """撞上 max_tokens：实参解析不出来，但事件必须带上截断事实。"""
    events = await _collect(
        [
            _chunk(tool_calls=_tool_delta(TRUNCATED_ARGS)),
            _chunk(finish_reason="length"),
        ]
    )
    calls = [e for e in events if e.kind == "function_call"]
    assert len(calls) == 1
    call = calls[0]
    assert call.data["name"] == "write_file"
    assert call.data["arguments"] == {}
    # 这三个字段是修复的全部意义：没有它们，下游只能报 schema 错。
    assert call.data["truncated"] is True
    assert call.data["raw_argument_chars"] == len(TRUNCATED_ARGS)
    assert call.data["finish_reason"] == "length"


@pytest.mark.asyncio
async def test_complete_tool_call_is_not_flagged():
    """正常收尾的调用不该被误判成截断。"""
    events = await _collect(
        [
            _chunk(tool_calls=_tool_delta(COMPLETE_ARGS)),
            _chunk(finish_reason="tool_calls"),
        ]
    )
    calls = [e for e in events if e.kind == "function_call"]
    assert len(calls) == 1
    assert calls[0].data["truncated"] is False
    assert calls[0].data["arguments"] == {"path": "a.md", "content": "hello"}


@pytest.mark.asyncio
async def test_truncation_arrives_in_chunks_and_still_flags():
    """实参跨多个 chunk 累积后才被切断，同样要标记。"""
    events = await _collect(
        [
            _chunk(tool_calls=_tool_delta('{"path": "x.md", ')),
            _chunk(tool_calls=_tool_delta('"content": "aaa')),
            _chunk(finish_reason="length"),
        ]
    )
    calls = [e for e in events if e.kind == "function_call"]
    assert len(calls) == 1
    assert calls[0].data["truncated"] is True
    assert calls[0].data["raw_argument_chars"] == len('{"path": "x.md", "content": "aaa')


@pytest.mark.asyncio
async def test_truncated_tool_result_tells_the_model_to_split():
    """错误文案必须把模型推向分块写，而不是原样重试。"""
    result = await _truncated_tool_result("write_file", 4096, "length")
    assert result.success is False
    assert result.data["truncated"] is True
    assert result.data["raw_argument_chars"] == 4096
    text = result.error.lower()
    # 说清是长度问题，不是参数写错了
    assert "output-token limit" in text
    assert "not a schema mistake" in text
    # 给出可执行的出路，并点名真实存在的工具
    assert "split the work" in text
    assert "write_file" in text
    assert "append_file" in text


class _TruncationRecoveryProvider:
    """先发一个被截断的 write_file，看到截断说明后改用分段写。

    这是真正要锁住的行为：provider 打标记、runtime 拦截、错误文案回到模型手里，
    三段必须接得上。单独测任何一段都盖不住这条链路——2026-08-30 那次死循环，
    provider 和工具各自都"正常工作"，坏的恰恰是它们之间的交接。
    """

    def __init__(self) -> None:
        self.model_name = "fake"
        self.samples = 0
        self.saw_truncation_notice = False

    async def stream(self, config, tools, messages, cancel, *, output_type=None):
        self.samples += 1
        if self.samples == 1:
            # 撞上 max_tokens：实参没解析出来，但带着截断标记。
            yield StreamEvent(
                kind="function_call",
                data={
                    "call_id": "c1",
                    "name": "write_file",
                    "arguments": {},
                    "truncated": True,
                    "raw_argument_chars": 4096,
                    "finish_reason": "length",
                },
            )
        elif self.samples == 2:
            returned = "".join(
                str(getattr(part, "content", ""))
                for message in messages
                if isinstance(message, ModelRequest)
                for part in message.parts
                if getattr(part, "part_kind", None) == "tool-return"
            )
            # 模型必须被告知是长度问题，而不是"缺必填参数"。
            assert "output-token limit" in returned
            assert "NOT a schema mistake" in returned
            assert "append_file" in returned
            assert "missing" not in returned.lower()
            self.saw_truncation_notice = True
            yield StreamEvent(
                kind="function_call",
                data={
                    "call_id": "c2",
                    "name": "write_file",
                    "arguments": {"path": "r.md", "content": "# Title\n"},
                },
            )
        else:
            yield StreamEvent(
                kind="text_delta", data={"delta": "done", "accumulated": "done"}
            )
        yield StreamEvent(kind="response_completed", data={"finish_reason": "stop"})


async def _noop_emit(*_a: object) -> None:
    return None


@pytest.mark.asyncio
async def test_truncated_call_recovers_in_the_same_turn() -> None:
    """端到端：截断 → 模型收到可归因的说明 → 同 turn 改成分段写。"""
    provider = _TruncationRecoveryProvider()
    written: dict[str, str] = {}

    @tool
    async def write_file(path: str, content: str) -> dict:
        written[path] = content
        return {"path": path}

    @tool
    async def append_file(path: str, content: str) -> dict:
        written[path] = written.get(path, "") + content
        return {"path": path}

    registry = ToolRegistry()
    registry.register(write_file)
    registry.register(append_file)
    agent = Agent(provider, registry, "system", AgentConfig(max_turns=5))
    ctx = AgentContext(
        thread=AthenaThread(
            thread_id="t1", session_id="s1", status="running", context_ref="c1"
        ),
        turn=AthenaTurn(
            turn_id="t1-turn", thread_id="t1", request_ref="c1", status="running"
        ),
        emit=_noop_emit,
        cancel=asyncio.Event(),
        tools=registry,
        memory=ContextManager(),
        input_text="write the report",
    )
    outcome = await agent.run(ctx)

    assert outcome.result_ref is not None
    assert provider.saw_truncation_notice, "截断说明没有回到模型手里"
    # 截断 → 恢复后写成 → 完成，同一 turn 内采样 3 次（与未知工具名的恢复路径一致）
    assert provider.samples == 3
    assert written == {"r.md": "# Title\n"}
