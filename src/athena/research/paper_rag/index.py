"""从 paper_markdown 输出构建 A-RAG 分层语料索引。

索引只做两件事：把 ``RetrievalUnit`` 收成 chunk、把 chunk 切成句子并编码。这里不重新
切分 chunk——上游的结构化切分不会从元素中间截断，重切只会丢掉 locator 溯源。

切句是结构感知的：标题行、``> Section:`` 前缀、表格分隔行这类纯结构片段不进入句子索
引（见 ``is_indexable``），跨行的展示公式整块作为一个单元。语料是论文全文而非维基段
落，Markdown 结构行占了相当比例，放任它们与正文句平权参与检索会系统性地劣化排序。
"""

import io
import json
import math
import re

import numpy

from athena.core.contracts import ArtifactRef
from athena.research.paper_markdown.schemas import PaperContent, RetrievalUnit
from athena.research.paper_rag.interfaces import TextEmbedder
from athena.research.paper_rag.schemas import (
    CorpusEntry,
    CorpusSentence,
    PaperCorpusIndex,
)
from athena.research.paper_scout.pool import title_key
from athena.core.contracts import ArtifactStore

SENTENCE_END = re.compile(r"[.!?](?=\s)")
WORD_BOUNDARY = re.compile(r"[\s(\[]")
SEMANTIC_MARGIN_THRESHOLD = 0.25
# 每组是（锚句，改写句，无关句）。锚句与改写句刻意不共享实词，词法编码器因此无法把改写
# 句排在无关句前面；三组覆盖不同措辞，避免个别词偶然重合让探针失效。
SEMANTIC_PROBES = (
    (
        "Retrieval augmentation reduces hallucination in question answering.",
        "Grounding a model in fetched documents makes its answers more factual.",
        "The cat slept on the windowsill all afternoon without moving.",
    ),
    (
        "The optimizer converged faster with a smaller learning rate.",
        "Training reached its plateau sooner once step sizes were reduced.",
        "She bought three loaves of bread and a jar of honey.",
    ),
    (
        "Ablation shows the reranking stage contributes most of the gain.",
        "Removing the second-pass scoring component costs nearly all improvement.",
        "Heavy rain delayed the ferry departure until the following morning.",
    ),
)
BIBLIOGRAPHY_KIND = "bibliography"
ABSTRACT_KIND = "abstract"
CITATION_KEY = re.compile(r"\[@([^\]\s]+)\]")
MIN_TITLE_MATCH_CHARS = 20
MIN_TITLE_RUN_CHARS = 40
HEADING_PATH_PREFIX = "> Section:"
TABLE_DELIMITER = re.compile(r"^\|[\s\-:|]+\|?$")
DISPLAY_MATH_FENCES = {"$$": "$$", "\\[": "\\]"}
DISPLAY_MATH_ENVIRONMENTS = frozenset(
    {
        "align",
        "alignat",
        "displaymath",
        "eqnarray",
        "equation",
        "flalign",
        "gather",
        "multline",
    }
)
MATH_ENVIRONMENT_BEGIN = re.compile(r"\\begin\{([a-zA-Z]+\*?)\}")
# [^\W\d_] 是"任意语言的字母"，因此 Gómez 这类作者名不会被当成无内容片段丢掉
LETTER_RUN = re.compile(r"[^\W\d_]{2,}")
ABBREVIATIONS = frozenset(
    {
        "al",
        "approx",
        "cf",
        "dr",
        "e.g",
        "eq",
        "etc",
        "fig",
        "i.e",
        "mr",
        "ms",
        "no",
        "prof",
        "ref",
        "resp",
        "sec",
        "tab",
        "vs",
    }
)
MIN_SENTENCE_CHARS = 2
EMBED_BATCH = 128
SENTENCE_TERMINATORS = (".", "!", "?")
DISPLAY_MATH_CLOSERS = ("$$", "\\]")


def normalize(vector: list[float]) -> list[float]:
    """缩放为单位长度，使余弦相似度退化为点积；零向量原样返回。"""
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return list(vector)
    return [value / norm for value in vector]


def is_abbreviation(fragment: str) -> bool:
    """判断片段末尾的句点属于缩写或姓名首字母；``"see Fig."`` 返回 True。"""
    if not fragment.endswith("."):
        return False
    word = WORD_BOUNDARY.split(fragment[:-1])[-1].lower()
    return len(word) <= 1 or word in ABBREVIATIONS


