"""组合根测试 — 环境变量解析、编码器、视觉解释器与工具注册。"""

import asyncio
import base64
import json
import tempfile
from pathlib import Path
import unittest
from unittest import mock

import httpx
from openai import RateLimitError

from athena.core.agent import settings
from athena.core.artifact_store import LocalArtifactStore
from athena.research.literature.paper_markdown.models import (
    SourceLocator,
    VisualInterpretationRequest,
)
from athena.research.literature.paper_source.http import HostRateLimiter
from athena.research.literature.survey.wiring import (
    ARTIFACT_ROOT_ENV,
    DEFAULT_ARTIFACT_ROOT,
    EMBED_MAX_RETRIES,
    SCORER_MODEL_ENV,
    SURVEY_MODEL_ENV,
    OpenAIEmbedder,
    SurveyStack,
    VisionInterpreter,
    build_artifact_store,
    build_survey_tools,
    resolve_model,
    resolve_scorer_model,
)


class FakeEmbeddingItem:
    def __init__(self, index: int, embedding: list[float]) -> None:
        self.index = index
        self.embedding = embedding


def rate_limited() -> RateLimitError:
    """构造一个真实形状的 429，重试逻辑捕获的就是这个类型。"""
    request = httpx.Request("POST", "https://example.invalid/embeddings")
    return RateLimitError(
        "Allocated quota exceeded",
        response=httpx.Response(429, request=request),
        body=None,
    )


async def no_sleep(_seconds: float) -> None:
    """让退避不真的等待，否则重试测试要跑几十秒。"""


class FakeEmbeddings:
    """按倒序返回，用来验证调用方确实按 ``index`` 重排。

    ``fail_times`` 模拟限流，``peak_inflight`` 记录同时在途的请求数——后者是并发上限
    这条约束唯一能观测到的地方。
    """

    def __init__(self) -> None:
        self.batches: list[list[str]] = []
        self.fail_times = 0
        self.inflight = 0
        self.peak_inflight = 0

    async def create(self, *, model: str, input: list[str]):
        self.inflight += 1
        self.peak_inflight = max(self.peak_inflight, self.inflight)
        try:
            # 让出一次控制权，否则协程从头跑到尾，永远观测不到并发
            await asyncio.sleep(0)
            if self.fail_times > 0:
                self.fail_times -= 1
                raise rate_limited()
            self.batches.append(list(input))
            items = [
                FakeEmbeddingItem(position, [float(position), float(len(text))])
                for position, text in enumerate(input)
            ]
            return type("Resp", (), {"data": list(reversed(items))})()
        finally:
            self.inflight -= 1


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
            "os.environ", {SURVEY_MODEL_ENV: "env-model"}, clear=False
        ):
            self.assertEqual("explicit", resolve_model("explicit"))

    def test_survey_env_beats_repository_default(self) -> None:
        with mock.patch.dict("os.environ", {SURVEY_MODEL_ENV: "survey"}, clear=False):
            self.assertEqual("survey", resolve_model())

    def test_falls_back_to_repository_default_model(self) -> None:
        """不设 survey 专用变量时沿用全仓默认，而不是留空让装配自检报缺模型。"""
        with mock.patch.dict("os.environ", {SURVEY_MODEL_ENV: ""}, clear=False):
            self.assertEqual(settings.model_name(), resolve_model())


class ScorerModelTest(unittest.TestCase):
    """打分模型可以和策略模型分开配置，不配就沿用策略模型。"""

    def test_explicit_argument_wins(self) -> None:
        with mock.patch.dict(
            "os.environ", {SCORER_MODEL_ENV: "env-scorer"}, clear=False
        ):
            self.assertEqual("explicit", resolve_scorer_model("explicit"))

    def test_reads_its_own_environment_variable(self) -> None:
        with mock.patch.dict("os.environ", {SCORER_MODEL_ENV: "flash"}, clear=False):
            self.assertEqual("flash", resolve_scorer_model())

    def test_unset_means_reuse_the_policy_model(self) -> None:
        """不设时行为必须与分开之前完全一致，否则这是个破坏性默认值。"""
        with mock.patch.dict("os.environ", {SCORER_MODEL_ENV: ""}, clear=False):
            self.assertEqual("", resolve_scorer_model())
        stack = SurveyStack(
            artifacts=mock.MagicMock(),
            client=mock.MagicMock(),
            model="policy",
            http=HostRateLimiter(),
        )
        self.assertEqual("policy", stack.effective_scorer_model())

    def test_configured_scorer_overrides_the_policy_model(self) -> None:
        stack = SurveyStack(
            artifacts=mock.MagicMock(),
            client=mock.MagicMock(),
            model="policy",
            http=HostRateLimiter(),
            scorer_model="flash",
        )
        self.assertEqual("flash", stack.effective_scorer_model())


class ArtifactRootTest(unittest.TestCase):
    def test_explicit_root_wins_over_environment(self) -> None:
        root = tempfile.mkdtemp(prefix="wiring_root_")
        with mock.patch.dict(
            "os.environ", {ARTIFACT_ROOT_ENV: "should-not-be-used"}, clear=False
        ):
            built = build_artifact_store(root)
        # 按路径比较而非字符串前缀：mkdtemp() 跟随 TEMP 环境变量的大小写
        # （Windows 上常是 C:\WINDOWS\TEMP），而 build_artifact_store 内部
        # resolve() 会归一成文件系统的真实大小写，前缀比较于是假失败。
        built_path = Path(built.path_for("sha256:" + "0" * 64)).resolve()
        self.assertTrue(built_path.is_relative_to(Path(root).resolve()))

    def test_default_root_is_outside_the_repository(self) -> None:
        self.assertNotIn("Athena/Athena", DEFAULT_ARTIFACT_ROOT.as_posix())
        self.assertTrue(DEFAULT_ARTIFACT_ROOT.as_posix().endswith(".athena/artifacts"))


