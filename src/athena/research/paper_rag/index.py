"""从 paper_markdown 输出构建 A-RAG 分层语料索引。

索引只做两件事：把 ``RetrievalUnit`` 收成 chunk、把 chunk 切成句子并编码。这里不重新
切分 chunk——上游的结构化切分不会从元素中间截断，重切只会丢掉 locator 溯源。

切句是结构感知的：标题行、``> Section:`` 前缀、表格分隔行这类纯结构片段不进入句子索
引（见 ``is_indexable``），跨行的展示公式整块作为一个单元。语料是论文全文而非维基段
落，Markdown 结构行占了相当比例，放任它们与正文句平权参与检索会系统性地劣化排序。
"""

import json
import math
import re

from athena.core.schemas import ArtifactRef
from athena.research.paper_markdown.schemas import PaperContent
from athena.research.paper_rag.interfaces import TextEmbedder
from athena.research.paper_rag.schemas import (
    CorpusEntry,
    CorpusSentence,
    PaperCorpusIndex,
)
from athena.storage.artifact_store import ArtifactStore

SENTENCE_END = re.compile(r"[.!?](?=\s)")
WORD_BOUNDARY = re.compile(r"[\s(\[]")
HEADING_PATH_PREFIX = "> Section:"
TABLE_DELIMITER = re.compile(r"^\|[\s\-:|]+\|?$")
DISPLAY_MATH_FENCES = {"$$": "$$", "\\[": "\\]"}
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


def display_math_end(lines: list[str], start: int) -> int:
    """返回展示公式块最后一行的下标；不是公式起始行或块未闭合时返回 ``start``。

    未闭合的块退化成普通行处理，避免一个漏写的定界符把后面整篇正文吞进同一个单元。
    """
    closing = DISPLAY_MATH_FENCES.get(lines[start].strip())
    if closing is None:
        return start
    for index in range(start + 1, len(lines)):
        if lines[index].strip() == closing:
            return index
    return start


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
    return spans


async def embed_sentences(
    store: ArtifactStore, index: PaperCorpusIndex, embedder: TextEmbedder
) -> ArtifactRef:
    """分批编码全部句子并归一化后落盘，向量顺序与 ``index.sentences`` 一一对应。"""
    texts = [index.sentence_text(position) for position in range(len(index.sentences))]
    vectors: list[list[float]] = []
    for start in range(0, len(texts), EMBED_BATCH):
        batch = await embedder.embed(texts[start : start + EMBED_BATCH])
        vectors.extend(normalize(vector) for vector in batch)
    return await store.put_text(json.dumps(vectors))


async def build_corpus_index(
    store: ArtifactStore,
    papers: list[PaperContent],
    embedder: TextEmbedder | None = None,
) -> ArtifactRef:
    """把若干篇论文构建成可检索语料，返回三个检索工具接受的 ``corpus_ref``。

    没有 ``embedder`` 时仍产出完整索引，只是不生成句向量：关键词检索与整篇读取照常可
    用，语义检索会明确报错而不是静默返回空结果。
    """
    entries: list[CorpusEntry] = []
    sentences: list[CorpusSentence] = []
    for paper in papers:
        for unit in await paper.load_retrieval_units(store):
            spans = split_sentences(unit.text)
            entries.append(
                CorpusEntry(
                    chunk_id=unit.unit_id,
                    paper_id=unit.metadata.get("paper_id", ""),
                    title=unit.metadata.get("title", ""),
                    kind=unit.kind,
                    heading_path=unit.heading_path,
                    text=unit.text,
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

    index = PaperCorpusIndex(entries=entries, sentences=sentences)
    if embedder is not None:
        index.embedding_ref = await embed_sentences(store, index, embedder)
        index.embedding_model = embedder.model
    return await store.put_text(index.model_dump_json())
