"""DeepSeek DSML tool-call 语法剥离。

DeepSeek 在 ``tool_choice=auto`` + 流式下会间歇性把 ``<｜DSML｜tool_calls>`` 等
标记片段泄漏进 ``content``，污染 json_object 结构化输出。这些测试锁定
``_DeepSeekTextFilter`` 的剥离行为（含跨 chunk 边界），并验证 provider 流式
集成的 ``accumulated`` 文本干净。
"""

import asyncio
from types import SimpleNamespace

import pytest

from athena.core.agent.provider import ResponsesProvider, _DeepSeekTextFilter


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


def _tool_call(
    index=0, call_id="call-1", name="shell_command", arguments='{"command": "ls"}'
):
    """构造一个流式 tool_call 增量（与 openai SDK 的 delta.tool_calls 形状一致）。"""
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


@pytest.mark.asyncio
async def test_deepseek_stream_emits_full_text_before_function_call():
    """过滤器扣留的尾部必须在 function_call 之前放出。

    ``agent/function_call`` 是投影层的消息边界：晚到的尾巴会让整条消息少一截，
    并被当成下一条消息的开头粘上去。
    """
    sentence = "Let me check the state.json to understand the task context better."
    client = _StreamClient(
        [
            _chunk(content=sentence),
            _chunk(tool_calls=[_tool_call()], finish_reason="tool_calls"),
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

    kinds = [e.kind for e in events]
    assert "function_call" in kinds
    call_at = kinds.index("function_call")
    before = "".join(
        e.data["delta"] for e in events[:call_at] if e.kind == "text_delta"
    )
    assert before == sentence
    # 边界之后不该再冒出属于上一条消息的文本。
    assert "text_delta" not in kinds[call_at:]
