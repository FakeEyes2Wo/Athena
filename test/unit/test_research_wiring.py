"""组合根测试 — 环境变量解析、编码器、视觉解释器与工具注册。"""

import base64
import json
import tempfile
import unittest
from unittest import mock

from athena.research.paper_markdown.interfaces import VisualInterpretationRequest
from athena.research.paper_markdown.schemas import SourceLocator
from athena.research.paper_source.http import HostRateLimiter
from athena.research.wiring import (
    ARTIFACT_ROOT_ENV,
    DEFAULT_ARTIFACT_ROOT,
    RESEARCH_MODEL_ENV,
    TUI_MODEL_ENV,
    OpenAIEmbedder,
    ResearchStack,
    VisionInterpreter,
    build_artifact_store,
    build_research_tools,
    resolve_model,
)
from athena.storage.artifact_store import LocalArtifactStore


class FakeEmbeddingItem:
    def __init__(self, index: int, embedding: list[float]) -> None:
        self.index = index
        self.embedding = embedding


class FakeEmbeddings:
    """按倒序返回，用来验证调用方确实按 ``index`` 重排。"""

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    async def create(self, *, model: str, input: list[str]):
        self.batches.append(list(input))
        items = [
            FakeEmbeddingItem(position, [float(position), float(len(text))])
            for position, text in enumerate(input)
        ]
        return type("Resp", (), {"data": list(reversed(items))})()


class FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = type("Msg", (), {"content": content})()


