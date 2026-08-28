"""DeepSeek DSML tool-call 语法剥离。

DeepSeek 在 ``tool_choice=auto`` + 流式下会间歇性把 ``<｜DSML｜tool_calls>`` 等
标记片段泄漏进 ``content``，污染 json_object 结构化输出。这些测试锁定
``_DeepSeekTextFilter`` 的剥离行为（含跨 chunk 边界），并验证 provider 流式
集成的 ``accumulated`` 文本干净。
"""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelResponse, ThinkingPart, ToolCallPart

from athena.core.agent.provider import ResponsesProvider, _DeepSeekTextFilter, _to_api


def _filtered(chunks):
    f = _DeepSeekTextFilter()
    out = []
    for chunk in chunks:
        out.append(f.push(chunk))
    out.append(f.flush())
    return "".join(out)


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        (
            [
                "before <｜DSML｜tool_use_error><tool_name>write</tool_name></｜DSML｜tool_use_error> after"
            ],
            "before  after",
        ),
        (
            ["before ", "<｜DS", "ML｜tool_calls>body</｜DSML｜tool_calls>", " after"],
            "before  after",
        ),
        (["<|DSML|tool_call>read</|DSML|tool_call> visible"], " visible"),
        (["<|DS", "ML|tool_call>read\n", "</|DSML|tool_calls>"], ""),
        (
            ["before <｜｜DS", "ML｜｜tool_calls>body</｜｜DSML｜｜tool_calls> after"],
            "before  after",
        ),
        (["visible <｜DSML｜tool_calls>partial body, no close"], "visible "),
        (
            [
                "a<｜DSML｜tool_use_error>x</｜DSML｜tool_use_error>b<｜DSML｜function_calls>y</｜DSML｜function_calls>c"
            ],
            "abc",
        ),
    ],
)
def test_dsml_filter_drops_markers(chunks, expected):
    assert _filtered(chunks) == expected
    assert "DSML" not in _filtered(chunks)


def test_dsml_filter_holds_partial_open_token():
    f = _DeepSeekTextFilter()
    mid = f.push("safe text<｜DSM")
    assert "<｜DSM" not in mid
    assert (
        f.push("L｜tool_calls>body</｜DSML｜tool_calls> done") + f.flush()
        == "safe text done"
    )


def test_dsml_filter_preserves_clean_json():
    f = _DeepSeekTextFilter()
    chunks = [
        '{"decision":"submit",',
        '"reason":"ok"}<｜DSML｜tool_calls>',
        "shadow</｜DSML｜tool_calls>",
    ]
    out = []
    for c in chunks:
        out.append(f.push(c))
    out.append(f.flush())
    assert "".join(out) == '{"decision":"submit","reason":"ok"}'


def _chunk(content=None, tool_calls=None, finish_reason=None, reasoning_content=None):
    delta = SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        reasoning_content=reasoning_content,
    )
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice])


class _StreamClient:
    def __init__(self, chunks):
        self._chunks = chunks
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)

        async def gen():
            for chunk in self._chunks:
                yield chunk

        return gen()


@pytest.mark.asyncio
async def test_deepseek_thinking_is_disabled_by_default():
    client = _StreamClient([_chunk(content="done", finish_reason="stop")])
    provider = ResponsesProvider(
        "deepseek-test", client=client, provider_kind="deepseek"
    )

    _ = [
        event
        async for event in provider.stream(
            SimpleNamespace(max_tokens=512, temperature=0.0, tool_choice="auto"),
            SimpleNamespace(specs=[]),
            [],
            asyncio.Event(),
        )
    ]

    assert provider.thinking_enabled is False
    assert client.calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}


@pytest.mark.asyncio
async def test_deepseek_thinking_can_be_enabled_explicitly():
    client = _StreamClient([_chunk(content="done", finish_reason="stop")])
    provider = ResponsesProvider(
        "deepseek-pro",
        client=client,
        provider_kind="deepseek",
        thinking=True,
    )

    _ = [
        event
        async for event in provider.stream(
            SimpleNamespace(max_tokens=512, temperature=0.0, tool_choice="auto"),
            SimpleNamespace(specs=[]),
            [],
            asyncio.Event(),
        )
    ]

    assert provider.thinking_enabled is True
    assert client.calls[0]["extra_body"] == {"thinking": {"type": "enabled"}}


def test_non_deepseek_provider_rejects_thinking_mode():
    with pytest.raises(ValueError, match="only supported by the DeepSeek provider"):
        ResponsesProvider("openai-test", provider_kind="openai", thinking=True)


def test_deepseek_tool_call_replays_complete_reasoning_content():
    messages = [
        ModelResponse(
            parts=[
                ThinkingPart(content="first thought; second thought"),
                ToolCallPart(tool_name="probe", args="{}", tool_call_id="call-1"),
            ]
        )
    ]

    assert _to_api(messages) == [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "probe", "arguments": "{}"},
                }
            ],
            "reasoning_content": "first thought; second thought",
        }
    ]


@pytest.mark.asyncio
async def test_deepseek_stream_emits_private_reasoning_separately():
    client = _StreamClient(
        [
            _chunk(reasoning_content="first "),
            _chunk(reasoning_content="second", finish_reason="stop"),
        ]
    )
    provider = ResponsesProvider(
        "deepseek-pro", client=client, provider_kind="deepseek", thinking=True
    )

    events = [
        event
        async for event in provider.stream(
            SimpleNamespace(max_tokens=512, temperature=0.0, tool_choice="auto"),
            SimpleNamespace(specs=[]),
            [],
            asyncio.Event(),
        )
    ]

    reasoning = [event for event in events if event.kind == "reasoning_delta"]
    assert [event.data["delta"] for event in reasoning] == ["first ", "second"]
    assert events[-1].data["reasoning_content"] == "first second"


@pytest.mark.asyncio
async def test_deepseek_stream_strips_dsml_from_accumulated():
    client = _StreamClient(
        [
            _chunk(content='{"decision":"submit",'),
            _chunk(
                content='  "reason":"ok"}<｜DSML｜tool_calls>body</｜DSML｜tool_calls>',
                finish_reason="stop",
            ),
        ]
    )
    provider = ResponsesProvider(
        "deepseek-test", client=client, provider_kind="deepseek"
    )

    events = []
    async for event in provider.stream(
        SimpleNamespace(max_tokens=512, temperature=0.0, tool_choice="auto"),
        SimpleNamespace(specs=[]),
        [],
        asyncio.Event(),
        output_type=None,
    ):
        events.append(event)

    accumulated = [e.data.get("accumulated") for e in events if e.kind == "text_delta"]
    final = [e for e in events if e.kind == "response_completed"][0]
    assert "DSML" not in "".join(accumulated)
    assert "DSML" not in final.data["accumulated_text"]
    assert final.data["accumulated_text"].startswith('{"decision":"submit"')


@pytest.mark.asyncio
async def test_openai_stream_is_untouched_by_dsml_filter():
    client = _StreamClient([_chunk(content='{"a": 1}', finish_reason="stop")])
    provider = ResponsesProvider("openai-test", client=client, provider_kind="openai")

    events = []
    async for event in provider.stream(
        SimpleNamespace(max_tokens=512, temperature=0.0, tool_choice="auto"),
        SimpleNamespace(specs=[]),
        [],
        asyncio.Event(),
        output_type=None,
    ):
        events.append(event)

    final = [e for e in events if e.kind == "response_completed"][0]
    assert final.data["accumulated_text"] == '{"a": 1}'
