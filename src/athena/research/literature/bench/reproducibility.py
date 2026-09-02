"""选片可复现性：同一查询跑两次，交付的论文重合多少。

这是整改里最该被盯住的一个数字，因为它量的是链路唯一一处**信号被随机毁掉**的地方。
打分只有四档，交付名额几乎总是在某一档内部被截断——实测一轮里 8 篇满分直接入选，剩下
5 个名额由 35 篇同为 0.45 的论文争夺，胜负由 ``tie_break`` 的散列决定。后果是同一查询
两次跑测的 10 篇里只有 2 篇相同。

下游做得再对也救不回这一步：语义检索 MRR 已经 0.938，找不到的东西不是没检索到，是它
根本没进语料。
"""

from athena.research.literature.bench.schemas import DeliveryOverlapReport


def jaccard(left: set[str], right: set[str]) -> float:
    """两个集合的 Jaccard 相似度；两边都空时定义为 1.0。

    都空表示"两次都没交付任何论文"，那是完全一致的结果——按 0/0 判成 0.0 会把它误报
    成最不可复现的情况。
    """
    union = left | right
    if not union:
        return 1.0
    return len(left & right) / len(union)


def delivery_overlap(
    runs: list[list[str]], *, query: str = "", label: str = ""
) -> DeliveryOverlapReport:
    """比较若干次调研各自交付的论文集合。

    取**两两 Jaccard 的均值**而不是全体交集：三次跑测里如果有两次高度一致、第三次完全
    不同，全体交集会把前两次的一致性也一并抹掉，而均值能把它保留下来。同时报交集与并集
    的大小——"稳定核心有多大"和"候选面有多宽"是两个不同的问题。
    """
    sets = [{item for item in run if item} for run in runs]
    if len(sets) < 2:
        raise ValueError("delivery_overlap needs at least two runs to compare")
    pairs = [
        jaccard(sets[left], sets[right])
        for left in range(len(sets))
        for right in range(left + 1, len(sets))
    ]
    common = set.intersection(*sets)
    union = set.union(*sets)
    return DeliveryOverlapReport(
        query=query,
        label=label,
        runs=len(sets),
        delivered=[len(item) for item in sets],
        mean_jaccard=sum(pairs) / len(pairs),
        min_jaccard=min(pairs),
        stable_core=sorted(common),
        union_size=len(union),
    )