def is_indexable(span: str) -> bool:
    """判断一个片段是否值得作为独立检索单元；``"## Abstract"`` 返回 False。

    标题行与 ``> Section:`` 前缀承载的信息已经结构化在 ``SearchHit.heading_path`` 里，
    表格分隔行与孤立的公式定界符则不含任何可检索内容。它们一旦成为独立句子，就会以
    "向量方向纯粹"的优势在余弦检索里挤掉真正命中查询的正文句——短片段只要命中查询里
    的任意一个词就能拿到接近上界的相似度，却不必覆盖查询的其余部分。

    只按结构判定，不按长度判定：表格数据行常常只有数字与方法名，却正是最该被检索到的
    内容，用长度阈值会把它们一并误删。
    """
    stripped = span.strip()
    if stripped.startswith(HEADING_PATH_PREFIX) or stripped.startswith("#"):
        return False
    if stripped.startswith("|"):
        return TABLE_DELIMITER.match(stripped) is None
    return LETTER_RUN.search(stripped) is not None


def line_sentence_spans(line: str) -> list[tuple[int, int]]:
    """在单行内按句末标点切分，返回行内字符区间，跳过缩写处的句点。"""
    spans: list[tuple[int, int]] = []
    start = 0
    for match in SENTENCE_END.finditer(line):
        if is_abbreviation(line[start : match.end()]):
            continue
        spans.append((start, match.end()))
        start = match.end()
        while start < len(line) and line[start].isspace():
            start += 1
    if start < len(line):
        spans.append((start, len(line)))
    return spans


def display_math_close(line: str) -> str | None:
    """返回该行开启的展示公式块所对应的结束定界符；不是起始行时返回 ``None``。

    除 ``$$`` 与 ``\\[`` 外还认顶层数学环境：换一个上游转换器就可能直接产出
    ``\\begin{equation}``。``aligned``、``cases`` 这类只能嵌套在数学模式内部的环境不在
    此列，它们总是被外层定界符一并吃掉。
    """
    stripped = line.strip()
    fence = DISPLAY_MATH_FENCES.get(stripped)
    if fence is not None:
        return fence
    match = MATH_ENVIRONMENT_BEGIN.fullmatch(stripped)
    if match is None or match.group(1).rstrip("*") not in DISPLAY_MATH_ENVIRONMENTS:
        return None
    return f"\\end{{{match.group(1)}}}"


def display_math_end(lines: list[str], start: int) -> int:
    """返回展示公式块最后一行的下标；不是公式起始行或块未闭合时返回 ``start``。

    未闭合的块退化成普通行处理，避免一个漏写的定界符把后面整篇正文吞进同一个单元。
    """
    closing = display_math_close(lines[start])
    if closing is None:
        return start
    for index in range(start + 1, len(lines)):
        if lines[index].strip() == closing:
            return index
    return start


def closes_display_math(fragment: str) -> bool:
    """判断片段是否以展示公式的结束定界符收尾。"""
    return fragment.endswith(DISPLAY_MATH_CLOSERS) or bool(
        re.search(r"\\end\{[a-zA-Z]+\*?\}$", fragment)
    )


def continues_across_math(
    text: str, previous: tuple[int, int], span: tuple[int, int]
) -> bool:
    """判断两个相邻片段是否属于同一个被展示公式夹断的句子。

    数学写作里"我们得到 [公式] 而 [公式]"是一句话，按行切之后会留下 ``Therefore``、
    ``and`` 这类没有独立意义的残片。只在与展示公式相邻时接合：表格行同样没有句末标点，
    但它们本来就该各自独立成检索单元。
    """
    left = text[previous[0] : previous[1]].rstrip()
    if left.endswith(SENTENCE_TERMINATORS):
        return False
    right = text[span[0] : span[1]].lstrip()
    if display_math_close(right.splitlines()[0]) is not None:
        return True
    # 公式之后另起大写字母多半是新句子，只接合 and / where 这类明显的续写
    return closes_display_math(left) and right[:1].islower()