class FakeCompletions:
    def __init__(self, content: str = "", error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.messages: list = []

    async def create(self, **kwargs):
        self.messages.append(kwargs["messages"])
        if self.error is not None:
            raise self.error
        return type("Resp", (), {"choices": [FakeChoice(self.content)]})()


class FakeClient:
    def __init__(self, content: str = "", error: Exception | None = None) -> None:
        self.embeddings = FakeEmbeddings()
        self.completions = FakeCompletions(content, error)
        self.chat = type("Chat", (), {"completions": self.completions})()


def store() -> LocalArtifactStore:
    return LocalArtifactStore(tempfile.mkdtemp(prefix="wiring_"))


class ResolveModelTest(unittest.TestCase):
    def test_explicit_argument_wins(self) -> None:
        with mock.patch.dict(
            "os.environ", {RESEARCH_MODEL_ENV: "env-model"}, clear=False
        ):
            self.assertEqual("explicit", resolve_model("explicit"))

    def test_research_env_beats_tui_env(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {RESEARCH_MODEL_ENV: "research", TUI_MODEL_ENV: "tui"},
            clear=False,
        ):
            self.assertEqual("research", resolve_model())

    def test_falls_back_to_tui_model(self) -> None:
        with mock.patch.dict(
            "os.environ", {RESEARCH_MODEL_ENV: "", TUI_MODEL_ENV: "tui"}, clear=False
        ):
            self.assertEqual("tui", resolve_model())


class ArtifactRootTest(unittest.TestCase):
    def test_explicit_root_wins_over_environment(self) -> None:
        root = tempfile.mkdtemp(prefix="wiring_root_")
        with mock.patch.dict(
            "os.environ", {ARTIFACT_ROOT_ENV: "should-not-be-used"}, clear=False
        ):
            built = build_artifact_store(root)
        self.assertTrue(str(built.path_for("sha256:" + "0" * 64)).startswith(root))

    def test_default_root_is_outside_the_repository(self) -> None:
        self.assertNotIn("Athena/Athena", DEFAULT_ARTIFACT_ROOT.as_posix())
        self.assertTrue(DEFAULT_ARTIFACT_ROOT.as_posix().endswith(".athena/artifacts"))


class EmbedderTest(unittest.IsolatedAsyncioTestCase):
    async def test_vectors_follow_input_order_despite_shuffled_response(self) -> None:
        client = FakeClient()
        embedder = OpenAIEmbedder(client, "m", batch_size=8)

        vectors = await embedder.embed(["a", "bb", "ccc"])

        self.assertEqual([[0.0, 1.0], [1.0, 2.0], [2.0, 3.0]], vectors)

    async def test_batches_respect_the_configured_size(self) -> None:
        client = FakeClient()
        embedder = OpenAIEmbedder(client, "m", batch_size=2)

        await embedder.embed(["a", "b", "c", "d", "e"])

        self.assertEqual([2, 2, 1], [len(item) for item in client.embeddings.batches])
        self.assertEqual(3, embedder.calls)
        self.assertEqual(5, embedder.embedded)

    async def test_empty_input_makes_no_request(self) -> None:
        client = FakeClient()
        embedder = OpenAIEmbedder(client, "m")

        self.assertEqual([], await embedder.embed([]))
        self.assertEqual(0, embedder.calls)


class VisionInterpreterTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = store()
        self.context_ref = await self.store.put_text("Figure 1: accuracy over epochs.")
        self.asset_ref = await self.store.put_bytes(b"\x89PNG-not-a-real-image")
        self.locator = SourceLocator(source_kind="tex", file="main.tex")

    def request(self, **overrides) -> VisualInterpretationRequest:
        payload = {
            "visual_id": "figure-1",
            "kind": "figure",
            "asset_ref": self.asset_ref,
            "media_type": "image/png",
            "context_ref": self.context_ref,
            "locator": self.locator,
        }
        payload.update(overrides)
        return VisualInterpretationRequest(**payload)

    async def test_image_is_inlined_as_a_data_uri(self) -> None:
        client = FakeClient(json.dumps({"summary": "s", "searchable_text": "t"}))
        interpreter = VisionInterpreter(client, "vl", self.store)

        await interpreter.interpret(self.request())

        content = client.completions.messages[0][0]["content"]
        encoded = base64.b64encode(b"\x89PNG-not-a-real-image").decode("ascii")
        self.assertEqual("text", content[0]["type"])
        self.assertEqual(
            f"data:image/png;base64,{encoded}", content[1]["image_url"]["url"]
        )

    async def test_structured_text_is_included_when_present(self) -> None:
        table_ref = await self.store.put_text("| method | acc |")
        client = FakeClient('{"summary": "s", "searchable_text": "t"}')
        interpreter = VisionInterpreter(client, "vl", self.store)

        await interpreter.interpret(
            self.request(kind="table", structured_text_ref=table_ref)
        )

        prompt = client.completions.messages[0][0]["content"][0]["text"]
        self.assertIn("| method | acc |", prompt)
        self.assertIn("Element kind: table", prompt)

    async def test_text_only_request_sends_no_image_part(self) -> None:
        table_ref = await self.store.put_text("| method | acc |")
        client = FakeClient('{"summary": "s", "searchable_text": "t"}')
        interpreter = VisionInterpreter(client, "vl", self.store)

        await interpreter.interpret(
            self.request(kind="table", asset_ref=None, structured_text_ref=table_ref)
        )

        content = client.completions.messages[0][0]["content"]
        self.assertEqual(1, len(content))

    async def test_json_reply_is_parsed_into_fields(self) -> None:
        payload = {
            "summary": "Accuracy rises then plateaus.",
            "searchable_text": "Accuracy increases with epochs and plateaus.",
            "structured_data": {"x": "epochs"},
        }
        client = FakeClient(f"Here you go:\n```json\n{json.dumps(payload)}\n```")
        interpreter = VisionInterpreter(client, "vl", self.store)

        result = await interpreter.interpret(self.request())

        self.assertEqual(payload["summary"], result.summary)
        self.assertEqual(payload["searchable_text"], result.searchable_text)
        self.assertEqual({"x": "epochs"}, result.structured_data)
        self.assertEqual("vl", result.model)

    async def test_unparseable_reply_falls_back_to_raw_text(self) -> None:
        client = FakeClient("The figure plots accuracy against epochs.")
        interpreter = VisionInterpreter(client, "vl", self.store)

        result = await interpreter.interpret(self.request())

        self.assertEqual("The figure plots accuracy against epochs.", result.summary)
        self.assertEqual(result.summary, result.searchable_text)
        self.assertEqual({}, result.structured_data)

    async def test_status_never_marks_a_successful_reply_unavailable(self) -> None:
        client = FakeClient("free text")
        interpreter = VisionInterpreter(client, "vl", self.store)

        result = await interpreter.interpret(self.request())

        self.assertNotEqual(
            "unavailable", result.structured_data.get("interpretation_status")
        )

    async def test_failure_is_counted_and_propagated(self) -> None:
        client = FakeClient(error=ConnectionError("endpoint down"))
        interpreter = VisionInterpreter(client, "vl", self.store)

        with self.assertRaises(ConnectionError):
            await interpreter.interpret(self.request())

        self.assertEqual(1, interpreter.calls)
        self.assertEqual(1, interpreter.failures)


class ToolRegistrationTest(unittest.TestCase):
    def stack(self, embedder) -> ResearchStack:
        return ResearchStack(
            artifacts=store(),
            client=FakeClient(),
            model="m",
            http=HostRateLimiter(),
            embedder=embedder,
        )

    def test_all_eight_tools_register_when_the_embedder_exists(self) -> None:
        names = [
            item.name
            for item in build_research_tools(
                self.stack(OpenAIEmbedder(FakeClient(), "e"))
            ).specs
        ]

        self.assertEqual(
            [
                "paper_chunk_read",
                "paper_cites",
                "paper_fetch",
                "paper_keyword_search",
                "paper_markdown",
                "paper_section_search",
                "paper_semantic_search",
                "paper_visual_of",
            ],
            names,
        )

    def test_semantic_search_is_omitted_without_an_embedder(self) -> None:
        names = [item.name for item in build_research_tools(self.stack(None)).specs]

        self.assertNotIn("paper_semantic_search", names)
        self.assertIn("paper_keyword_search", names)
