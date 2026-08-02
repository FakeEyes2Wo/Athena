"""Unit tests for the shared LLM test doubles."""

import unittest

from pydantic import BaseModel
from pydantic_ai import Agent

from unit.fakes import make_routed_model


class _Answer(BaseModel):
    value: str


class MakeRoutedModelTest(unittest.IsolatedAsyncioTestCase):
    async def test_routes_by_substring(self) -> None:
        model = make_routed_model({
            "alpha branch": _Answer(value="a"),
            "beta branch": _Answer(value="b"),
        })
        agent = Agent(output_type=_Answer)
        result = await agent.run("please take the beta branch", model=model)
        self.assertEqual("b", result.output.value)

    async def test_same_key_can_be_hit_repeatedly(self) -> None:
        # 与 make_scripted_model 不同：路由版不消费响应，同一 key 可被并发的多个候选重复命中
        model = make_routed_model({"alpha branch": _Answer(value="a")})
        agent = Agent(output_type=_Answer)
        for _ in range(3):
            result = await agent.run("alpha branch", model=model)
            self.assertEqual("a", result.output.value)

    async def test_zero_hits_raises_with_prompt_excerpt(self) -> None:
        model = make_routed_model({"alpha branch": _Answer(value="a")})
        agent = Agent(output_type=_Answer)
        with self.assertRaises(AssertionError) as ctx:
            await agent.run("nothing matches here", model=model)
        self.assertIn("nothing matches here", str(ctx.exception))

    async def test_multiple_hits_raises_instead_of_picking_longest(self) -> None:
        # 最长匹配会静默选错，正好违背"避免静默返回错误响应"的初衷
        model = make_routed_model({
            "structured": _Answer(value="short"),
            "structured list of gaps": _Answer(value="long"),
        })
        agent = Agent(output_type=_Answer)
        with self.assertRaises(AssertionError):
            await agent.run("a structured list of gaps", model=model)

    async def test_exception_value_is_raised(self) -> None:
        model = make_routed_model({"alpha branch": ValueError("boom")})
        agent = Agent(output_type=_Answer)
        with self.assertRaises(ValueError):
            await agent.run("alpha branch", model=model)
