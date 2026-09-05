"""Academic Survey 的组合根：把 artifact 存储、模型接口与论文工具装配起来。

四个 paper 包（``paper_scout`` / ``paper_source`` / ``paper_markdown`` /
``paper_rag``）都按依赖注入写成：模块不在导入时创建客户端、不读环境变量。代价是
必须有一处把它们拼起来，本模块就是那一处，也是全仓唯一读 survey 相关环境变量的
地方。``SurveyStack`` 之下的类只接受注入，因此单元测试可以完全绕开环境。

LLM 凭据不自己读：客户端一律由 ``athena.core.agent.settings`` 提供，与
supervisor / TUI 走同一份 ``.env`` 契约。本模块只额外读 survey 独有的几项——
artifact 根目录、打分/编码/视觉模型名，以及两个检索后端的礼貌池凭据。

``providers.py`` 中的 ``VisionInterpreter`` 与 ``OpenAIEmbedder`` 是
``VisualInterpreter`` 和 ``TextEmbedder`` 两个协议的生产实现；本模块只负责把它们装配进
``SurveyStack``。
"""

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from openai import AsyncOpenAI

from athena.core.agent import settings
from athena.core.artifact_store import LocalArtifactStore
from athena.core.tool import StackTool, ToolRegistry
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec
from athena.research.literature.paper_markdown.processor import PaperProcessor
from athena.research.literature.paper_markdown.tool import PaperMarkdownTool
from athena.research.literature.paper_rag.search import CorpusCache, RetrievalSession
from athena.research.literature.paper_rag.tool import (
    PaperChunkReadTool,
    PaperCitesTool,
    PaperCorpusOverviewTool,
    PaperKeywordSearchTool,
    PaperRagRuntime,
    PaperSearchTool,
    PaperSectionSearchTool,
    PaperSemanticSearchTool,
    PaperVisualOfTool,
)
from athena.research.literature.paper_scout.backends import (
    SEMANTIC_SCHOLAR_INTERVAL,
    build_default_backends,
)
from athena.research.literature.paper_scout.scorer import DashScopeReranker
from athena.research.literature.paper_source.fetcher import (
    LocatorCache,
    PaperFetchTool,
    PaperSourceFetcher,
    PaperSourceRuntime,
)
from athena.research.literature.paper_source.http import (
    HostRateLimiter,
    UrllibTransport,
)
from athena.research.literature.survey.library import LibraryVectorCache, PaperLibrary
from athena.research.literature.survey.pipeline import SurveyRequest, run_survey
from athena.research.literature.survey.providers import (
    EMBED_MAX_RETRIES,  # noqa: F401 - established wiring import surface
    OpenAIEmbedder,
    VisionInterpreter,
)

ARTIFACT_ROOT_ENV = "ATHENA_ARTIFACT_ROOT"
SURVEY_MODEL_ENV = "ATHENA_SURVEY_MODEL"
SCORER_MODEL_ENV = "ATHENA_SCORER_MODEL"
RERANK_MODEL_ENV = "ATHENA_RERANK_MODEL"
EMBEDDING_MODEL_ENV = "ATHENA_EMBEDDING_MODEL"
VISION_MODEL_ENV = "ATHENA_VISION_MODEL"
CONTACT_EMAIL_ENV = "ATHENA_CONTACT_EMAIL"
SEMANTIC_SCHOLAR_KEY_ENV = "SEMANTIC_SCHOLAR_API_KEY"
OPENALEX_KEY_ENV = "OPENALEX_API_KEY"

DEFAULT_ARTIFACT_ROOT = Path.home() / ".athena" / "artifacts"
GHOSTSCRIPT_ENV = "ATHENA_GHOSTSCRIPT"
GHOSTSCRIPT_NAMES = ("gs", "gswin64c", "gswin32c", "mgs", "rungs")
SURVEY_TOOL_NAME = "paper_survey"