class EmbedderTest(unittest.IsolatedAsyncioTestCase):
    async def test_vectors_follow_input_order_despite_shuffled_response(self) -> None:
        client = FakeClient()
        embedder = OpenAIEmbedder(client, "m", batch_size=8)

        vectors = await embedder.embed(["a", "bb", "ccc"])

        self.assertEqual([[0.0, 1.0], [1.0, 2.0], [2.0, 3.0]], vectors)

    async def test_in_flight_requests_are_capped(self) -> None:
        """真机命中：50 篇约 39000 条句子、两千多个批次同时打出去，撞出 insufficient_quota。

        撞上的时机最难受——取源和转换都做完了，钱已经花掉，报告却拿不到。
        """
        client = FakeClient()
        embedder = OpenAIEmbedder(client, "m", batch_size=1, concurrency=4)

        await embedder.embed([str(index) for index in range(40)])

        self.assertEqual(4, client.embeddings.peak_inflight)
        self.assertEqual(40, len(client.embeddings.batches))

    async def test_a_rate_limited_batch_is_retried(self) -> None:
        client = FakeClient()
        client.embeddings.fail_times = 2
        embedder = OpenAIEmbedder(client, "m", batch_size=8)

        with mock.patch("asyncio.sleep", new=no_sleep):
            vectors = await embedder.embed(["a", "bb"])

        self.assertEqual(2, len(vectors))
        self.assertEqual(2, embedder.retries)
        self.assertEqual(3, embedder.calls)

    async def test_retrying_stops_and_surfaces_the_failure(self) -> None:
        """重试不能无限：真的没配额时要让调用方看见，而不是挂在那里。"""
        client = FakeClient()
        client.embeddings.fail_times = 99
        embedder = OpenAIEmbedder(client, "m")

        with mock.patch("asyncio.sleep", new=no_sleep):
            with self.assertRaises(RateLimitError):
                await embedder.embed(["a"])

        self.assertEqual(EMBED_MAX_RETRIES, embedder.calls)

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
    def stack(self, embedder) -> SurveyStack:
        return SurveyStack(
            artifacts=store(),
            client=FakeClient(),
            model="m",
            http=HostRateLimiter(),
            embedder=embedder,
        )

    def test_every_tool_registers_when_the_embedder_exists(self) -> None:
        names = [
            item.name
            for item in build_survey_tools(
                self.stack(OpenAIEmbedder(FakeClient(), "e"))
            ).specs
        ]

        self.assertEqual(
            [
                "paper_chunk_read",
                "paper_cites",
                "paper_corpus_overview",
                "paper_fetch",
                "paper_keyword_search",
                "paper_markdown",
                "paper_search",
                "paper_section_search",
                "paper_semantic_search",
                "paper_survey",
                "paper_visual_of",
            ],
            names,
        )

    def test_the_embedding_channels_are_omitted_without_an_embedder(self) -> None:
        """没有向量时 paper_search 会退化成纯词面，与 paper_keyword_search 完全重复。"""
        names = [item.name for item in build_survey_tools(self.stack(None)).specs]

        self.assertNotIn("paper_semantic_search", names)
        self.assertNotIn("paper_search", names)
        self.assertIn("paper_keyword_search", names)

    def test_the_survey_tool_can_be_left_out_for_corpus_only_agents(self) -> None:
        """已经拿到 corpus_ref 的 Agent 不该看见那个几分钟起步的全链路工具。"""
        names = [
            item.name
            for item in build_survey_tools(self.stack(None), include_survey=False).specs
        ]

        self.assertNotIn("paper_survey", names)
        self.assertIn("paper_chunk_read", names)

    def test_a_read_only_agent_gets_neither_the_survey_nor_the_producers(self) -> None:
        """Ideator 只读语料：取源与转换会写出新语料，不该出现在它的工具表里。"""
        names = {
            item.name
            for item in build_survey_tools(
                self.stack(None), include_survey=False, include_producers=False
            ).specs
        }

        self.assertEqual(
            set(), names & {"paper_survey", "paper_fetch", "paper_markdown"}
        )
        self.assertIn("paper_corpus_overview", names)
        self.assertIn("paper_chunk_read", names)

    def test_every_retrieval_tool_shares_one_session(self) -> None:
        """七个算子各建一个会话时，同一份语料会被解码七次，已读账本也只对自己成立。"""
        tools = build_survey_tools(self.stack(OpenAIEmbedder(FakeClient(), "e")))
        sessions = {
            id(tools.resolve(name).runtime.session)
            for name in (
                "paper_corpus_overview",
                "paper_keyword_search",
                "paper_chunk_read",
                "paper_visual_of",
                "paper_cites",
                "paper_section_search",
                "paper_semantic_search",
            )
        }

        self.assertEqual(1, len(sessions))

    def test_separate_tool_sets_share_the_stack_corpus_cache(self) -> None:
        """三条 Ideator lane 各拿一套工具，但语料只该解码一份。"""
        stack = self.stack(None)
        first = build_survey_tools(stack).resolve("paper_keyword_search")
        second = build_survey_tools(stack).resolve("paper_keyword_search")

        self.assertIsNot(first.runtime.session, second.runtime.session)
        self.assertIs(
            first.runtime.session._cache,
            second.runtime.session._cache,
        )
