"""AcademicSurvey 的去重、证据门控与确定性排序。"""

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from athena.research.academic_survey.schemas import (
    CandidateObservation,
    CriterionJudgment,
    JudgmentDraft,
    QueryPlan,
    RankBreakdown,
    RankingIntent,
    RelevanceJudgment,
    SearchQuery,
    SurveyCandidate,
    SurveyConstraints,
    SurveyRequest,
)
from athena.research.paper_source.schemas import PaperIdentity

RRF_K = 60
RUBRIC_VERSION = "academic-survey-rubric-v1"
_TOKEN = re.compile(r"[\w]+", re.UNICODE)
_BIOMEDICAL_TERMS = {
    "biological",
    "biology",
    "bioinformatics",
    "biomedical",
    "clinical",
    "disease",
    "drug",
    "genomics",
    "genomic",
    "health",
    "medical",
    "medicine",
    "neuroscience",
    "patient",
    "protein",
}


def _identity_tokens(identity: PaperIdentity) -> set[str]:
    """把一个身份展开为只在同命名空间比较的 token。"""
    return {
        f"{field}:{value}"
        for field in (
            "arxiv_id",
            "doi",
            "s2_paper_id",
            "openalex_id",
            "pmid",
            "pmc_id",
        )
        if (value := getattr(identity, field))
    }


def merge_observations(
    observations: Iterable[CandidateObservation],
) -> tuple[list[SurveyCandidate], list[CandidateObservation]]:
    """仅按真实 ID 做传递合并；标题-only 结果进入 quarantine。"""
    groups: list[tuple[set[str], list[CandidateObservation]]] = []
    quarantined: list[CandidateObservation] = []
    seen_observations: set[str] = set()

    for observation in observations:
        fingerprint = observation.model_dump_json()
        if fingerprint in seen_observations:
            continue
        seen_observations.add(fingerprint)
        identity = observation.identity.to_paper_identity()
        if identity is None:
            quarantined.append(observation)
            continue
        tokens = _identity_tokens(identity)
        matching = [index for index, (known, _) in enumerate(groups) if known & tokens]
        if not matching:
            groups.append((tokens, [observation]))
            continue

        first = matching[0]
        groups[first][0].update(tokens)
        groups[first][1].append(observation)
        for index in reversed(matching[1:]):
            other_tokens, other_observations = groups.pop(index)
            groups[first][0].update(other_tokens)
            groups[first][1].extend(other_observations)

    candidates = [_candidate_from_group(group) for _, group in groups]
    return candidates, quarantined


def _candidate_from_group(group: list[CandidateObservation]) -> SurveyCandidate:
    identities = [
        identity
        for item in group
        if (identity := item.identity.to_paper_identity()) is not None
    ]
    identity_data: dict[str, str] = {}
    for field in (
        "arxiv_id",
        "doi",
        "s2_paper_id",
        "openalex_id",
        "pmid",
        "pmc_id",
    ):
        if value := next(
            (getattr(item, field) for item in identities if getattr(item, field)), None
        ):
            identity_data[field] = value

    richest = max(group, key=lambda item: (len(item.abstract), len(item.title)))
    identity = PaperIdentity(**identity_data, title=richest.title)
    authors = next((item.authors for item in group if item.authors), [])
    year = next((item.year for item in group if item.year is not None), None)
    venue = next((item.venue for item in group if item.venue), "")
    language = next((item.language for item in group if item.language), "")
    citations = [
        item.citation_count for item in group if item.citation_count is not None
    ]
    return SurveyCandidate(
        candidate_id=identities[0].paper_key(),
        identity=identity,
        title=richest.title,
        abstract=richest.abstract,
        authors=authors,
        year=year,
        venue=venue,
        language=language,
        citation_count=max(citations) if citations else None,
        observations=group,
    )


def constraint_status(
    candidate: SurveyCandidate, constraints: SurveyConstraints
) -> Literal["eligible", "rejected", "unknown"]:
    """区分已知违反约束与缺少显式约束所需元数据。"""
    if constraints.year_from is not None:
        if candidate.year is None:
            return "unknown"
        if candidate.year < constraints.year_from:
            return "rejected"
    if constraints.year_to is not None:
        if candidate.year is None:
            return "unknown"
        if candidate.year > constraints.year_to:
            return "rejected"
    if constraints.venues:
        allowed = {_normal_text(value) for value in constraints.venues}
        if not candidate.venue:
            return "unknown"
        if _normal_text(candidate.venue) not in allowed:
            return "rejected"
    if constraints.languages:
        allowed = {_normal_language(value) for value in constraints.languages}
        if not candidate.language:
            return "unknown"
        if _normal_language(candidate.language) not in allowed:
            return "rejected"
    return "eligible"