def split_sentences(text: str) -> list[tuple[int, int]]:
    """把 chunk 正文切成可检索的句子区间 ``[start, end)``。

    先按行切分，让 Markdown 表格行各自成为独立单元，再在行内按句末标点切分，最后按
    ``is_indexable`` 剔除纯结构片段。跨行的展示公式整块作为一个单元：按行切会把一条
    公式拆成 ``$$``、``\\begin{aligned}``、``&=`` 等十几个碎片，既让整条公式不再可检索，
    又要为每个碎片各付一次编码。区间指向的仍是未经改动的 chunk 正文，因此
    ``paper_chunk_read`` 返回的全文不受影响。
    ``split_sentences("Hello there. Next one.")`` 返回 ``[(0, 12), (13, 22)]``。
    """
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)

    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(lines):
        last = display_math_end(lines, index)
        if last > index:
            indent = len(lines[index]) - len(lines[index].lstrip())
            spans.append(
                (offsets[index] + indent, offsets[last] + len(lines[last].rstrip()))
            )
            index = last + 1
            continue
        stripped = lines[index].strip()
        indent = len(lines[index]) - len(lines[index].lstrip())
        for start, end in line_sentence_spans(stripped):
            if end - start >= MIN_SENTENCE_CHARS and is_indexable(stripped[start:end]):
                position = offsets[index] + indent
                spans.append((position + start, position + end))
        index += 1

    joined: list[tuple[int, int]] = []
    for span in spans:
        if joined and continues_across_math(text, joined[-1], span):
            joined[-1] = (joined[-1][0], span[1])
            continue
        joined.append(span)
    return joined


def visual_link_ids(unit: RetrievalUnit) -> list[str]:
    """取出该单元的图文互链 id，并补上论文命名空间。

    正文 chunk 的 ``visual_ids`` 指向它讨论的图表，视觉单元的 ``chunk_ids`` 指向讨论它
    的正文；上游两侧都只存裸 id，加上命名空间后才能直接交给 ``paper_chunk_read``。

    这条边与引用边分开存放：两者的语义完全不同（一个留在篇内，一个跨到另一篇论文），
    合并成一个无类型列表会让 Agent 只能盲跟。
    """
    namespace = unit.metadata.get("retrieval_namespace", "")
    linked = unit.metadata.get("visual_ids") or unit.metadata.get("chunk_ids") or ""
    return [f"{namespace}:{item}" for item in linked.split(",") if item]


def pack_vectors(vectors: list[list[float]]) -> bytes:
    """把归一化后的向量打包成 float32 numpy 缓冲。

    1.0 的 JSON 文本编码在真实语料上不可用：44 篇论文的 36920 条句向量落盘 800 MB，
    ``json.loads`` 要 10 秒，解码成 ``list[list[float]]`` 常驻 1.15 GB（每个 Python
    float 24 字节）。同一批向量按 float32 打包是 144 MB、装载 0.02 秒、常驻 144 MB，
    而 float32 与 float64 的差异在 1e-7 量级，对 top-k 排序没有影响。
    """
    buffer = io.BytesIO()
    numpy.save(buffer, numpy.asarray(vectors, dtype=numpy.float32), allow_pickle=False)
    return buffer.getvalue()


def unpack_vectors(data: bytes) -> numpy.ndarray:
    """读回 ``pack_vectors`` 写下的缓冲，得到 ``(句数, 维度)`` 的 float32 矩阵。"""
    return numpy.load(io.BytesIO(data), allow_pickle=False)


def decode_json_vectors(text: str) -> numpy.ndarray:
    """读回 1.0 语料的 JSON 向量，仍归一成同一种内存表示。

    磁盘格式有两种，内存表示只有一种：旧语料只是多付一次解析，检索路径无需分支。
    """
    return numpy.asarray(json.loads(text), dtype=numpy.float32)


async def embed_texts(
    store: ArtifactStore, texts: list[str], embedder: TextEmbedder
) -> ArtifactRef:
    """分批编码并归一化后落盘，向量顺序与输入一一对应。"""
    vectors: list[list[float]] = []
    for start in range(0, len(texts), EMBED_BATCH):
        batch = await embedder.embed(texts[start : start + EMBED_BATCH])
        vectors.extend(normalize(vector) for vector in batch)
    return await store.put_bytes(pack_vectors(vectors))


async def embed_sentences(
    store: ArtifactStore, index: PaperCorpusIndex, embedder: TextEmbedder
) -> ArtifactRef:
    """编码全部句子，向量顺序与 ``index.sentences`` 一一对应。"""
    return await embed_texts(
        store,
        [index.sentence_text(position) for position in range(len(index.sentences))],
        embedder,
    )


