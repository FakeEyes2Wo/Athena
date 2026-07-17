"""Unit tests for single_turn_chat."""

import unittest

from pydantic import BaseModel

from athena.utils.single_turn_chat import single_turn_chat
from unit.fakes import FakeChatModel


class Answer(BaseModel):
    value: str


class SingleTurnChatTest(unittest.IsolatedAsyncioTestCase):
    async def test_returns_structured_result_from_model(self) -> None:
        fake = FakeChatModel([Answer(value="hi")])
        result = await single_turn_chat("say hi", Answer, model=fake)
        self.assertEqual("hi", result.value)

    async def test_does_not_retry_on_its_own(self) -> None:
        # single_turn_chat 的 docstring 明确"重试由调用方负责"；这里验证它确实不吞异常、不自己重试
        fake = FakeChatModel([ValueError("boom")])
        with self.assertRaises(ValueError):
            await single_turn_chat("say hi", Answer, model=fake)
