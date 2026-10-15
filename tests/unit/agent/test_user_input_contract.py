"""Typed human responses retain the legacy tool text boundary."""

import asyncio

import pytest

from athena.core.agent.tools.user_input import RequestUserInputTool
from athena.core.human_request import ChoiceReply, HumanRequest, SkipReply, TextReply
from athena.core.tool_types import ToolContext


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (ChoiceReply(kind="choice", value="f1"), "f1"),
        (TextReply(kind="text", text="custom metric"), "custom metric"),
        (SkipReply(kind="skip"), "Skipped by user"),
        ("legacy answer", "legacy answer"),
    ],
)
@pytest.mark.asyncio
async def test_typed_request_accepts_reply_models_and_legacy_text(reply, expected):
    async def ask_user(request):
        assert isinstance(request, HumanRequest)
        assert request.session_id == "session-contract"
        assert [choice.value for choice in request.choices] == ["f1", "auc"]
        return reply

    ctx = ToolContext(
        "request_user_input",
        "typed-reply",
        lambda *_args: asyncio.sleep(0),
        asyncio.Event(),
        ask_user=ask_user,
        session_id="session-contract",
    )
    result = await RequestUserInputTool().execute(
        {
            "prompt": "Choose metric",
            "choices": [
                {"label": "F1", "value": "f1"},
                {"label": "AUC", "value": "auc"},
            ],
        },
        ctx,
    )
    assert result == expected