class NonSemanticEmbedderError(RuntimeError):
    """注入的编码器无法把改写句与无关句区分开，语义检索会退化成噪声。"""


async def semantic_margin(embedder: TextEmbedder) -> float:
    """探针测量编码器的语义分辨力：改写句与无关句的余弦相似度之差。

    每组探针的锚句与其改写句刻意不共享实词，因此**词法编码器无法把改写句排在无关句
    前面**，而语义编码器可以。实测同一组探针上词法 bag-of-words 得 0.114，
    ``qwen3.7-text-embedding`` 得 0.502，相差 4.4 倍，中间留得下阈值。
    """
    texts = [text for probe in SEMANTIC_PROBES for text in probe]
    vectors = [normalize(vector) for vector in await embedder.embed(texts)]
    margins = []
    for position in range(0, len(vectors), 3):
        anchor, paraphrase, unrelated = vectors[position : position + 3]
        near = sum(left * right for left, right in zip(anchor, paraphrase))
        far = sum(left * right for left, right in zip(anchor, unrelated))
        margins.append(near - far)
    return sum(margins) / len(margins) if margins else 0.0


async def require_semantic_embedder(embedder: TextEmbedder) -> float:
    """在组合根启动时校验编码器确实是语义的；不合格直接拒绝，返回实测 margin。

    这一步刻意不放进 ``build_corpus_index``：它要额外打一次模型，而单元测试用假编码器
    是正当的。它属于装配期检查——一次错误注入会让之后每一次检索都无声地返回噪声，实测
    词法与神经编码的 MRR 相差 8.7 倍、R@1 仅 0.025。
    """
    margin = await semantic_margin(embedder)
    if margin < SEMANTIC_MARGIN_THRESHOLD:
        raise NonSemanticEmbedderError(
            f"Embedder '{getattr(embedder, 'model', '?')}' separates paraphrase from "
            f"unrelated text by only {margin:.3f}; semantic retrieval needs at least "
            f"{SEMANTIC_MARGIN_THRESHOLD}. Inject a neural text embedder."
        )
    return margin


def paper_anchors(units_by_paper: list[list[RetrievalUnit]]) -> dict[str, str]:
    """给每篇论文选一个可被引用指向的落点：优先摘要，否则第一个单元。"""
    anchors: dict[str, str] = {}
    for units in units_by_paper:
        if not units:
            continue
        namespace = units[0].metadata.get("retrieval_namespace", "")
        chosen = next((item for item in units if item.kind == ABSTRACT_KIND), units[0])
        anchors[namespace] = chosen.unit_id
    return anchors


def title_matches(key: str, normalized: str) -> bool:
    """一条参考文献的规范化文本是否指向标题规范化为 ``key`` 的论文。

    先试整题包含，这是干净的情形。真实语料里大量引用过不了这一关，原因不是引错了论文
    而是标题本身有出入：作者拼错自己的题目（``hetergeneous`` vs ``heterogeneous``）、
    引用的是 arXiv 版而语料收的是会议版（多出一个 ``representation``）。整题包含对这
    一个字符的差别是全或无的。

    因此再试一条：标题里存在一段 ``MIN_TITLE_RUN_CHARS`` 长的连续规范化字符出现在参考
    文献里，就算命中。阈值取 40 是有依据的——44 篇真实语料上它恰好补回三条人工核对为
    真的引用（10→11 篇引用方、8→10 篇被引方），且不引入任何误连；同时它天然排除短标题
    （"Enhanced Cost-sensitive Ensemble" 规范化后只有 29 字符，永远够不到 40），而短标题
    正是宽松匹配下误连的唯一来源。
    """
    if len(key) >= MIN_TITLE_MATCH_CHARS and key in normalized:
        return True
    if len(key) < MIN_TITLE_RUN_CHARS:
        return False
    return any(
        key[start : start + MIN_TITLE_RUN_CHARS] in normalized
        for start in range(len(key) - MIN_TITLE_RUN_CHARS + 1)
    )


