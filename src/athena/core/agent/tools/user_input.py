"""``request_user_input`` 工具 — LLM 需要澄清时向用户提问并等待回答。

对齐 Codex ``request_user_input``（codex-rs/core/src/tools/handlers/request_user_input.rs）：
模型调用工具 → ``ctx.ask_user`` 阻塞等待 → 回答作为工具结果返回 → 经
``ToolReturnPart`` 写回 memory，采样循环自然继续，无需特殊处理。
"""

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolSpec


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
        if not choices and allow_custom is True and allow_skip is True:
            # 旧 ask_user 回调只接受 prompt；无 choices 时保持旧调用路径。
            answer = await ctx.ask_user(input["prompt"])
        else:
            answer = await ctx.ask_user(
                input["prompt"],
                choices=choices,
                allow_custom=allow_custom,
                allow_skip=allow_skip,
            )
        return answer if answer is not None else "[用户未在超时内回答]"
