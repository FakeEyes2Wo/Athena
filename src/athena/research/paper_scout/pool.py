"""累积的 paper pool，以及交给策略模型的双列表 observation。

pool 是 PaperScout 的隐状态：它记住所有被接受的论文、每篇是搜到的还是扩展来的、以及
是否已经被当作扩展种子用过。observation 只暴露其中一小部分——按论文的设定，最多 10 篇
已扩展和 10 篇未扩展——因为策略模型的上下文装不下整个池，而"已扩展/未扩展"正是它决定
下一步该 search 还是 expand 所需要的全部信息。
"""

from athena.research.paper_scout.schemas import (
    MAX_ABSTRACT_WORDS,
    OBSERVATION_EXPANDED,
    OBSERVATION_UNEXPANDED,
    ScoutPaper,
)

POOL_HEADER = (
    "Paper Pool Status:\n"
    "- [EXP]: Paper has been expanded (already used as a seed for more papers).\n"
    "- [NEW]: New paper found via search or expansion, candidate for further "
    "exploration.\n"
    "- Format: [arxiv_id] (score) [STATUS] Title"
)
EMPTY_POOL = "No papers in the pool."


def truncate_abstract(abstract: str, max_words: int = MAX_ABSTRACT_WORDS) -> str:
    """按词数截断摘要；``truncate_abstract("a b c", 2)`` 返回 ``"a b..."``。"""
    words = abstract.split()
    if len(words) <= max_words:
        return abstract
    return " ".join(words[:max_words]) + "..."


def title_key(title: str) -> str:
    """规范化标题为去重用的键；``title_key("Attention Is All You Need!")`` 返回
    ``"attentionisallyouneed"``。"""
    return "".join(character for character in title if character.isalnum()).lower()


def has_retrievable_source(paper: ScoutPaper) -> bool:
    """论文是否存在 ``paper_source`` 拿得到的源：arXiv id，或上游给出的开放获取 PDF。

    取不到源的论文进了交付集合就是一条 ``skipped`` 记录：既没有正文，又占掉一个
    ``max_papers`` 名额，而池里排在它后面、真能下载的论文因此永远轮不上。

    判据不是"有没有 DOI"而是"有没有拿得到的字节"。Semantic Scholar 的
    ``openAccessPdf`` 在付费墙论文上返回空串、在开放获取论文上返回可直接下载的链接，
    这个字段与标题摘要在同一次请求里返回，因此这层区分不花任何额外请求——只按
    arXiv id 判会连同开放获取期刊论文一起误杀，而它们是真能取到的。

    上游的说法未经验证，只作为线索：``paper_source`` 仍然会校验下载到的字节。判错的
    代价是一次失败的下载，不是一篇错误的语料。
    """
    return bool(paper.arxiv_id or paper.open_access_pdf)


class PaperPool:
    """按标识符与规范化标题双重去重的论文池，按相关性分数降序排列。

    仅按 ``paper_key`` 去重不够：同一篇论文在 Semantic Scholar 里可能存在多条记录，各自
    带不同的 ``paperId``，标识符优先级会让它们取到不同的 key 而同时进池。重复条目会直接
    抬高交付集合的分母、压低 precision，因此标题相同即视为同一篇。

    没有维护额外的排序结构：池的规模受检索预算限制在千篇量级，每次取观测时排序一次
    比维护有序容器更简单，也不会成为瓶颈。
    """

    def __init__(self) -> None:
        self._papers: dict[str, ScoutPaper] = {}
        self._titles: set[str] = set()

    def __len__(self) -> int:
        return len(self._papers)

    def has(self, paper_key: str) -> bool:
        """池中是否已有该 key 的论文。"""
        return paper_key in self._papers

    def contains(self, paper: ScoutPaper) -> bool:
        """论文是否已在池中——标识符命中或规范化标题命中都算。"""
        return paper.paper_key in self._papers or title_key(paper.title) in self._titles

    def get(self, paper_key: str) -> ScoutPaper | None:
        """取出论文；不存在时返回 ``None``。"""
        return self._papers.get(paper_key)

    def add(self, paper: ScoutPaper) -> bool:
        """加入一篇新论文；标识符或标题已存在时返回 ``False`` 且不覆盖原记录。"""
        if self.contains(paper):
            return False
        self._papers[paper.paper_key] = paper
        self._titles.add(title_key(paper.title))
        return True

    def mark_expanded(self, paper_key: str) -> bool:
        """把论文标记为已扩展；论文不存在或已标记过时返回 ``False``。"""
        paper = self._papers.get(paper_key)
        if paper is None or paper.expanded:
            return False
        paper.expanded = True
        return True

    def ranked(self) -> list[ScoutPaper]:
        """按相关性降序、同分时按 key 升序返回全部论文。"""
        return sorted(
            self._papers.values(), key=lambda item: (-item.relevance, item.paper_key)
        )

    def retained(
        self,
        threshold: float,
        limit: int = 0,
        *,
        require_retrievable_source: bool = False,
    ) -> list[ScoutPaper]:
        """返回分数不低于阈值的论文；``limit`` 为 0 表示不截断。

        ``require_retrievable_source`` 的过滤发生在截断**之前**：先剔除取不到源的，
        再取前 ``limit`` 篇。顺序反过来的话过滤就只是把交付集合变短，空出来的名额不会
        让位给后面能下载的论文。
        """
        kept = [item for item in self.ranked() if item.relevance >= threshold]
        if require_retrievable_source:
            kept = [item for item in kept if has_retrievable_source(item)]
        return kept[:limit] if limit else kept

    def observation(self) -> str:
        """渲染双列表观测：至多 10 篇 [EXP] 与 10 篇 [NEW]，按分数降序合并。

        论文里的 observation 只保留 pool 的"搜索前沿"。已扩展的论文提供上下文（这些
        方向已经挖过），未扩展的论文是候选种子，两者都截断后再按分数合并，让模型在一
        个列表里同时看到深度和广度。
        """
        if not self._papers:
            return EMPTY_POOL
        ranked = self.ranked()
        expanded = [item for item in ranked if item.expanded][:OBSERVATION_EXPANDED]
        fresh = [item for item in ranked if not item.expanded][:OBSERVATION_UNEXPANDED]
        shown = sorted(
            expanded + fresh, key=lambda item: (-item.relevance, item.paper_key)
        )
        lines = [POOL_HEADER]
        for paper in shown:
            status = "[EXP]" if paper.expanded else "[NEW]"
            locator = paper.arxiv_id or paper.paper_key
            lines.append(
                f"[{locator}] ({paper.relevance:.2f}) {status} {paper.title}\n"
                f"Abstract: {truncate_abstract(paper.abstract)}"
            )
        return "\n\n".join(lines)
