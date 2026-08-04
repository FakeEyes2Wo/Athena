"""research 链路的组合根：把 artifact 存储、模型接口与论文工具装配起来。

四个 research 包（``paper_scout`` / ``paper_source`` / ``paper_markdown`` /
``paper_rag``）都按依赖注入写成：模块不在导入时创建客户端、不读环境变量。代价是
必须有一处把它们拼起来，本模块就是那一处，也是全仓唯一读 research 相关环境变量的
地方。``ResearchStack`` 之下的类只接受注入，因此单元测试可以完全绕开环境。

``VisionInterpreter`` 与 ``OpenAIEmbedder`` 是 ``VisualInterpreter`` 和
``TextEmbedder`` 两个协议的首个生产实现——在此之前它们只有测试里的 Fake，
``paper_markdown`` 的视觉解读与 ``paper_rag`` 的语义检索因此都无法真正启用。
"""

import asyncio
import base64
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from openai import AsyncOpenAI, RateLimitError

from athena.core.tool import ToolRegistry
from athena.research.paper_markdown.interfaces import (
    VisualInterpretation,
    VisualInterpretationRequest,
)
from athena.research.paper_markdown.tool import PaperMarkdownTool
from athena.research.paper_rag.tool import (
    PaperChunkReadTool,
    PaperCitesTool,
    PaperKeywordSearchTool,
    PaperSectionSearchTool,
    PaperSemanticSearchTool,
    PaperVisualOfTool,
)
from athena.research.paper_scout.backends import build_default_backends
from athena.research.paper_source.http import HostRateLimiter, UrllibTransport
from athena.research.paper_source.tool import PaperFetchTool
from athena.storage.artifact_store import LocalArtifactStore

ARTIFACT_ROOT_ENV = "ATHENA_ARTIFACT_ROOT"
RESEARCH_MODEL_ENV = "ATHENA_RESEARCH_MODEL"
TUI_MODEL_ENV = "ATHENA_TUI_MODEL"
EMBEDDING_MODEL_ENV = "ATHENA_EMBEDDING_MODEL"
VISION_MODEL_ENV = "ATHENA_VISION_MODEL"
CONTACT_EMAIL_ENV = "ATHENA_CONTACT_EMAIL"
SEMANTIC_SCHOLAR_KEY_ENV = "SEMANTIC_SCHOLAR_API_KEY"
OPENALEX_KEY_ENV = "OPENALEX_API_KEY"
BASE_URL_ENV = "OPENAI_BASE_URL"
API_KEY_ENV = "OPENAI_API_KEY"

DEFAULT_ARTIFACT_ROOT = Path.home() / ".athena" / "artifacts"
GHOSTSCRIPT_ENV = "ATHENA_GHOSTSCRIPT"
GHOSTSCRIPT_NAMES = ("gs", "gswin64c", "gswin32c", "mgs", "rungs")
DEFAULT_EMBED_BATCH = 16
DEFAULT_EMBED_CONCURRENCY = 4
EMBED_MAX_RETRIES = 5
EMBED_BACKOFF_SECONDS = 2.0
DEFAULT_VISION_TIMEOUT = 180.0
SEMANTIC_SCHOLAR_INTERVAL = 1.1
JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)

VISUAL_PROMPT = """You are reading one visual element from a scientific paper. \
Explain it faithfully for a researcher who cannot see it.

Element kind: {kind}
Caption and surrounding discussion:
{context}
{structured}
Return one JSON object and nothing else, with exactly these keys:
- "summary": a faithful explanation of what the element shows.
- "searchable_text": self-contained text for retrieval. Name the quantities, \
methods, datasets and directions of change explicitly; do not write phrases like \
"the figure shows" or refer to the element by number.
- "structured_data": an object with machine-readable facts such as axes, units, \
compared methods, or table fields. Use an empty object when nothing applies.

Report only what the element supports. Never invent numbers you cannot read."""


