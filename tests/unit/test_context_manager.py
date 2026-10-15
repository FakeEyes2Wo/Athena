"""``athena.memory.context_manager.ContextManager`` 的单元测试。"""

import subprocess
import sys

import pytest

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    UserPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnContent,
    ToolReturnPart,
)


def _user(text: str) -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(content=text)])


def _assistant(text: str) -> ModelResponse:
    return ModelResponse(parts=[TextPart(content=text)])


def _tool_call(name: str, args: dict) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool_name=name, args=args)])


def _tool_result(
    name: str, content: ToolReturnContent, call_id: str = "c1"
) -> ModelRequest:
    return ModelRequest(
        parts=[
            ToolReturnPart(
                tool_name=name,
                content=content,
                tool_call_id=call_id,
            )
        ]
    )


def test_memory_can_be_imported_before_the_core_facade():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from athena.memory import ContextManager; from athena.core import Agent",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


class TestContextManager:

    @pytest.mark.parametrize("write", ["append", "replace_range"])
    def test_nested_tool_result_input_cannot_change_owned_history(self, write):
        from athena.memory.context_manager import ContextManager

        content = {"rows": [[1, 2]]}
        message = _tool_result("bash", content)
        ctx = ContextManager()
        if write == "append":
            ctx.append(message)
        else:
            ctx.replace_range(0, 0, [message])
        snapshot, tokens = ctx.snapshot(), ctx.tokens
        content["rows"][0][0] = 99
        message.parts[0].tool_name = "changed"
        assert ctx.items[0].parts[0].content == {"rows": [[1, 2]]}
        assert ctx.items[0].parts[0].tool_name == "bash"
        assert ctx.snapshot() == snapshot
        assert ctx.tokens == tokens

    @pytest.mark.parametrize("write", ["append", "replace_range"])
    def test_nested_input_mutation_cannot_change_owned_history(self, write):
        from athena.memory.context_manager import ContextManager

        args = {"commands": ["read"]}
        message = _tool_call("bash", args)
        ctx = ContextManager()
        if write == "append":
            ctx.append(message)
        else:
            ctx.replace_range(0, 0, [message])
        snapshot, tokens = ctx.snapshot(), ctx.tokens
        args["commands"].append("injected" * 100)
        message.parts[0].tool_name = "changed"
        assert ctx.items[0].parts[0].args == {"commands": ["read"]}
        assert ctx.items[0].parts[0].tool_name == "bash"
        assert ctx.snapshot() == snapshot
        assert ctx.tokens == tokens

    @pytest.mark.parametrize("view", ["items", "items_since"])
    def test_nested_view_mutation_cannot_rewrite_history(self, view):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        ctx.append(_tool_call("bash", {"commands": ["read"]}))
        ctx.append(_tool_result("bash", {"rows": [[1, 2]]}))
        messages = ctx.items if view == "items" else ctx.items_since(0)
        messages[0].parts[0].args["commands"].append("injected")
        messages[1].parts[0].content["rows"][0][0] = 99
        assert ctx.items[0].parts[0].args == {"commands": ["read"]}
        assert ctx.items[1].parts[0].content == {"rows": [[1, 2]]}

    def test_empty_context_has_zero_tokens(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        assert ctx.tokens == 0
        assert ctx.items == []
        assert ctx.version == 0

    def test_append_increments_tokens_and_version(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        ctx.append(_user("hello world"))
        assert ctx.tokens > 0
        assert ctx.version == 1
        assert len(ctx.items) == 1

    def test_append_multiple_accrues_tokens(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        ctx.append(_user("hello"))
        t1 = ctx.tokens
        ctx.append(_assistant("hi there"))
        assert ctx.tokens > t1
        assert ctx.version == 2

    def test_short_message_still_costs_tokens(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        ctx.append(_user("x"))
        assert ctx.tokens > 0

    def test_items_container_is_independent(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        ctx.append(_user("a"))
        copy = ctx.items
        copy.clear()
        assert len(ctx.items) == 1

    def test_large_tool_result_is_truncated(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        ctx.append(_tool_result("bash", "x" * 100_000))
        part = ctx.items[0].parts[0]
        c = getattr(part, "content", "")
        assert isinstance(c, str) and len(c) < 100_000
        assert "[TRUNCATED]" in c

    def test_truncation_preserves_head_and_tail_without_mutating_input(self):
        from athena.memory.context_manager import ContextManager

        original_content = "BEGIN" + ("x" * 60_000) + "END"
        msg = _tool_result("bash", original_content)
        ctx = ContextManager()
        ctx.append(msg)

        stored = getattr(ctx.items[0].parts[0], "content", "")
        assert stored.startswith("BEGIN")
        assert stored.endswith("END")
        assert len(stored) <= 50_000
        assert msg.parts[0].content == original_content

    def test_tool_result_under_limit_stays_intact(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        ctx.append(_tool_result("bash", "short output"))
        c = getattr(ctx.items[0].parts[0], "content", "")
        assert c == "short output"

    def test_system_prompt_not_truncated(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        huge = "y" * 60_000
        ctx.append(ModelRequest(parts=[SystemPromptPart(content=huge)]))
        c = getattr(ctx.items[0].parts[0], "content", "")
        assert isinstance(c, str) and len(c) == 60_000

    def test_token_margin_positive_when_under_limit(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager(context_limit=200_000)
        assert ctx.token_margin() == int(200_000 * 0.85)

    def test_token_margin_decreases_after_appends(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager(context_limit=200_000)
        m1 = ctx.token_margin()
        ctx.append(_user("hello world " * 100))
        assert ctx.token_margin() < m1


class TestReplaceRange:

    def test_replace_keeps_token_count_accurate(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        ctx.append(_user("hello world"))
        ctx.append(_assistant("hi back"))
        before = ctx.tokens
        ctx.replace_range(
            0, 2, [ModelRequest(parts=[SystemPromptPart(content="summary")])]
        )
        assert ctx.tokens < before
        assert len(ctx.items) == 1
        assert ctx.version == 3

    def test_replace_empty_range(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        ctx.append(_user("a"))
        before = ctx.tokens
        v0 = ctx.version
        ctx.replace_range(0, 0, [])
        assert ctx.tokens == before
        assert ctx.version == v0 + 1

    def test_replace_partial_range_keeps_tail(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        for text in ("first", "second", "third"):
            ctx.append(_user(text))
        ctx.replace_range(0, 2, [_user("merged")])
        assert len(ctx.items) == 2
        tail = getattr(ctx.items[1].parts[0], "content", "")
        assert tail == "third"

    def test_version_monotonic_across_operations(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        v0 = ctx.version
        ctx.append(_user("a"))
        v1 = ctx.version
        ctx.replace_range(0, 1, [_assistant("b")])
        v2 = ctx.version
        assert v0 < v1 < v2

    def test_replacement_applies_same_tool_output_limit(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        ctx.append(_user("old"))
        replacement = _tool_result("bash", "z" * 80_000)
        ctx.replace_range(0, 1, [replacement])
        stored = ctx.items[0].parts[0].content
        assert len(stored) <= 50_000
        assert "[TRUNCATED]" in stored
        assert replacement.parts[0].content == "z" * 80_000


class TestTokenEstimation:

    def test_english_text_estimate_is_plausible(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        ctx.append(_user("hello world " * 36))  # 约 400 字符
        assert 80 <= ctx.tokens <= 120

    def test_code_block_estimate_is_plausible(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        ctx.append(_assistant("def foo():\n    return 42\n" * 50))
        assert ctx.tokens > 0


class TestEdgeCases:

    def test_context_limit_is_stored(self):
        from athena.memory.context_manager import ContextManager

        assert ContextManager(context_limit=128_000).limit == 128_000

    def test_tool_call_arguments_contribute_tokens(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        ctx.append(_tool_call("bash", {"command": "ls"}))
        assert ctx.tokens > 0

    def test_mixed_parts_token_sum(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        ctx.append(
            ModelResponse(
                parts=[
                    TextPart(content="Let me check."),
                    ToolCallPart(tool_name="bash", args={"command": "ls"}),
                ]
            )
        )
        assert ctx.tokens > 0  # 仅 TextPart 被计数