def satisfies_constraints(
    candidate: SurveyCandidate, constraints: SurveyConstraints
) -> bool:
    """仅允许元数据完整且没有违反显式硬约束的候选。"""
    return constraint_status(candidate, constraints) == "eligible"


def enforce_plan_policy(plan: QueryPlan, request: SurveyRequest) -> QueryPlan:
    """确定性收紧 PubMed 使用范围并应用 intent 排序权重。"""
    context = " ".join(
        [request.topic, plan.domain, *request.constraints.domains]
    ).casefold()
    allow_pubmed = "pubmed" in request.topic.casefold() or bool(
        set(_TOKEN.findall(context)) & _BIOMEDICAL_TERMS
    )
    queries = plan.queries
    if not allow_pubmed:
        queries = []
        for query in plan.queries:
            channels = [channel for channel in query.channels if channel != "pubmed"]
            if channels:
                queries.append(query.model_copy(update={"channels": channels}))
    weights = plan.ranking_intent.model_dump()
    if plan.intent == "recent_advances":
        weights["recency_weight"] *= 1.5
    elif plan.intent in {"influential_works", "early_works"}:
        weights["authority_weight"] *= 1.5
    return plan.model_copy(
        update={"queries": queries, "ranking_intent": RankingIntent(**weights)}
    )


def possible_duplicate_pairs(
    candidates: Iterable[SurveyCandidate], limit: int = 100
) -> list[tuple[str, str]]:
    """记录规范化标题相同但真实 ID 不同的候选，不自动合并。"""
    buckets: dict[str, list[str]] = {}
    for candidate in candidates:
        buckets.setdefault(_normal_text(candidate.title), []).append(
            candidate.candidate_id
        )
    pairs = []
    for candidate_ids in buckets.values():
        for index, left in enumerate(candidate_ids):
            for right in candidate_ids[index + 1 :]:
                pairs.append((left, right))
                if len(pairs) == limit:
                    return pairs
    return pairs


def validate_judgment(
    plan: QueryPlan,
    candidate: SurveyCandidate,
    draft: JudgmentDraft,
    prompt_bundle_version: str,
) -> RelevanceJudgment:
    """校验证据原文，并用 required criteria 聚合最终 verdict。"""
    drafts = {item.criterion_id: item for item in draft.criteria}
    criteria: list[CriterionJudgment] = []
    for criterion in plan.criteria:
        item = drafts.get(criterion.criterion_id)
        if item is None:
            criteria.append(
                CriterionJudgment(
                    criterion_id=criterion.criterion_id, verdict="unknown"
                )
            )
            continue
        evidence = [
            quote
            for quote in item.evidence
            if _quote_is_present(candidate, quote.field, quote.quote)
        ]
        invalid_evidence = len(evidence) != len(item.evidence)
        verdict = item.verdict
        if invalid_evidence or (verdict in {"met", "unmet"} and not evidence):
            verdict = "unknown"
        criteria.append(
            CriterionJudgment(
                criterion_id=criterion.criterion_id,
                verdict=verdict,
                evidence=evidence,
            )
        )

    required = {
        criterion.criterion_id for criterion in plan.criteria if criterion.required
    }
    required_verdicts = [
        item.verdict for item in criteria if item.criterion_id in required
    ]
    if "unmet" in required_verdicts:
        verdict = "irrelevant"
    elif "unknown" in required_verdicts:
        verdict = "uncertain"
    else:
        verdict = "relevant"
    return RelevanceJudgment(
        rubric_version=RUBRIC_VERSION,
        prompt_bundle_version=prompt_bundle_version,
        criteria=criteria,
        verdict=verdict,
    )


def queries_overlap(left: str, right: str, threshold: float = 0.8) -> bool:
    """用规范化 token Jaccard 判断两个查询是否重复。"""
    left_tokens = set(_TOKEN.findall(left.casefold()))
    right_tokens = set(_TOKEN.findall(right.casefold()))
    if not left_tokens or not right_tokens:
        return not left_tokens and not right_tokens
    return (
        len(left_tokens & right_tokens) / len(left_tokens | right_tokens) >= threshold
    )