def resolve_model(explicit: str = "") -> str:
    """研究链路的文本模型：显式参数 > ``ATHENA_RESEARCH_MODEL`` > ``ATHENA_TUI_MODEL``。

    留两级环境变量是因为策略/打分模型和 TUI 对话模型的取舍不同（前者要便宜稳定、
    后者要好用），但大多数人只想配一个。
    """
    return (
        explicit
        or os.environ.get(RESEARCH_MODEL_ENV, "")
        or os.environ.get(TUI_MODEL_ENV, "")
    )


def build_artifact_store(root: str | Path = "") -> LocalArtifactStore:
    """按 ``ATHENA_ARTIFACT_ROOT`` 建内容寻址存储，默认落在 ``~/.athena/artifacts``。

    默认放家目录而不是仓库内：取源阶段会下载 TeX 包与 PDF，体积增长快且与具体
    checkout 无关，混进工作区只会污染 ``git status``。
    """
    resolved = root or os.environ.get(ARTIFACT_ROOT_ENV, "") or DEFAULT_ARTIFACT_ROOT
    return LocalArtifactStore(resolved)


def find_ghostscript() -> str:
    """找出可用的 Ghostscript 可执行文件；找不到返回空串。

    EPS/PS 是 ``paper_markdown`` 唯一无法用 Python 生态渲染的格式（PostScript 是图灵
    完备语言），而 arXiv 上 2015 年前的论文几乎全用 EPS 插图。实测一批 44 篇里有 59
    张图卡在这里。

    显式的 ``ATHENA_GHOSTSCRIPT`` 优先；否则按 PATH 依次找常见命名，其中 ``mgs`` 与
    ``rungs`` 是 MiKTeX 自带的那份——装了 TeX 发行版的机器通常已经有了，不必另装。
    """
    explicit = os.environ.get(GHOSTSCRIPT_ENV, "").strip()
    if explicit:
        return explicit if Path(explicit).exists() else (shutil.which(explicit) or "")
    for name in GHOSTSCRIPT_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return ""


def build_client(client: AsyncOpenAI | None = None) -> AsyncOpenAI:
    """构造 OpenAI 兼容客户端；``OPENAI_BASE_URL`` 为空串时才回落官方地址。"""
    if client is not None:
        return client
    return AsyncOpenAI(
        api_key=os.environ.get(API_KEY_ENV),
        base_url=os.environ.get(BASE_URL_ENV) or None,
    )


