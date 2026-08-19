"""文献链路的可重跑基准。

在此之前，这条链路的每一个质量数字——检索 MRR、缺摘要的篇数、引用边密度——都来自一次性
脚本，而脚本本身不在仓库里。后果是：**任何改动都无法证明变好，任何回归都不会被发现。**
本包把那些测量搬进仓库，让它们成为能 diff 的输入与输出。

三个入口，覆盖三类问题：

- ``run_known_item``：改写过的提问能不能找回那篇论文（检索质量）；
- ``corpus_health``：这份语料的结构够不够用（摘要覆盖、章节覆盖、引用边密度）；
- ``delivery_overlap``：同一查询两次调研交付的论文重合多少（选片可复现性）；
- ``evaluate_recall``：损失拆成"没找到 / 没判对 / 没交付"三段——known-item 只量了
  "进了语料的能不能被找到"，而真正的大头在它上游。

三者都是纯计算，除语义通道要编码查询外不访网络；``corpus_health`` 连模型都不调。
"""

from athena.research.bench.health import corpus_health
from athena.research.bench.known_item import (
    KEYWORD_CHANNEL,
    SEMANTIC_CHANNEL,
    compare,
    run_known_item,
)
from athena.research.bench.query_sets import (
    DEFAULT_QUERY_SET,
    available,
    dump_report,
    load_query_set,
)
from athena.research.bench.recall import (
    RELEVANT_THRESHOLD,
    RecallQuerySet,
    RecallReport,
    evaluate_recall,
)
from athena.research.bench.reproducibility import delivery_overlap
from athena.research.bench.schemas import (
    ChannelScore,
    CorpusHealthReport,
    DeliveryOverlapReport,
    KnownItemQuery,
    PaperHealth,
    QueryOutcome,
    QuerySet,
    RetrievalBenchReport,
)

__all__ = [
    "ChannelScore",
    "CorpusHealthReport",
    "DEFAULT_QUERY_SET",
    "DeliveryOverlapReport",
    "KEYWORD_CHANNEL",
    "KnownItemQuery",
    "PaperHealth",
    "QueryOutcome",
    "QuerySet",
    "RELEVANT_THRESHOLD",
    "RecallQuerySet",
    "RecallReport",
    "RetrievalBenchReport",
    "SEMANTIC_CHANNEL",
    "available",
    "compare",
    "corpus_health",
    "delivery_overlap",
    "dump_report",
    "evaluate_recall",
    "load_query_set",
    "run_known_item",
]