def unique_queries(
    queries: Iterable[SearchQuery],
    history: Iterable[str],
    limit: int,
    *,
    threshold: float = 0.8,
    diagnostics: list[str] | None = None,
) -> list[SearchQuery]:
    """按原顺序去掉与历史或本批高度重合的查询。"""
    known = list(history)
    result: list[SearchQuery] = []
    for query in queries:
        if any(queries_overlap(query.text, previous, threshold) for previous in known):
            if diagnostics is not None:
                diagnostics.append(f"{query.query_id}: duplicate query text")
            continue
        result.append(query)
        known.append(query.text)
        if len(result) == limit:
            break
    return result


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    candidate: SurveyCandidate
    breakdown: RankBreakdown
    independent_lists: int


def rank_candidates(
    candidates: list[SurveyCandidate],
    judgments: dict[str, RelevanceJudgment],
    plan: QueryPlan,
    *,
    current_year: int | None = None,
) -> list[RankedCandidate]:
    """计算 RRF、criterion coverage、authority 与 recency 后稳定排序。"""
    if not candidates:
        return []
    year = current_year or datetime.now(UTC).year
    raw_rrf = {item.candidate_id: _rrf(item) for item in candidates}
    max_rrf = max(raw_rrf.values(), default=1.0) or 1.0
    ranked: list[RankedCandidate] = []
    for candidate in candidates:
        judgment = judgments[candidate.candidate_id]
        rrf = raw_rrf[candidate.candidate_id] / max_rrf
        relevance = _criterion_coverage(plan, judgment)
        authority = _authority(candidate, candidates)
        recency = _recency(candidate.year, plan.intent, year)
        weights = plan.ranking_intent
        final = (
            weights.rrf_weight * rrf
            + weights.relevance_weight * relevance
            + weights.authority_weight * authority
            + weights.recency_weight * recency
        )
        lists = len({(item.channel, item.query_id) for item in candidate.observations})
        ranked.append(
            RankedCandidate(
                candidate,
                RankBreakdown(
                    raw_rrf=raw_rrf[candidate.candidate_id],
                    citation_count=candidate.citation_count,
                    publication_year=candidate.year,
                    rrf=rrf,
                    relevance=relevance,
                    authority=authority,
                    recency=recency,
                    final_score=final,
                ),
                lists,
            )
        )
    return sorted(
        ranked,
        key=lambda item: (
            -item.breakdown.final_score,
            -item.independent_lists,
            item.candidate.candidate_id,
        ),
    )


def _rrf(candidate: SurveyCandidate) -> float:
    ranks: dict[tuple[str, str], int] = {}
    for item in candidate.observations:
        key = (item.channel, item.query_id)
        ranks[key] = min(ranks.get(key, item.raw_rank), item.raw_rank)
    return sum(1 / (RRF_K + rank) for rank in ranks.values())


def _criterion_coverage(plan: QueryPlan, judgment: RelevanceJudgment) -> float:
    optional = {item.criterion_id for item in plan.criteria if not item.required}
    if not optional:
        return 1.0
    met = sum(
        item.criterion_id in optional and item.verdict == "met"
        for item in judgment.criteria
    )
    return met / len(optional)


def _authority(candidate: SurveyCandidate, candidates: list[SurveyCandidate]) -> float:
    if candidate.citation_count is None:
        return 0.5
    peers = [
        math.log1p(item.citation_count)
        for item in candidates
        if item.year == candidate.year and item.citation_count is not None
    ]
    value = math.log1p(candidate.citation_count)
    return sum(peer <= value for peer in peers) / len(peers)


def _recency(year: int | None, intent: str, current_year: int) -> float:
    if year is None:
        return 0.5
    age = max(0, current_year - year)
    if intent == "early_works":
        return 1 - math.pow(2, -age / 10)
    half_life = 5 if intent == "recent_advances" else 10
    return math.pow(2, -age / half_life)


def _quote_is_present(candidate: SurveyCandidate, field: str, quote: str) -> bool:
    source = candidate.title if field == "title" else candidate.abstract
    return " ".join(quote.split()) in " ".join(source.split())


def _normal_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _normal_language(value: str) -> str:
    normalized = _normal_text(value)
    return {"eng": "en", "english": "en", "zho": "zh", "chi": "zh"}.get(
        normalized, normalized
    )
