"""召回基准：把损失拆成"没找到 / 没判对 / 没交付"三段。

现有的 known-item 基准量的是**进了语料的能不能被找到**。而真正的大头在它上游：
SPARBench 上 Recall 0.100 对 R@all 0.111，转化率 90%——在池子里的东西九成都交付了，
瓶颈是**进不进得了池子**。那一段此前没有任何常设测量。

三段分开报，因为修法完全不同：

===============  ==========================  ==============================
段               丢在这里意味着              该动的地方
===============  ==========================  ==============================
进池             检索没找到                  查询构造、``search_top_k``、后端
判为相关         找到了但打分没给够          打分提示、打分模型
进交付           判对了但被名额挤掉          ``max_papers``、选片、取源成功率
===============  ==========================  ==============================

合成一个 Recall 会把三种完全不同的病混成一个数字——真机上正是这样：一次 F1 0.079 的
运行里，检索丢掉 56/72，而证据门几乎无损（16 篇里保住 15 篇）。只看总分会去优化打分。

## 金标必须多遍取均值，否则"判为相关"那一段量的是回归均值

这条是被自己坑出来的。第一版金标取"某一次运行单遍打分 ≥0.45 的那些"，量出来 deep 那轮
在打分段丢掉 15/29 篇，看上去像是打分器不稳。逐条查下来是**选择效应**：

============================  =====  ==========  ====================
按什么筛                      n      档位一致率  均分变化
============================  =====  ==========  ====================
不按分数筛                    109    0.771       0.297 → 0.301 (+0.004)
按金标 ≥0.45 筛（即金标集）   29     0.414       0.564 → 0.453 (-0.110)
按对照运行 ≥0.45 筛（反向）   22     0.545       0.484 → 0.700 (+0.216)
按金标 <0.45 筛               80     0.900       0.200 → 0.246 (+0.046)
============================  =====  ==========  ====================

打分器**没有系统性偏移**（不筛时 +0.004）。但按"单遍分数高"筛出来的那批，天然包含了抽到
好签的那些，再打一遍必然回落——两个方向都对称地出现。于是金标本身制造了一段并不存在的
损失。

修法是把金标建在**多遍均值**上（见随包 ``imbalance_auc_recall`` 的 ``gold_source``）。
一般规则：**金标与被评运行共用同一个打分器时，"判为相关"这一段必然被回归均值污染，除非
金标那一侧做了降方差。**
"""

from typing import Literal

from pydantic import BaseModel, Field

from athena.research.bench.schemas import BENCH_SCHEMA_VERSION

RELEVANT_THRESHOLD = 0.45
"""判为"相关"的分数线，取 ``GRADE_SCORES`` 里 2 分档的取值。

不取交付门槛 0.5：那条线是"只有 3 分才算"，而这一段要区分的是"打分器认不认为它有关"，
2 分（切题但不满足全部条件）应当算认。
"""


class RecallQuerySet(BaseModel):
    """一个课题的召回金标。

    ``gold_source`` 是必填的散文说明，因为这类金标几乎不可能是人工标注的绝对真值——
    它通常来自"一次更贵的运行判定为相关的那些论文"。把来源写死在数据里，读数的人才不会
    把代理指标当成 ground truth，也才知道换了来源之后前后两次不可比。
    """

    name: str = Field(description="Query set identity.")
    topic: str = Field(min_length=1, description="The survey topic being evaluated.")
    gold: list[str] = Field(min_length=1, description="Paper ids that should be found.")
    gold_source: str = Field(
        min_length=1,
        description=(
            "Where these golds came from, in prose. Required: a proxy gold read as "
            "ground truth is worse than no gold at all."
        ),
    )


class StageRecall(BaseModel):
    """一段的召回与它丢掉的那些。"""

    stage: Literal["in_pool", "judged_relevant", "delivered"]
    found: int = Field(ge=0)
    recall: float = Field(ge=0.0, le=1.0)
    lost_here: int = Field(
        ge=0, description="Golds that survived the previous stage but not this one."
    )


class RecallReport(BaseModel):
    """一次召回评测的完整结果。"""

    schema_version: Literal["1.0"] = BENCH_SCHEMA_VERSION
    query_set: str
    topic: str
    gold_source: str
    gold_total: int = Field(ge=0)
    pool_size: int = Field(ge=0)
    delivered_size: int = Field(ge=0)
    threshold: float = Field(ge=0.0, le=1.0)
    stages: list[StageRecall] = Field(default_factory=list)
    missing_from_pool: list[str] = Field(
        default_factory=list, description="Golds retrieval never surfaced."
    )
    scored_below_threshold: list[str] = Field(
        default_factory=list, description="Golds found but not judged relevant."
    )
    dropped_at_delivery: list[str] = Field(
        default_factory=list, description="Golds judged relevant but not delivered."
    )


def evaluate_recall(
    query_set: RecallQuerySet,
    pool: dict[str, float],
    delivered: list[str],
    *,
    threshold: float = RELEVANT_THRESHOLD,
) -> RecallReport:
    """按三段拆解一次运行相对金标的召回。

    ``pool`` 是 ``paper_key -> relevance``，``delivered`` 是交付集合的 key 列表。两者都
    直接来自 ``ScoutCorpus``，所以这个函数不碰网络也不调模型——它只是把已经落盘的事实
    重新组织成"损失发生在哪一段"。

    每一段的分母都是**金标总数**而不是上一段的存量：读数的人要的是"最终拿到多少"，
    而 ``lost_here`` 单独给出各段自己丢了多少，两个问题分开答。
    """
    gold = list(dict.fromkeys(query_set.gold))
    total = len(gold)
    in_pool = [item for item in gold if item in pool]
    relevant = [item for item in in_pool if pool[item] >= threshold]
    shipped = [item for item in relevant if item in set(delivered)]

    def ratio(found: int) -> float:
        return found / total if total else 0.0

    stages = [
        StageRecall(
            stage="in_pool",
            found=len(in_pool),
            recall=ratio(len(in_pool)),
            lost_here=total - len(in_pool),
        ),
        StageRecall(
            stage="judged_relevant",
            found=len(relevant),
            recall=ratio(len(relevant)),
            lost_here=len(in_pool) - len(relevant),
        ),
        StageRecall(
            stage="delivered",
            found=len(shipped),
            recall=ratio(len(shipped)),
            lost_here=len(relevant) - len(shipped),
        ),
    ]
    return RecallReport(
        query_set=query_set.name,
        topic=query_set.topic,
        gold_source=query_set.gold_source,
        gold_total=total,
        pool_size=len(pool),
        delivered_size=len(delivered),
        threshold=threshold,
        stages=stages,
        missing_from_pool=[item for item in gold if item not in pool],
        scored_below_threshold=[item for item in in_pool if item not in relevant],
        dropped_at_delivery=[item for item in relevant if item not in shipped],
    )
