"""从 paper_markdown 输出构建 A-RAG 分层语料索引。

索引只做两件事：把 ``RetrievalUnit`` 收成 chunk、把 chunk 切成句子并编码。这里不重新
切分 chunk——上游的结构化切分不会从元素中间截断，重切只会丢掉 locator 溯源。
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


def split_sentences(text: str) -> list[tuple[int, int]]:
    """把 chunk 正文切成句子区间 ``[start, end)``。

    先按行切分，让 Markdown 表格行与公式行各自成为独立单元，再在行内按句末标点切
    分。``split_sentences("Hello there. Next one.")`` 返回 ``[(0, 12), (13, 22)]``。
    """
    spans: list[tuple[int, int]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        indent = len(line) - len(line.lstrip())
        for start, end in line_sentence_spans(line.strip()):
            if end - start >= MIN_SENTENCE_CHARS:
                spans.append((offset + indent + start, offset + indent + end))
        offset += len(line)
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
