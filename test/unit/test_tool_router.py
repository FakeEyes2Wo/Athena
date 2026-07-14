"""Unit tests for the minimal ToolRouter contract."""

import unittest

from athena.tool_router import ToolRouter


class EchoAdapter:
    async def invoke(self, request_ref: str) -> str:
        return f"result://{request_ref}"


class ToolRouterTest(unittest.IsolatedAsyncioTestCase):
    async def test_dispatches_to_registered_adapter(self) -> None:
        router = ToolRouter()
        router.register("echo", EchoAdapter())

        self.assertEqual(("echo",), router.capabilities)
        self.assertEqual("result://request/1", await router.invoke("echo", "request/1"))

    async def test_rejects_duplicate_and_unknown_capabilities(self) -> None:
        router = ToolRouter()
        router.register("echo", EchoAdapter())

        with self.assertRaises(ValueError):
            router.register("echo", EchoAdapter())
        with self.assertRaises(KeyError):
            router.resolve("missing")
