"""Pure recall and reproducibility metrics."""

from itertools import combinations

from athena.research.literature.bench.models import (
    DeliveryOverlapReport,
    RecallQuerySet,
    RecallReport,
    StageRecall,
)

RELEVANT_THRESHOLD = 0.45


def evaluate_recall(
    query_set: RecallQuerySet,
    pool: dict[str, float],
    delivered: list[str],
    *,
    threshold: float = RELEVANT_THRESHOLD,
) -> RecallReport:
    """Split gold losses across retrieval, judging, and delivery."""
    gold = list(dict.fromkeys(query_set.gold))
    in_pool = [paper for paper in gold if paper in pool]
    relevant = [paper for paper in in_pool if pool[paper] >= threshold]
    delivered_set = set(delivered)
    shipped = [paper for paper in relevant if paper in delivered_set]
    total = len(gold)

    def stage(name: str, found: int, previous: int) -> StageRecall:
        return StageRecall(
            stage=name,
            found=found,
            recall=found / total if total else 0.0,
            lost_here=previous - found,
        )

    return RecallReport(
        query_set=query_set.name,
        topic=query_set.topic,
        gold_source=query_set.gold_source,
        gold_total=total,
        pool_size=len(pool),
        delivered_size=len(delivered),
        threshold=threshold,
        stages=[
            stage("in_pool", len(in_pool), total),
            stage("judged_relevant", len(relevant), len(in_pool)),
            stage("delivered", len(shipped), len(relevant)),
        ],
        missing_from_pool=[paper for paper in gold if paper not in pool],
        scored_below_threshold=[paper for paper in in_pool if paper not in relevant],
        dropped_at_delivery=[paper for paper in relevant if paper not in shipped],
    )


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def delivery_overlap(
    runs: list[list[str]], *, query: str = "", label: str = ""
) -> DeliveryOverlapReport:
    """Measure mean pairwise Jaccard across repeated delivery sets."""
    sets = [{paper for paper in run if paper} for run in runs]
    if len(sets) < 2:
        raise ValueError("delivery_overlap needs at least two runs to compare")
    scores = [_jaccard(left, right) for left, right in combinations(sets, 2)]
    return DeliveryOverlapReport(
        query=query,
        label=label,
        runs=len(sets),
        delivered=[len(run) for run in sets],
        mean_jaccard=sum(scores) / len(scores),
        min_jaccard=min(scores),
        stable_core=sorted(set.intersection(*sets)),
        union_size=len(set.union(*sets)),
    )
