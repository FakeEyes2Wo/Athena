"""语料的结构体检：量"这份语料够不够用"，而不是"链路有没有崩"。

``SurveyReport.final_status`` 只回答后者。前者需要单独的判据，因为链路的失败在这里是
安静的：转换报 100% 成功，而一半论文没有摘要 chunk、论文之间只有个位数引用边——两个
图遍历算子于是形同虚设，``paper_corpus_overview`` 交给 Ideator 的"语料里有什么"有一半
是作者列表和 LaTeX 导言区。

本模块只读索引：不调模型、不访网络、不碰句向量。
"""

from athena.research.paper_rag.index import (
    MIN_ANCHOR_PROSE_CHARS,
    anchor_index,
    novel_prose_chars,
)
from athena.research.paper_rag.search import (
    LoadedCorpus,
    heading_variants,
    paper_namespace,
)
from athena.research.bench.schemas import CorpusHealthReport, PaperHealth

ABSTRACT_KIND = "abstract"

PROBED_SECTIONS = (
    "abstract",
    "introduction",
    "related work",
    "method",
    "dataset",
    "experiment",
    "ablation",
    "limitation",
    "conclusion",
)
"""体检要探的章节名，经 ``heading_variants`` 展开同义写法后统计。

``ablation`` 与 ``limitation`` 单独列在这里不是凑数：工具描述把它们说成获取反面证据
的路径，而真机上生产尺寸语料的 Ablation 覆盖是 0/8。一条被反复推荐、实际命不中的
路径，比没有这条路径更糟。
"""


def anchor_position(corpus: LoadedCorpus, positions: list[int]) -> int:
    """一篇论文被引用与被总览时的落点。

    直接走 ``index.anchor_index``，不另写一套——体检要量的是**生产路径实际选中的那个
    chunk**。规则在两处各写一遍，体检就会在规则改动后继续报旧行为，而那种"尺子和被测
    对象各说各话"是最难察觉的一类错误。
    """
    entries = corpus.index.entries
    return positions[
        anchor_index(
            [entries[position].kind for position in positions],
            [entries[position].text for position in positions],
            [entries[position].title for position in positions],
        )
    ]


def paper_sections(corpus: LoadedCorpus, positions: list[int]) -> list[str]:
    """一篇论文出现过的顶层章节名，按文档顺序去重。

    只取 ``path[0]``，与 ``PaperSummary.sections`` 同一口径：那是 Ideator 在总览里看到
    的东西。章节覆盖率用的是另一套（``all_headings``），因为它要回答的是另一个问题。
    """
    entries = corpus.index.entries
    sections: list[str] = []
    for position in positions:
        path = entries[position].heading_path
        if path and path[0] not in sections:
            sections.append(path[0])
    return sections


def all_headings(corpus: LoadedCorpus, positions: list[int]) -> list[str]:
    """一篇论文各层级出现过的全部章节名。

    章节覆盖率必须按**全部层级**统计，因为 ``section_search`` 就是这么匹配的（它遍历
    ``enumerate(entry.heading_path)`` 并按深度给分）。只看顶层会把 Ablation 报成 0——
    它几乎总是 "Experiments / Ablation Study" 这样的子节，于是这个数字回答的就不再是
    "该算子能不能找到反面证据"，而变成了"论文有没有把消融放在一级标题"。
    """
    entries = corpus.index.entries
    names: list[str] = []
    for position in positions:
        for name in entries[position].heading_path:
            if name not in names:
                names.append(name)
    return names


def matches_section(headings: list[str], probe: str) -> bool:
    """该论文是否有一个章节名落在 ``probe`` 的同义写法里。

    走 ``heading_variants`` 而不是字面比较：论文之间对同一部分的叫法不统一，按字面统计
    会把"这些论文不讨论局限"和"它们把局限写在别的标题下"混成同一个数字。
    """
    variants = heading_variants(probe)
    return any(variant in name.lower() for name in headings for variant in variants)


def corpus_health(corpus: LoadedCorpus, corpus_ref: str = "") -> CorpusHealthReport:
    """给一份已装载的语料做结构体检。

    ``citation_edges`` 数的是去重后的"论文 → 论文"边，不是 chunk 级的边计数：后者会被
    一篇论文在多个 chunk 里引同一篇论文放大，让密度看上去比实际好。
    """
    index = corpus.index
    grouped: dict[str, list[int]] = {}
    for position, entry in enumerate(index.entries):
        grouped.setdefault(entry.paper_id, []).append(position)

    details: list[PaperHealth] = []
    paper_edges: set[tuple[str, str]] = set()
    visual_links = 0
    section_coverage = dict.fromkeys(PROBED_SECTIONS, 0)

    for paper_id, positions in grouped.items():
        entries = [index.entries[position] for position in positions]
        anchor = index.entries[anchor_position(corpus, positions)]
        sections = paper_sections(corpus, positions)
        headings = all_headings(corpus, positions)
        outbound = {
            paper_namespace(target)
            for entry in entries
            for target in entry.cited_ids
            if paper_namespace(target) != paper_id
        }
        paper_edges |= {(paper_id, target) for target in outbound}
        links = sum(len(entry.visual_ids) for entry in entries)
        visual_links += links
        for probe in PROBED_SECTIONS:
            if matches_section(headings, probe):
                section_coverage[probe] += 1
        details.append(
            PaperHealth(
                paper_id=paper_id,
                title=anchor.title,
                chunks=len(positions),
                sentences=sum(
                    entry.sentence_end - entry.sentence_start for entry in entries
                ),
                has_abstract_chunk=any(
                    entry.kind == ABSTRACT_KIND for entry in entries
                ),
                anchor_kind=anchor.kind,
                anchor_prose_chars=novel_prose_chars(anchor.text, anchor.title),
                sections=sections,
                outbound_citations=len(outbound),
                visual_links=links,
            )
        )

    details.sort(key=lambda item: item.paper_id)
    papers = len(details)
    with_abstract = sum(1 for item in details if item.has_abstract_chunk)
    thin = sum(
        1 for item in details if item.anchor_prose_chars < MIN_ANCHOR_PROSE_CHARS
    )
    return CorpusHealthReport(
        corpus_ref=corpus_ref,
        papers=papers,
        chunks=len(index.entries),
        sentences=len(index.sentences),
        semantic_search=index.embedding_ref is not None,
        embedding_model=index.embedding_model,
        abstract_coverage=(with_abstract / papers) if papers else 0.0,
        thin_anchor_papers=thin,
        citation_edges=len(paper_edges),
        citation_density=(len(paper_edges) / papers) if papers else 0.0,
        visual_links=visual_links,
        section_coverage=section_coverage,
        papers_detail=details,
    )