class OpenAIEmbedder:
    """OpenAI 兼容 ``/embeddings`` 端点的 ``TextEmbedder`` 实现。

    响应按 ``index`` 重排后再返回：批量编码的响应顺序由服务端决定，而
    ``build_corpus_index`` 依赖向量与句子严格一一对应，顺序错位不会报错，
    只会让之后每一次语义检索都返回错的句子。

    并发有上限、限流会重试。一次 50 篇的调研要编码约 39000 条句子，按 16 条一批就是
    两千多个请求；无上限地 ``gather`` 会把它们同时打出去，真机上直接撞出
    ``insufficient_quota``，而且是在取源与转换都已完成之后——最贵的那部分成本已经付了。
    """

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        *,
        batch_size: int = DEFAULT_EMBED_BATCH,
        concurrency: int = DEFAULT_EMBED_CONCURRENCY,
    ) -> None:
        self.client = client
        self.model = model
        self.batch_size = batch_size
        self.calls = 0
        self.embedded = 0
        self.retries = 0
        self._limit = asyncio.Semaphore(concurrency)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量编码文本，返回与输入等长、顺序一致的向量列表。"""
        if not texts:
            return []
        batches = [
            texts[start : start + self.batch_size]
            for start in range(0, len(texts), self.batch_size)
        ]
        results = await asyncio.gather(*(self._embed_batch(item) for item in batches))
        return [vector for batch in results for vector in batch]

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        async with self._limit:
            return await self._request(batch)

    async def _request(self, batch: list[str]) -> list[list[float]]:
        """发一批编码请求，遇限流按指数退避重试；重试用尽才抛出。"""
        for attempt in range(EMBED_MAX_RETRIES):
            self.calls += 1
            try:
                reply = await self.client.embeddings.create(
                    model=self.model, input=batch
                )
            except RateLimitError:
                # 配额是按时间窗分配的，退避后通常能过；最后一次仍失败才让调用方看见
                if attempt == EMBED_MAX_RETRIES - 1:
                    raise
                self.retries += 1
                await asyncio.sleep(EMBED_BACKOFF_SECONDS * 2**attempt)
                continue
            self.embedded += len(batch)
            ordered = sorted(reply.data, key=lambda item: item.index)
            return [list(item.embedding) for item in ordered]
        raise RuntimeError("unreachable: the retry loop either returns or raises")


class VisionInterpreter:
    """多模态模型驱动的 ``VisualInterpreter`` 实现。

    图像以 data URI 内联，不走外链：``ArtifactStore`` 是内容寻址的本地存储，
    没有可供模型访问的 URL，而把图片临时上传到公网只为了让模型看一眼，既多一
    份凭据又多一处泄漏面。

    ``calls`` / ``failures`` 暴露真实调用数，视觉解读是整条链路里最贵的一步，
    成本必须可计量。
    """

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        artifacts: LocalArtifactStore,
        *,
        timeout: float = DEFAULT_VISION_TIMEOUT,
    ) -> None:
        self.client = client
        self.model = model
        self.artifacts = artifacts
        self.timeout = timeout
        self.calls = 0
        self.failures = 0

    async def interpret(
        self, request: VisualInterpretationRequest
    ) -> VisualInterpretation:
        """解释单个图、表或低文本页面；解析失败时退回原始回复文本。"""
        content = await self._build_content(request)
        self.calls += 1
        try:
            reply = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": content}],
                temperature=0,
                timeout=self.timeout,
            )
        except Exception:
            # 端点报错、超时或不支持图像输入 → 计数后上抛，由 visual_policy 决定
            # 是让整篇失败（required）还是退回仅证据文本（best_effort）
            self.failures += 1
            raise
        return self._parse(reply.choices[0].message.content or "")

    async def _build_content(self, request: VisualInterpretationRequest) -> list[dict]:
        """拼装多模态消息体：提示文本在前，图像作为 data URI 附在后面。"""
        context = await self.artifacts.get_text(request.context_ref)
        structured = ""
        if request.structured_text_ref is not None:
            extracted = await self.artifacts.get_text(request.structured_text_ref)
            structured = f"Extracted text of the element:\n{extracted}\n"
        prompt = VISUAL_PROMPT.format(
            kind=request.kind, context=context, structured=structured
        )
        content: list[dict] = [{"type": "text", "text": prompt}]
        if request.asset_ref is None:
            return content
        data = await self.artifacts.get_bytes(request.asset_ref)
        media_type = request.media_type or "image/png"
        encoded = base64.b64encode(data).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{encoded}"},
            }
        )
        return content

    def _parse(self, text: str) -> VisualInterpretation:
        """把模型回复解析成结构化解释；拿不到 JSON 时把整段回复当作可检索文本。"""
        match = JSON_OBJECT.search(text)
        payload: dict = {}
        if match is not None:
            try:
                loaded = json.loads(match.group(0))
            except json.JSONDecodeError:
                # 模型输出了近似 JSON（尾随逗号、代码围栏内注释）→ 退回纯文本路径
                loaded = None
            if isinstance(loaded, dict):
                payload = loaded
        summary = str(payload.get("summary", "")).strip()
        searchable = str(payload.get("searchable_text", "")).strip()
        structured = payload.get("structured_data")
        fallback = text.strip()
        return VisualInterpretation(
            summary=summary or fallback,
            searchable_text=searchable or summary or fallback,
            structured_data=structured if isinstance(structured, dict) else {},
            model=self.model,
        )


@dataclass(slots=True)
class ResearchStack:
    """一次 research 运行需要的全部依赖。

    ``embedder`` 与 ``visual_interpreter`` 允许为 ``None``：没有编码器时关键词
    检索和整篇读取照常可用，语义检索会明确报错；没有视觉模型时
    ``visual_policy="best_effort"`` 会退回仅证据文本。两者都是可降级的，
    因此不在装配期强制要求。
    """

    artifacts: LocalArtifactStore
    client: AsyncOpenAI
    model: str
    http: HostRateLimiter
    embedder: OpenAIEmbedder | None = None
    visual_interpreter: VisionInterpreter | None = None
    contact_email: str = ""
    semantic_scholar_api_key: str = ""
    openalex_api_key: str = ""
    ghostscript: str = ""

    def build_backends(self) -> tuple[list, object]:
        """构造共享限流器的检索后端与引用后端。"""
        return build_default_backends(
            self.http,
            semantic_scholar_api_key=self.semantic_scholar_api_key or None,
            contact_email=self.contact_email or None,
        )


def build_research_stack(
    *,
    artifact_root: str | Path = "",
    model: str = "",
    client: AsyncOpenAI | None = None,
    enable_embedder: bool = True,
    enable_vision: bool = True,
) -> ResearchStack:
    """按环境变量装配整条 research 链路的依赖。

    编码器与视觉模型各自需要一个模型名；对应环境变量缺失时该能力保持关闭而不是
    报错——链路在降级形态下仍然完整可跑，把它做成硬错误只会让首次接入寸步难行。
    """
    resolved_client = build_client(client)
    artifacts = build_artifact_store(artifact_root)
    contact = os.environ.get(CONTACT_EMAIL_ENV, "")
    http = HostRateLimiter(
        transport=UrllibTransport(),
        bucket_intervals={"api.semanticscholar.org": SEMANTIC_SCHOLAR_INTERVAL},
        contact_email=contact or None,
    )
    embedding_model = os.environ.get(EMBEDDING_MODEL_ENV, "")
    vision_model = os.environ.get(VISION_MODEL_ENV, "")
    embedder = None
    if enable_embedder and embedding_model:
        embedder = OpenAIEmbedder(resolved_client, embedding_model)
    interpreter = None
    if enable_vision and vision_model:
        interpreter = VisionInterpreter(resolved_client, vision_model, artifacts)
    return ResearchStack(
        artifacts=artifacts,
        client=resolved_client,
        model=resolve_model(model),
        http=http,
        embedder=embedder,
        visual_interpreter=interpreter,
        contact_email=contact,
        semantic_scholar_api_key=os.environ.get(SEMANTIC_SCHOLAR_KEY_ENV, ""),
        openalex_api_key=os.environ.get(OPENALEX_KEY_ENV, ""),
        ghostscript=find_ghostscript(),
    )


def build_research_tools(stack: ResearchStack) -> ToolRegistry:
    """注册取源、转换与六个检索算子，返回可直接交给 Agent 的工具表。

    ``paper_semantic_search`` 只在装配了编码器时注册：没有编码器时它会在每次调用
    时抛错，注册一个必然失败的工具只会诱导模型反复重试。
    """
    tools = ToolRegistry()
    tools.register(
        PaperFetchTool(
            stack.artifacts,
            http=stack.http,
            contact_email=stack.contact_email or None,
            openalex_api_key=stack.openalex_api_key or None,
        )
    )
    tools.register(
        PaperMarkdownTool(
            stack.artifacts,
            stack.visual_interpreter,
            ghostscript=stack.ghostscript or None,
        )
    )
    tools.register(PaperKeywordSearchTool(stack.artifacts))
    tools.register(PaperChunkReadTool(stack.artifacts))
    tools.register(PaperVisualOfTool(stack.artifacts))
    tools.register(PaperCitesTool(stack.artifacts))
    tools.register(PaperSectionSearchTool(stack.artifacts))
    if stack.embedder is not None:
        tools.register(PaperSemanticSearchTool(stack.artifacts, stack.embedder))
    return tools
