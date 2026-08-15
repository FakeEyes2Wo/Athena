"""HypoPriList：候选假设的纯算法 Elo 排序（P1+P2 落地）。

只消费已经产生的 pairwise 比较结果（PairwiseComparison），本模块内不调用任何 LLM——真正产出
比较结果的 PairwiseJudge 在 workflows/search/workflow.py 里，以保证这里可以脱离 LLM/provider
单测、稳定运行。设计参考 Co-Scientist（AI co-scientist, arXiv:2502.18864）的锦标赛式在线 Elo
机制；离线 Bradley-Terry+bootstrap CI 的 PortfolioRanker 留给 P3 视需要再启用。
"""

from pydantic import BaseModel, Field, model_validator


# ====== 常量 ======

DEFAULT_ELO_RATING: float = 1200.0
DEFAULT_K_FACTOR: float = 32.0
RANKING_RUBRIC_VERSION: str = "hypo-pri-list/v1"


# ====== 数据模型 ======

class PairwiseComparison(BaseModel):
    """Result of a pairwise comparison; winner_id must be either idea_id_a or idea_id_b.

    Example:
        >>> PairwiseComparison(idea_id_a="idea-1", idea_id_b="idea-2",
        ...                      winner_id="idea-1", rationale="more falsifiable").winner_id
        'idea-1'
    """
    idea_id_a: str = Field(description="First candidate id.")
    idea_id_b: str = Field(description="Second candidate id.")
    winner_id: str = Field(description="Winning candidate id; must equal idea_id_a or idea_id_b.")
    rationale: str = Field(description="Itemized evidence for why winner_id won; never a bare score.")

    @model_validator(mode="after")
    def check_winner_is_a_participant(self) -> "PairwiseComparison":
        # winner_id 必须是参赛双方之一，且双方不能是同一个候选，否则 Elo 更新没有意义
        if self.idea_id_a == self.idea_id_b:
            raise ValueError("idea_id_a and idea_id_b must differ")
        if self.winner_id not in (self.idea_id_a, self.idea_id_b):
            raise ValueError("winner_id must be idea_id_a or idea_id_b")
        return self


class RankedCandidate(BaseModel):
    """A single item in the ranking result; includes rubric version and itemized evidence, never a bare score.

    Example:
        >>> RankedCandidate(idea_id="idea-1", rating=1200.0, comparisons=0,
        ...                   rubric_version="hypo-pri-list/v1", evidence=[]).rating
        1200.0
    """
    idea_id: str = Field(description="Candidate id.")
    rating: float = Field(description="Current Elo rating.")
    comparisons: int = Field(ge=0, description="Number of pairwise comparisons this candidate took part in.")
    rubric_version: str = Field(description="Versioned ranking rubric.")
    evidence: list[str] = Field(
        default_factory=list, description="Rationale strings from every comparison involving this candidate."
    )


# ====== 排序队列 ======

class HypoPriList:
    """在线 Elo 优先队列；每条比较立即更新评分，不做离线批量重算。"""

    def __init__(self, *, initial_rating: float = DEFAULT_ELO_RATING, k_factor: float = DEFAULT_K_FACTOR) -> None:
        # 初始评分/K 因子可配置，默认值（1200/32）适用于大多数场景，无需调参即可使用
        self._initial_rating = initial_rating
        self._k_factor = k_factor
        self._ratings: dict[str, float] = {}
        self._comparisons: dict[str, int] = {}
        self._evidence: dict[str, list[str]] = {}

    def _rating_of(self, idea_id: str) -> float:
        # 首次出现的候选按 initial_rating 起步；已存在的候选直接返回当前评分
        return self._ratings.setdefault(idea_id, self._initial_rating)

    def ensure_registered(self, idea_id: str) -> None:
        """确保候选出现在排名里，即便它还没有参与任何比较（例如唯一存活候选）。

        Example:
            >>> book = HypoPriList()
            >>> book.ensure_registered("solo")
            >>> book.rank()[0].comparisons
            0
        """
        self._rating_of(idea_id)
        self._comparisons.setdefault(idea_id, 0)
        self._evidence.setdefault(idea_id, [])

    def record_comparison(self, comparison: PairwiseComparison) -> None:
        """按标准 Elo 公式用一条比较结果更新两个候选的评分。

        Example:
            >>> book = HypoPriList()
            >>> book.record_comparison(PairwiseComparison(idea_id_a="a", idea_id_b="b",
            ...                                              winner_id="a", rationale="better"))
            >>> book.rank()[0].idea_id
            'a'
        """
        rating_a = self._rating_of(comparison.idea_id_a)
        rating_b = self._rating_of(comparison.idea_id_b)
        expected_a = 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))
        score_a = 1.0 if comparison.winner_id == comparison.idea_id_a else 0.0

        self._ratings[comparison.idea_id_a] = rating_a + self._k_factor * (score_a - expected_a)
        self._ratings[comparison.idea_id_b] = rating_b + self._k_factor * ((1.0 - score_a) - (1.0 - expected_a))

        for idea_id in (comparison.idea_id_a, comparison.idea_id_b):
            self._comparisons[idea_id] = self._comparisons.get(idea_id, 0) + 1
            self._evidence.setdefault(idea_id, []).append(comparison.rationale)

    def rank(self) -> list[RankedCandidate]:
        """按当前评分从高到低返回排序列表；评分相同按 idea_id 排序，保证结果确定。

        Example:
            >>> HypoPriList().rank()
            []
        """
        return [
            RankedCandidate(
                idea_id=idea_id,
                rating=self._ratings[idea_id],
                comparisons=self._comparisons.get(idea_id, 0),
                rubric_version=RANKING_RUBRIC_VERSION,
                evidence=self._evidence.get(idea_id, []),
            )
            for idea_id in sorted(self._ratings, key=lambda i: (-self._ratings[i], i))
        ]