def citation_edges(
    units_by_paper: list[list[RetrievalUnit]], anchors: dict[str, str]
) -> dict[tuple[str, str], str]:
    """把参考文献条目解析成 ``(命名空间, 引用键) → 被引论文落点`` 的边。

    只认语料内部的引用：一条参考文献的文本若被 ``title_matches`` 判定指向语料中某篇论
    文，就把该条目的引用键连到那篇论文。跨出语料的引用没有落点，留着只会变成
    ``not_found``。标题短于 ``MIN_TITLE_MATCH_CHARS`` 时不参与匹配，避免"RAG"这类短名
    误连。
    """
    catalogue = [
        (title_key(units[0].metadata.get("title", "")), namespace)
        for units, namespace in (
            (items, items[0].metadata.get("retrieval_namespace", ""))
            for items in units_by_paper
            if items
        )
        if len(title_key(units[0].metadata.get("title", ""))) >= MIN_TITLE_MATCH_CHARS
    ]
    edges: dict[tuple[str, str], str] = {}
    for units in units_by_paper:
        for unit in units:
            if unit.kind != BIBLIOGRAPHY_KIND:
                continue
            namespace = unit.metadata.get("retrieval_namespace", "")
            normalized = title_key(unit.text)
            target = next(
                (
                    anchors[cited]
                    for key, cited in catalogue
                    if cited != namespace and title_matches(key, normalized)
                ),
                None,
            )
            if target is None:
                continue
            for citation_key in CITATION_KEY.findall(unit.text):
                edges[(namespace, citation_key)] = target
    return edges


def cited_paper_ids(
    unit: RetrievalUnit, edges: dict[tuple[str, str], str]
) -> list[str]:
    """取出该单元引用到的、语料内部论文的落点 id，保持出现顺序且去重。"""
    namespace = unit.metadata.get("retrieval_namespace", "")
    found: list[str] = []
    for citation_key in unit.metadata.get("citation_keys", "").split(","):
        target = edges.get((namespace, citation_key.strip()))
        if target and target not in found:
            found.append(target)
    return found


async def build_corpus_index(
    store: ArtifactStore,
    papers: list[PaperContent],
    embedder: TextEmbedder | None = None,
    *,
    index_bibliography: bool = False,
) -> ArtifactRef:
    """把若干篇论文构建成可检索语料，返回三个检索工具接受的 ``corpus_ref``。

    没有 ``embedder`` 时仍产出完整索引，只是不生成句向量：关键词检索与整篇读取照常可
    用，语义检索会明确报错而不是静默返回空结果。

    参考文献默认不作为独立检索单元，而是解析成引用边挂到引用它的正文 chunk 上（见
    ``citation_edges``）。实测一篇论文的参考文献能占到语料 45% 的条目，而它们本身是稀
    薄文本；SciRAG（EACL 2026）把这种做法称为对引用关系的"表层利用"——参考文献的价值
    在于它是图的边，不是一段可检索的正文。``index_bibliography=True`` 可恢复旧行为，
    用于对照测量。
    """
    units_by_paper = [await paper.load_retrieval_units(store) for paper in papers]
    anchors = paper_anchors(units_by_paper)
    edges = citation_edges(units_by_paper, anchors)

    entries: list[CorpusEntry] = []
    sentences: list[CorpusSentence] = []
    for units in units_by_paper:
        for unit in units:
            if unit.kind == BIBLIOGRAPHY_KIND and not index_bibliography:
                continue
            spans = split_sentences(unit.text)
            entries.append(
                CorpusEntry(
                    chunk_id=unit.unit_id,
                    paper_id=unit.metadata.get("paper_id", ""),
                    title=unit.metadata.get("title", ""),
                    kind=unit.kind,
                    heading_path=unit.heading_path,
                    text=unit.text,
                    visual_ids=visual_link_ids(unit),
                    cited_ids=cited_paper_ids(unit, edges),
                    sentence_start=len(sentences),
                    sentence_end=len(sentences) + len(spans),
                )
            )
            sentences.extend(
                CorpusSentence(
                    entry_index=len(entries) - 1, char_start=start, char_end=end
                )
                for start, end in spans
            )

    # 未被解释的视觉单元与未索引的参考文献都不在语料里，指向它们的链接必须剔除，
    # 否则 Agent 会读到 not_found
    present = {entry.chunk_id for entry in entries}
    for entry in entries:
        entry.visual_ids = [item for item in entry.visual_ids if item in present]
        entry.cited_ids = [item for item in entry.cited_ids if item in present]

    index = PaperCorpusIndex(entries=entries, sentences=sentences)
    if embedder is not None:
        index.embedding_ref = await embed_sentences(store, index, embedder)
        index.embedding_format = "float32"
        index.embedding_model = embedder.model
    return await store.put_text(index.model_dump_json())