class PaperSurveyTool(StackTool):
    """Build one searchable paper corpus from a natural-language topic."""

    spec = ToolSpec(
        name=SURVEY_TOOL_NAME,
        description=(
            "Build a searchable corpus of academic papers on a topic. Retrieves "
            "candidate papers, fetches their TeX or PDF source, converts them to "
            "Markdown with figures and tables interpreted, and indexes the result. "
            "Returns corpus_ref plus per-paper outcomes. Pass corpus_ref to "
            "paper_keyword_search, paper_semantic_search and paper_chunk_read to "
            "read the corpus. This runs for several minutes and downloads tens of "
            "megabytes, so call it once per topic and reuse the corpus_ref."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Natural language survey topic.",
                },
                "max_papers": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 10,
                    "description": (
                        "Papers carried through to the corpus. Cost grows roughly "
                        "linearly downstream of retrieval."
                    ),
                },
                "published_to": {
                    "type": "string",
                    "description": (
                        "Inclusive ISO date upper bound, for reproducing a survey as "
                        "of a past date. Empty means no bound."
                    ),
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        concurrency_safe=False,
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        query = input.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string.")
        report = await run_survey(
            self.stack,
            SurveyRequest(
                query=query,
                max_papers=_max_papers(input),
                published_to=str(input.get("published_to") or ""),
            ),
        )
        report_ref = await self.stack.artifacts.put_text(report.model_dump_json())
        data = {
            "corpus_ref": report.corpus_ref,
            "report_ref": report_ref,
            "status": report.status,
            "papers_indexed": sum(1 for item in report.papers if item.indexed),
            "papers_converted": report.converted(),
            "papers_fetched": report.fetched,
            "pool_size": report.scout.pool_size,
            "warnings": report.warnings,
        }
        if report.corpus_ref is None:
            return ToolResult(
                data=data,
                success=False,
                error=(
                    f"No corpus was built (status={report.status}); "
                    f"see report_ref {report_ref} for per-paper reasons."
                ),
            )
        return ToolResult(data=data)


def _max_papers(input: dict) -> int:
    """Clamp the unvalidated model input to the tool's documented range."""
    value = input.get("max_papers")
    if not isinstance(value, int) or value < 1:
        return SurveyRequest.model_fields["max_papers"].default
    return min(value, 50)


def resolve_model(explicit: str = "") -> str:
    """检索策略的文本模型：显式参数 > ``ATHENA_SURVEY_MODEL`` > 仓库默认模型。

    单独留一个环境变量是因为策略模型的取舍与 supervisor 不同——它每步要在 20 篇
    观测里决定下一步搜什么、从哪篇扩展，宁可换更强的那档；不设时沿用
    ``settings.model_name()``，与全仓其余部分保持一致。
    """
    return explicit or os.environ.get(SURVEY_MODEL_ENV, "") or settings.model_name()


def resolve_scorer_model(explicit: str = "") -> str:
    """相关性打分的模型；未设 ``ATHENA_SCORER_MODEL`` 时回落到策略模型。

    分开是因为两者的形状完全不同。策略每步一次，要在 20 篇观测里决定下一步搜什么、
    从哪篇扩展，值得用强模型；打分是**四档分类**——读标题加摘要判 0/1/2/3——但它是调用
    次数最多的一环：真机一轮 4 步就发了 38 次，而策略只有 4 次。

    延迟由输出 token 决定。实测策略调用一次吐 2210 个 token、38.9 秒，打分一次吐 857 个、
    15.8 秒。整条链路里 scout 占 69% 的墙钟，其中最大的一块就是这几十次打分，所以把它换成
    不做长推理的轻量模型，是压缩总时长性价比最高的一处。

    回落而不是报错：不设这个变量时行为与此前完全一致。
    """
    return explicit or os.environ.get(SCORER_MODEL_ENV, "")


def build_reranker(model: str = "") -> DashScopeReranker | None:
    """按 ``ATHENA_RERANK_MODEL`` 建同分次序的交叉编码器；未配置时返回 ``None``。

    与编码器、视觉模型同样是可降级的能力：没有它排序退回 ``pool.tie_break`` 的散列，
    行为与接入之前完全一致，因此不在装配期强制要求。

    凭据复用 ``settings`` 那份 —— rerank 与打分、编码走同一个百炼账号，只是端点不同
    （它不是 OpenAI 兼容接口，见 ``DashScopeReranker``）。多解析一份 key 就多一处能
    不一致的地方。

    拿不到 key 时返回 ``None`` 而不是抛错：``.env`` 只配了 ``ATHENA_RERANK_MODEL``
    却没有凭据的情况下，让整条链路跑不起来比降级更糟。
    """
    resolved = model or os.environ.get(RERANK_MODEL_ENV, "")
    if not resolved:
        return None
    api_key = getattr(settings.get_client(), "api_key", "") or ""
    if not api_key:
        return None
    return DashScopeReranker(api_key, resolved)


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
    """取 LLM 客户端：注入优先，否则由 ``settings`` 按 ``.env`` 构造。

    不自己读 key/base_url——survey 与 supervisor、TUI 共用同一份凭据契约，
    多一处解析就多一处能不一致的地方。
    """
    return client if client is not None else settings.get_client()


@dataclass(slots=True)
class SurveyStack:
    """一次 Academic Survey 运行需要的全部依赖。

    ``embedder`` 与 ``visual_interpreter`` 允许为 ``None``：没有编码器时关键词
    检索和整篇读取照常可用，语义检索会明确报错；没有视觉模型时
    ``visual_policy="best_effort"`` 会退回仅证据文本。两者都是可降级的，
    因此不在装配期强制要求。

    ``scorer_model`` 为空串表示打分沿用 ``model``，用 ``effective_scorer_model()`` 取值。

    ``corpus_cache`` 挂在 stack 上而不是每套工具各建一个：一次运行里可能有多个 Agent
    读同一份语料（三条 Ideator lane 各拿一套工具），解码后的语料是只读的，共享一份
    才不会把内存乘以并发度。
    """

    artifacts: LocalArtifactStore
    client: AsyncOpenAI
    model: str
    http: HostRateLimiter
    scorer_model: str = ""
    embedder: OpenAIEmbedder | None = None
    visual_interpreter: VisionInterpreter | None = None
    contact_email: str = ""
    semantic_scholar_api_key: str = ""
    openalex_api_key: str = ""
    ghostscript: str = ""
    corpus_cache: CorpusCache = field(default_factory=CorpusCache)
    library: PaperLibrary | None = None
    reranker: DashScopeReranker | None = None

    def locator_cache(self) -> LocatorCache:
        """落盘的取源定位符缓存，放在论文库根下。

        没有库时退回进程内缓存（行为与此前一致）。缓存的价值全在跨进程：arXiv 每 3 秒
        只允许一次请求，而一次 10 篇的调研要发几十次。
        """
        if self.library is None:
            return LocatorCache()
        return LocatorCache(self.library.root / "locators.json")

    def source_fetcher(self) -> PaperSourceFetcher:
        """Build the source service from this stack's shared transport and cache."""
        return PaperSourceFetcher(
            PaperSourceRuntime(
                self.artifacts,
                http=self.http,
                cache=self.locator_cache(),
                contact_email=self.contact_email or None,
                openalex_api_key=self.openalex_api_key or None,
            )
        )

    def vector_cache(self) -> LibraryVectorCache | None:
        """按篇复用句向量的缓存；没有库或没有编码器时为 ``None``。

        编码器标识进键：不同模型的向量不在同一个空间里，混用会让检索给出看上去正常、
        实际毫无意义的分数。
        """
        if self.library is None or self.embedder is None:
            return None
        return LibraryVectorCache(self.library, self.artifacts, self.embedder.model)

    def effective_scorer_model(self) -> str:
        """实际用于打分的模型名；未单独配置时就是策略模型。"""
        return self.scorer_model or self.model

    def rerank_model(self) -> str:
        """同分次序用的交叉编码器名；没配时为空串。

        进检索缓存键（见 ``pipeline._scorer_fingerprint``）：换了它，同分论文的次序就变了，
        缓存里那份结果不再代表当前配置。"""
        return self.reranker.model if self.reranker is not None else ""

    def build_backends(self) -> tuple[list, object]:
        """构造共享限流器的检索后端与引用后端。"""
        return build_default_backends(
            self.http,
            semantic_scholar_api_key=self.semantic_scholar_api_key or None,
            contact_email=self.contact_email or None,
        )


def build_survey_stack(
    *,
    artifact_root: str | Path = "",
    model: str = "",
    scorer_model: str = "",
    client: AsyncOpenAI | None = None,
    artifacts: LocalArtifactStore | None = None,
    enable_embedder: bool = True,
    enable_vision: bool = True,
    enable_library: bool = True,
    library_root_path: str | Path = "",
) -> SurveyStack:
    """按环境变量装配整条 Academic Survey 链路的依赖。

    编码器与视觉模型各自需要一个模型名；对应环境变量缺失时该能力保持关闭而不是
    报错——链路在降级形态下仍然完整可跑，把它做成硬错误只会让首次接入寸步难行。

    ``artifacts`` 给了就用调用方那份存储，``artifact_root`` 随之失效。宿主（例如
    ``ResearchRuntime``）已经有自己的内容寻址存储时必须走这条路：``corpus_ref`` 要
    和宿主记在同一份状态里的其他引用一起被解析，落在两个 store 里就会出现"状态里
    记着、宿主取不到"。

    缺 LLM 凭据时由 ``settings.get_client()`` 抛错，不在这里重复判断。
    """
    resolved_client = build_client(client)
    store = artifacts if artifacts is not None else build_artifact_store(artifact_root)
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
        interpreter = VisionInterpreter(resolved_client, vision_model, store)
    return SurveyStack(
        artifacts=store,
        client=resolved_client,
        model=resolve_model(model),
        http=http,
        scorer_model=resolve_scorer_model(scorer_model),
        embedder=embedder,
        visual_interpreter=interpreter,
        contact_email=contact,
        semantic_scholar_api_key=os.environ.get(SEMANTIC_SCHOLAR_KEY_ENV, ""),
        openalex_api_key=os.environ.get(OPENALEX_KEY_ENV, ""),
        ghostscript=find_ghostscript(),
        library=PaperLibrary(library_root_path) if enable_library else None,
        reranker=build_reranker(),
    )


def build_survey_tools(
    stack: SurveyStack,
    *,
    include_survey: bool = True,
    include_producers: bool = True,
    session: RetrievalSession | None = None,
) -> ToolRegistry:
    """注册全链路、取源、转换与七个检索算子，返回可直接交给 Agent 的工具表。

    七个检索算子共用**一个** ``RetrievalSession``：会话既是语料缓存的入口，也是"本
    会话已读过哪些 chunk"的账本。每个工具各建一个会话时，两件事都会坏——同一份语料
    被解码七次，而 ``paper_chunk_read`` 的去重也只对它自己成立。

    ``paper_semantic_search`` 只在装配了编码器时注册：没有编码器时它会在每次调用
    时抛错，注册一个必然失败的工具只会诱导模型反复重试。

    ``include_survey=False`` 去掉 ``paper_survey``，``include_producers=False`` 再去掉
    取源与转换，留给已经拿到 ``corpus_ref``、只需要读语料的 Agent——把一个几分钟起步
    的工具摆在那里，模型迟早会去按它。

    ``session`` 可由调用方注入：会话记着"这个 Agent 真正打开过哪些论文"，而引用核验
    需要那份账本（见 ``RetrievalSession.read_papers``）。不注入时自建一个，行为不变。
    """
    tools = ToolRegistry()
    session = session if session is not None else RetrievalSession(stack.corpus_cache)
    rag = PaperRagRuntime(stack.artifacts, session, stack.embedder)
    if include_survey:
        tools.register(PaperSurveyTool(stack))
    if include_producers:
        tools.register(PaperFetchTool(stack.source_fetcher()))
        tools.register(
            PaperMarkdownTool(
                PaperProcessor(
                    stack.artifacts,
                    stack.visual_interpreter,
                    ghostscript=stack.ghostscript or None,
                )
            )
        )
    tools.register(PaperCorpusOverviewTool(rag))
    tools.register(PaperKeywordSearchTool(rag))
    tools.register(PaperChunkReadTool(rag))
    tools.register(PaperVisualOfTool(rag))
    tools.register(PaperCitesTool(rag))
    tools.register(PaperSectionSearchTool(rag))
    if stack.embedder is not None:
        tools.register(PaperSemanticSearchTool(rag))
        # 融合入口只在有编码器时注册：没有向量它会退化成纯词面，与
        # paper_keyword_search 完全重复，多摆一个只会让选择变难。
        tools.register(PaperSearchTool(rag))
    return tools
