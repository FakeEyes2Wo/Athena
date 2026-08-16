"""语料的结构体检：量"这份语料够不够用"，而不是"链路有没有崩"。

``SurveyReport.final_status`` 只回答后者。前者需要单独的判据，因为链路的失败在这里是
安静的：转换报 100% 成功，而一半论文没有摘要 chunk、论文之间只有个位数引用边——两个
图遍历算子于是形同虚设，``paper_corpus_overview`` 交给 Ideator 的"语料里有什么"有一半
是作者列表和 LaTeX 导言区。

本模块只读索引：不调模型、不访网络、不碰句向量。
"""

import re

from athena.research.paper_rag.index import HEADING_PATH_PREFIX, LETTER_RUN
from athena.research.paper_rag.search import (
    LoadedCorpus,
    heading_variants,
    paper_namespace,
)
from athena.research.bench.schemas import CorpusHealthReport, PaperHealth

ABSTRACT_KIND = "abstract"
HEADING_PREFIX = HEADING_PATH_PREFIX
LATEX_COMMAND = re.compile(r"\\[a-zA-Z@]+\*?")

OVERVIEW_WINDOW_CHARS = 280
"""``PaperSummary.abstract`` 的额度，与 ``search.OVERVIEW_ABSTRACT_CHARS`` 一致。

体检要量的是"Ideator 实际看到的那 280 个字符里有多少是正文"，所以窗口必须与生产
路径同宽；宽一点窄一点都会把这个数字变成另一件事的度量。
"""

MIN_ANCHOR_PROSE_CHARS = 40
"""低于这个字数就认为门面几乎没有正文散文。

真机（语料 A，44 篇）实测：22 篇的 280 字额度里正文不足 40 字，额度花在标题（``title``
字段本来就有）与作者列表上，有一篇整段是 ``\\definecolor...pdftitle=`` 的导言区。
阈值取得很松，只用来识别"几乎什么都没有"。
"""

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
    """一篇论文被引用与被总览时的落点；优先摘要，否则首个 chunk。

    规则必须与 ``search._paper_summary`` 和 ``index.paper_anchors`` 一致——体检要量的是
    生产路径实际选中的那个 chunk，另立一套规则就量成了别的东西。
    """
    entries = corpus.index.entries
    return next(
        (item for item in positions if entries[item].kind == ABSTRACT_KIND),
        positions[0],
    )


def novel_prose_chars(text: str, title: str) -> int:
    """总览窗口里**新增信息**的字符数：扣掉标题与 LaTeX 命令之后还剩多少实词。

    直接数实词是不够的。作者列表与论文标题都由字母组成，数出来是满的，而 Ideator 从
    这段文字里一个字的新信息都得不到——标题已经在 ``PaperSummary.title`` 字段里另给了
    一份。所以先扣掉三样东西再数：

    - ``> Section: …`` 前缀（``chunking`` 给无标题 chunk 加的结构行）；
    - 标题本身，逐词扣（论文常在正文首行重复一遍自己的题目，大小写与断行都可能不同）；
    - ``\\command`` 形态的 LaTeX 命令（有一篇的锚点整段是 ``\\definecolor…pdftitle=``）。

    剩下的字母串长度就是这 280 字额度真正买到的东西。
    """
    window = text.strip()
    if window.startswith(HEADING_PREFIX):
        window = window.split("\n", 1)[-1].strip()
    window = window[:OVERVIEW_WINDOW_CHARS]
    window = LATEX_COMMAND.sub(" ", window)
    title_words = {word.lower() for word in LETTER_RUN.findall(title) if len(word) > 2}
    return sum(
        len(word)
        for word in LETTER_RUN.findall(window)
        if word.lower() not in title_words
    )


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
    return any(
        variant in name.lower() for name in headings for variant in variants
    )


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
    section_coverage = {probe: 0 for probe in PROBED_SECTIONS}

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
