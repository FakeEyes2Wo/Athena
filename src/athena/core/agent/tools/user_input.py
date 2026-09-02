"""``request_user_input`` 工具 — LLM 需要澄清时向用户提问并等待回答。

对齐 Codex ``request_user_input``（codex-rs/core/src/tools/handlers/request_user_input.rs）：
模型调用工具 → ``ctx.ask_user`` 阻塞等待 → 回答作为工具结果返回 → 经
``ToolReturnPart`` 写回 memory，采样循环自然继续，无需特殊处理。

Athena 的 typed broker 以 ``HumanRequest`` / ``HumanOutcome`` 表达同一交互；
本工具把 typed outcome 转换成旧版字符串工具结果，供 Agent 读取。旧的
``(prompt) -> 回答文本`` 回调仍可直接使用。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from athena.core.human_request import (
    HumanChoice,
    HumanOutcome,
    HumanRequest,
    HumanReply,
    parse_human_reply,
)
from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolSpec
from athena.core.contracts import new_id


class RequestUserInputTool(BaseTool):
    """当用户意图不明确、缺少关键信息或需要澄清时向用户提问。"""

    spec = ToolSpec(
        name="request_user_input",
        description=(
            "当用户意图不明确、缺少关键信息或需要澄清时，向用户提出一个问题。"
            "传入具体、可回答的问题，不要臆测用户意图。可以附带 2-3 个候选答案。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "向用户提出的澄清问题",
                },
                "choices": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        "required": ["label", "value"],
                        "additionalProperties": False,
                    },
                    "description": "可选：2-3 个候选答案",
                },
                "allow_custom": {
                    "type": "boolean",
                    "default": True,
                },
                "allow_skip": {
                    "type": "boolean",
                    "default": True,
                },
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    )

    async def execute(self, input: dict, ctx: ToolContext) -> str:
        if ctx.ask_user is None:
            raise RuntimeError("ask_user 未注入，无法请求用户输入")
        choices = input.get("choices")
        allow_custom = input.get("allow_custom", True)
        allow_skip = input.get("allow_skip", True)
        if (not choices and allow_custom is True and allow_skip is True) or (
            choices and len(choices) not in (2, 3)
        ):
            # 旧 ask_user 回调只接受 prompt；无 choices 或非 2/3 个选项时保持旧的
            # 扩展字符串路径（可选的单选项场景来自历史工具测试）。
            if not choices:
                answer = await ctx.ask_user(input["prompt"])  # type: ignore[call-arg]
            else:
                answer = await ctx.ask_user(  # type: ignore[call-arg]
                    input["prompt"],
                    choices=choices,
                    allow_custom=allow_custom,
                    allow_skip=allow_skip,
                )
            return answer if answer is not None else "[用户未在超时内回答]"

        request = self._build_request(
            input["prompt"],
            choices=choices or [],
            allow_custom=allow_custom,
            allow_skip=allow_skip,
            ctx=ctx,
        )
        try:
            outcome = await ctx.ask_user(request)
        except TypeError:
            # 兼容旧回调：新协议不接受 HumanRequest 时，退回扩展字符串路径。
            answer = await ctx.ask_user(  # type: ignore[call-arg]
                input["prompt"],
                choices=choices,
                allow_custom=allow_custom,
                allow_skip=allow_skip,
            )
            return answer if answer is not None else "[用户未在超时内回答]"

        if isinstance(outcome, HumanOutcome):
            return outcome.to_tool_text()
        if outcome is None:
            return "[用户未在超时内回答]"
        if isinstance(outcome, HumanReply):
            parsed = parse_human_reply(outcome)
            if parsed.kind == "skip":
                return "Skipped by user"
            if parsed.kind == "text":
                return parsed.text
            return parsed.value
        if isinstance(outcome, str):
            return outcome
        return str(outcome)

    @staticmethod
    def _build_request(
        prompt: str,
        *,
        choices: list[dict[str, str]] | list[HumanChoice],
        allow_custom: bool,
        allow_skip: bool,
        ctx: ToolContext,
    ) -> HumanRequest:
        """Build a typed request while preserving the tool's runtime scope."""
        now = datetime.now(UTC)
        request_id = new_id("human")
        return HumanRequest(
            request_id=request_id,
            session_id=ctx.session_id,
            scope_id="runtime",
            scope_kind="runtime",
            prompt=prompt,
            choices=[
                (
                    choice
                    if isinstance(choice, HumanChoice)
                    else HumanChoice(label=choice["label"], value=choice["value"])
                )
                for choice in choices
            ],
            allow_custom=bool(allow_custom),
            allow_skip=bool(allow_skip),
            created_at=now,
            expires_at=now + timedelta(minutes=2),
        )
