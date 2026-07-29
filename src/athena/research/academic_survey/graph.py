"""AcademicSurvey 的固定 SPAR LangGraph。"""

import asyncio
import hashlib
import json
import time
from dataclasses import asdict, dataclass
from typing import TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field

from athena.core.schemas import ArtifactRef
from athena.core.tool_types import EmitEvent
from athena.research.academic_survey.budget import allocate_search_indices, budget_for
from athena.research.academic_survey.channels.base import ChannelFailure
from athena.research.academic_survey.interfaces import ChannelAdapter, SurveyChains
from athena.research.academic_survey.logic import (
    constraint_status,
    enforce_plan_policy,
    merge_observations,
    possible_duplicate_pairs,
    rank_candidates,
    unique_queries,
    validate_judgment,
)
from athena.research.academic_survey.schemas import (
    AcademicSurveyResult,
    CandidateObservation,
    ChannelName,
    JudgmentDraft,
    QueryEvolution,
    QueryPlan,
    ReferencePaper,
    RelevanceJudgment,
    RewrittenQuery,
    SearchQuery,
    SurveyCandidate,
    SurveyCorpus,
    SurveyRequest,
    SurveyStats,
)
from athena.research.paper_source.schemas import (
    PaperRef,
    PaperSourceRequest,
    SourceHint,
)
from athena.storage.artifact_store import ArtifactStore


class CandidateLedger(BaseModel):
    """完整候选账本；大字段始终保存在 artifact 中。"""

    candidates: list[SurveyCandidate] = Field(default_factory=list)
    quarantined: list[CandidateObservation] = Field(default_factory=list)
    constraint_rejected: list[str] = Field(default_factory=list)
    constraint_unknown: list[str] = Field(default_factory=list)
    possible_duplicates: list[tuple[str, str]] = Field(default_factory=list)


class StoredJudgment(BaseModel):
    """判断本体及其独立审计引用。"""

    candidate_id: str
    evidence_fingerprint: str
    judgment: RelevanceJudgment
    judgment_ref: ArtifactRef


class JudgmentLedger(BaseModel):
    records: dict[str, StoredJudgment] = Field(default_factory=dict)


class RankedSelection(BaseModel):
    references: list[ReferencePaper]
    candidate_ids: list[str]


class SurveyState(TypedDict, total=False):
    run_id: str
    request_ref: ArtifactRef
    budget_ref: ArtifactRef
    prompt_bundle_ref: ArtifactRef
    query_plan_ref: ArtifactRef
    pending_queries_ref: ArtifactRef
    round_observations_ref: ArtifactRef
    reference_observations_ref: ArtifactRef
    candidate_ledger_ref: ArtifactRef
    eligible_candidates_ref: ArtifactRef
    judgment_ledger_ref: ArtifactRef
    ranked_selection_ref: ArtifactRef
    survey_corpus_ref: ArtifactRef
    paper_source_request_ref: ArtifactRef | None
    stats_ref: ArtifactRef
    result_ref: ArtifactRef
    round_index: int
    seen_query_texts: list[str]
    refchain_expanded_ids: list[str]
    quarantine_enrichment_keys: list[str]
    new_relevant_ids: list[str]
    query_pulls: dict[str, int]
    arm_pulls: dict[str, int]
    search_cursors: dict[str, str]
    exhausted_search_arms: list[str]
    channel_pulls: dict[ChannelName, int]
    bandit_pulls: dict[ChannelName, int]
    channel_rewards: dict[ChannelName, int]
    rewarded_candidate_ids: list[str]
    batch_allocations: list[dict[ChannelName, int]]
    query_channel_batches: list[dict[str, str | int]]
    query_diagnostics: list[str]
    disabled_channels: list[ChannelName]
    retryable_failures: dict[ChannelName, int]
    attempted_pulls: int
    successful_pulls: int
    judged_count: int
    last_relevant_total: int
    saturation_streak: int
    saturation_curve: list[int]
    refchain_seed_count: int
    refchain_observation_count: int
    paginated_pulls: int
    started_at: float
    stop_reason: str
    errors: list[str]


@dataclass(slots=True)
class SurveyRuntime:
    chains: SurveyChains
    artifacts: ArtifactStore
    channels: dict[ChannelName, ChannelAdapter]
    emit: EmitEvent
    cancel: asyncio.Event
    deadline: float
    metrics: dict[str, int]


class _StructuredOutputFailure(RuntimeError):
    """结构化 LLM 输出在一次重试后仍无效。"""


def build_survey_graph(
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    """构造并编译唯一的在线 SPAR 控制流。"""
    builder = StateGraph(SurveyState, context_schema=SurveyRuntime)
    builder.add_node("load_request", load_request)
    builder.add_node("understand", understand)
    builder.add_node("retrieve_round", retrieve_round)
    builder.add_node("merge_and_dedup", merge_and_dedup)
    builder.add_node("apply_hard_filters", apply_hard_filters)
    builder.add_node("judge_new_candidates", judge_new_candidates)
    builder.add_node("expand_refchain", expand_refchain)
    builder.add_node("merge_and_judge_references", merge_and_judge_references)
    builder.add_node("evolve_queries", evolve_queries)
    builder.add_node("should_continue", update_stop_state)
    builder.add_node("rank_and_select", rank_and_select)
    builder.add_node("build_paper_source_request", build_paper_source_request)
    builder.add_node("persist_result", persist_result)

    builder.add_edge(START, "load_request")
    builder.add_edge("load_request", "understand")
    builder.add_edge("understand", "retrieve_round")
    builder.add_edge("retrieve_round", "merge_and_dedup")
    builder.add_edge("merge_and_dedup", "apply_hard_filters")
    builder.add_edge("apply_hard_filters", "judge_new_candidates")
    builder.add_edge("judge_new_candidates", "expand_refchain")
    builder.add_edge("expand_refchain", "merge_and_judge_references")
    builder.add_edge("merge_and_judge_references", "evolve_queries")
    builder.add_edge("evolve_queries", "should_continue")
    builder.add_conditional_edges(
        "should_continue",
        route_after_stop_check,
        {"continue": "retrieve_round", "stop": "rank_and_select"},
    )
    builder.add_edge("rank_and_select", "build_paper_source_request")
    builder.add_edge("build_paper_source_request", "persist_result")
    builder.add_edge("persist_result", END)
    return builder.compile(checkpointer=checkpointer)


async def load_request(state: SurveyState, runtime: Runtime[SurveyRuntime]) -> dict:
    """从 Athena artifact 边界加载并验证请求。"""
    context = runtime.context
    _check_cancel(context)
    request = SurveyRequest.model_validate_json(
        await context.artifacts.get_text(state["request_ref"])
    )
    budget = budget_for(request.mode)
    empty_candidates = await _put_model(context.artifacts, CandidateLedger())
    empty_judgments = await _put_model(context.artifacts, JudgmentLedger())
    budget_ref = await context.artifacts.put_text(json.dumps(asdict(budget)))
    prompt_bundle = getattr(context.chains, "prompt_bundle", None)
    prompt_bundle_ref = await context.artifacts.put_text(
        prompt_bundle.model_dump_json()
        if prompt_bundle is not None
        else json.dumps({"version": context.chains.prompt_bundle_version})
    )
    await context.emit(
        "academic_survey/started",
        state["request_ref"],
        {
            "run_id": state["run_id"],
            "mode": request.mode,
            "bundle_version": context.chains.prompt_bundle_version,
        },
    )
    return {
        "budget_ref": budget_ref,
        "prompt_bundle_ref": prompt_bundle_ref,
        "candidate_ledger_ref": empty_candidates,
        "judgment_ledger_ref": empty_judgments,
        "round_index": 0,
        "seen_query_texts": [],
        "refchain_expanded_ids": [],
        "quarantine_enrichment_keys": [],
        "query_pulls": {},
        "arm_pulls": {},
        "search_cursors": {},
        "exhausted_search_arms": [],
        "channel_pulls": {},
        "bandit_pulls": {},
        "channel_rewards": {},
        "rewarded_candidate_ids": [],
        "batch_allocations": [],
        "query_channel_batches": [],
        "query_diagnostics": [],
        "disabled_channels": [],
        "retryable_failures": {},
        "attempted_pulls": 0,
        "successful_pulls": 0,
        "judged_count": 0,
        "last_relevant_total": 0,
        "saturation_streak": 0,
        "saturation_curve": [],
        "refchain_seed_count": 0,
        "refchain_observation_count": 0,
        "paginated_pulls": 0,
        "started_at": time.time(),
        "errors": [
            f"unresolved constraint: {value}"
            for value in request.unresolved_constraints
        ],
    }


async def understand(state: SurveyState, runtime: Runtime[SurveyRuntime]) -> dict:
    """运行 LangChain Query Understanding 并收紧 query 预算。"""
    context = runtime.context
    _check_cancel(context)
    request = await _request(state, context)
    budget = budget_for(request.mode)
    plan = await _external(context, lambda: _retry_understand(context.chains, request))
    plan = enforce_plan_policy(plan, request)
    diagnostics: list[str] = []
    queries = unique_queries(
        plan.queries,
        [],
        budget.max_initial_queries,
        threshold=budget.query_overlap_threshold,
        diagnostics=diagnostics,
    )
    if not queries:
        raise ValueError("Query Understanding produced no unique queries")
    plan = plan.model_copy(update={"queries": queries})
    plan_ref = await _put_model(context.artifacts, plan)
    pending_ref = await _put_models(context.artifacts, queries)
    await context.emit(
        "academic_survey/query_plan_ready",
        plan_ref,
        {"queries": len(queries)},
    )
    return {
        "query_plan_ref": plan_ref,
        "pending_queries_ref": pending_ref,
        "seen_query_texts": [query.text for query in queries],
        "query_diagnostics": diagnostics,
    }


async def retrieve_round(state: SurveyState, runtime: Runtime[SurveyRuntime]) -> dict:
    """优先覆盖 query family/channel，再按 UCB 拉取可分页检索 arm。"""
    context = runtime.context
    _check_cancel(context)
    request = await _request(state, context)
    budget = budget_for(request.mode)
    plan = await _plan(state, context)
    queries = await _load_queries(context.artifacts, state["pending_queries_ref"])
    existing = await _load_candidate_ledger(state, context)
    used = len(existing.candidates) + len(existing.quarantined)
    remaining = max(0, budget.max_candidates - used)
    pulls = dict(state.get("channel_pulls", {}))
    bandit_pulls = dict(state.get("bandit_pulls", {}))
    query_pulls = dict(state.get("query_pulls", {}))
    arm_pulls = dict(state.get("arm_pulls", {}))
    cursors = dict(state.get("search_cursors", {}))
    exhausted = list(state.get("exhausted_search_arms", []))
    errors = list(state.get("errors", []))
    disabled = list(state.get("disabled_channels", []))
    failures = dict(state.get("retryable_failures", {}))
    potential = [
        (query, channel_name, _search_arm_key(query, channel_name))
        for query in queries
        for channel_name in query.channels
        if channel_name not in disabled
        and _search_arm_key(query, channel_name) not in exhausted
    ]
    selected = allocate_search_indices(
        [(query.query_id, channel, arm_key) for query, channel, arm_key in potential],
        query_pulls,
        arm_pulls,
        bandit_pulls,
        state.get("channel_rewards", {}),
        budget,
    )
    allocation: dict[ChannelName, int] = {}
    calls: list[tuple[SearchQuery, ChannelName, str, str | None, ChannelAdapter]] = []
    round_number = state.get("round_index", 0) + 1
    query_channel_batches = list(state.get("query_channel_batches", []))
    attempted = state.get("attempted_pulls", 0)
    for index in selected:
        query, channel_name, arm_key = potential[index]
        cursor = cursors.get(arm_key)
        batch: dict[str, str | int] = {
            "round": round_number,
            "query_id": query.query_id,
            "channel": channel_name,
        }
        if cursor:
            batch["cursor"] = cursor
        attempted += 1
        pulls[channel_name] = pulls.get(channel_name, 0) + 1
        bandit_pulls[channel_name] = bandit_pulls.get(channel_name, 0) + 1
        allocation[channel_name] = allocation.get(channel_name, 0) + 1
        adapter = context.channels.get(channel_name)
        if adapter is None:
            _add_error(errors, f"{channel_name}: adapter is not configured")
            disabled.append(channel_name)
        else:
            query_channel_batches.append(batch)
            arm_pulls[arm_key] = arm_pulls.get(arm_key, 0) + 1
            calls.append((query, channel_name, arm_key, cursor, adapter))

    async def execute(
        query: SearchQuery,
        channel_name: ChannelName,
        cursor: str | None,
        adapter: ChannelAdapter,
    ):
        rewrite_error = None
        try:
            rewritten = await _external(
                context,
                lambda: _retry_rewrite(
                    context.chains, request, plan, query, channel_name
                ),
            )
            channel_query = query.model_copy(update={"text": rewritten.text})
        except TimeoutError as error:
            rewrite_error = f"{channel_name} rewrite: {type(error).__name__}: {error}"
            channel_query = query
        except Exception as error:
            raise _StructuredOutputFailure(
                f"{channel_name} query rewrite failed: {error}"
            ) from error
        result = await _external(
            context,
            lambda: adapter.search_page(
                channel_query,
                request.constraints,
                max(1, min(budget.search_batch_size, remaining)),
                cursor,
                context.cancel,
            ),
        )
        return channel_query, result, rewrite_error

    results = await asyncio.gather(
        *(
            execute(query, channel_name, cursor, adapter)
            for query, channel_name, _, cursor, adapter in calls
        ),
        return_exceptions=True,
    )
    observations: list[CandidateObservation] = []
    successful = state.get("successful_pulls", 0)
    paginated = state.get("paginated_pulls", 0)
    for (query, channel_name, arm_key, cursor, _), result in zip(
        calls, results, strict=True
    ):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, _StructuredOutputFailure):
            raise result
        if isinstance(result, BaseException):
            _add_error(errors, f"{channel_name}: {type(result).__name__}: {result}")
            _record_channel_failure(channel_name, result, failures, disabled)
            continue
        channel_query, page, rewrite_error = result
        if rewrite_error:
            _add_error(errors, rewrite_error)
        successful += 1
        query_pulls[query.query_id] = query_pulls.get(query.query_id, 0) + 1
        if cursor:
            paginated += 1
        if page.next_cursor is None:
            exhausted.append(arm_key)
            cursors.pop(arm_key, None)
        else:
            cursors[arm_key] = page.next_cursor
        observations.extend(
            item.model_copy(
                update={
                    "channel": channel_name,
                    "query_id": channel_query.query_id,
                    "query_text": channel_query.text,
                    "depth": 0,
                }
            )
            for item in page.observations
        )
    _check_cancel(context)
    observations = observations[:remaining]
    observations_ref = await _put_models(context.artifacts, observations)
    round_index = round_number
    await context.emit(
        "academic_survey/retrieval_round",
        observations_ref,
        {
            "round": round_index,
            "pulls": len(calls),
            "observations": len(observations),
        },
    )
    return {
        "round_index": round_index,
        "round_observations_ref": observations_ref,
        "query_pulls": query_pulls,
        "arm_pulls": arm_pulls,
        "search_cursors": cursors,
        "exhausted_search_arms": list(dict.fromkeys(exhausted)),
        "channel_pulls": pulls,
        "bandit_pulls": bandit_pulls,
        "batch_allocations": [*state.get("batch_allocations", []), allocation],
        "query_channel_batches": query_channel_batches,
        "disabled_channels": list(dict.fromkeys(disabled)),
        "retryable_failures": failures,
        "attempted_pulls": attempted,
        "successful_pulls": successful,
        "paginated_pulls": paginated,
        "errors": errors,
    }


async def merge_and_dedup(state: SurveyState, runtime: Runtime[SurveyRuntime]) -> dict:
    """把本轮 observations 合并进 ID 账本。"""
    context = runtime.context
    ledger = await _load_candidate_ledger(state, context)
    current = await _load_observations(
        context.artifacts, state["round_observations_ref"]
    )
    all_observations = [
        observation
        for candidate in ledger.candidates
        for observation in candidate.observations
    ]
    all_observations.extend(ledger.quarantined)
    all_observations.extend(current)
    candidates, quarantined = merge_observations(all_observations)
    request = await _request(state, context)
    budget = budget_for(request.mode)
    enrichment = await _enrich_quarantine(
        quarantined,
        state,
        context,
        request,
        max(0, budget.max_candidates - len(candidates)),
    )
    if enrichment["observations"]:
        candidates, quarantined = merge_observations(
            [*all_observations, *enrichment["observations"]]
        )
        enriched_titles = {
            " ".join(item.title.casefold().split())
            for item in enrichment["observations"]
        }
        quarantined = [
            item
            for item in quarantined
            if " ".join(item.title.casefold().split()) not in enriched_titles
        ]
    candidates = candidates[: budget.max_candidates]
    merged = CandidateLedger(
        candidates=candidates,
        quarantined=quarantined[: max(0, budget.max_candidates - len(candidates))],
        possible_duplicates=possible_duplicate_pairs(candidates),
    )
    return {
        "candidate_ledger_ref": await _put_model(context.artifacts, merged),
        **{key: value for key, value in enrichment.items() if key != "observations"},
    }


async def apply_hard_filters(
    state: SurveyState, runtime: Runtime[SurveyRuntime]
) -> dict:
    """在任何 LLM 判断之前执行显式硬约束。"""
    context = runtime.context
    request = await _request(state, context)
    ledger = await _load_candidate_ledger(state, context)
    eligible = _partition_constraints(ledger, request)
    return {
        "candidate_ledger_ref": await _put_model(context.artifacts, ledger),
        "eligible_candidates_ref": await _put_models(context.artifacts, eligible),
    }


async def judge_new_candidates(
    state: SurveyState, runtime: Runtime[SurveyRuntime]
) -> dict:
    """判断尚未见过或证据文本已更新的候选。"""
    return await _judge_eligible(state, runtime.context, reward_search=True)


async def expand_refchain(state: SurveyState, runtime: Runtime[SurveyRuntime]) -> dict:
    """对本轮 newly relevant 根候选做且只做一次单跳扩展。"""
    context = runtime.context
    _check_cancel(context)
    request = await _request(state, context)
    budget = budget_for(request.mode)
    ledger = await _load_candidate_ledger(state, context)
    expanded = list(state.get("refchain_expanded_ids", []))
    remaining_seeds = budget.max_refchain_seeds - len(expanded)
    seed_ids = [
        candidate_id
        for candidate_id in state.get("new_relevant_ids", [])
        if candidate_id not in expanded
    ][:remaining_seeds]
    candidates = {item.candidate_id: item for item in ledger.candidates}
    calls: list[tuple[str, ChannelName, ChannelAdapter, int]] = []
    pulls = dict(state.get("channel_pulls", {}))
    attempted = state.get("attempted_pulls", 0)
    errors = list(state.get("errors", []))
    disabled = list(state.get("disabled_channels", []))
    failures = dict(state.get("retryable_failures", {}))
    for seed_id in seed_ids:
        seed = candidates[seed_id]
        configured = _reference_channels(seed, context, disabled)
        per_channel = max(
            1,
            (budget.references_per_seed + max(1, len(configured)) - 1)
            // max(1, len(configured)),
        )
        for channel_name in configured:
            attempted += 1
            pulls[channel_name] = pulls.get(channel_name, 0) + 1
            calls.append(
                (seed_id, channel_name, context.channels[channel_name], per_channel)
            )

    results = await asyncio.gather(
        *(
            _external(
                context,
                lambda adapter=adapter, seed_id=seed_id, limit=limit: adapter.references(
                    candidates[seed_id], limit, context.cancel
                ),
                reserve_seconds=budget.refchain_judgment_reserve_seconds,
            )
            for seed_id, _, adapter, limit in calls
        ),
        return_exceptions=True,
    )
    by_seed: dict[str, list[CandidateObservation]] = {
        seed_id: [] for seed_id in seed_ids
    }
    successful = state.get("successful_pulls", 0)
    for (seed_id, channel_name, _, _), result in zip(calls, results, strict=True):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException):
            _add_error(
                errors, f"{channel_name} references: {type(result).__name__}: {result}"
            )
            _record_channel_failure(channel_name, result, failures, disabled)
            continue
        successful += 1
        for item in result:
            by_seed[seed_id].append(
                item.model_copy(
                    update={
                        "channel": channel_name,
                        "query_id": f"ref:{seed_id}",
                        "query_text": candidates[seed_id].title,
                        "depth": 1,
                    }
                )
            )
    _check_cancel(context)
    references = [
        item
        for seed_id in seed_ids
        for item in by_seed[seed_id][: budget.references_per_seed]
    ]
    references_ref = await _put_models(context.artifacts, references)
    expanded.extend(seed_ids)
    await context.emit(
        "academic_survey/refchain_complete",
        references_ref,
        {"seeds": len(seed_ids), "observations": len(references)},
    )
    return {
        "reference_observations_ref": references_ref,
        "refchain_expanded_ids": expanded,
        "channel_pulls": pulls,
        "disabled_channels": list(dict.fromkeys(disabled)),
        "retryable_failures": failures,
        "refchain_seed_count": state.get("refchain_seed_count", 0) + len(seed_ids),
        "refchain_observation_count": state.get("refchain_observation_count", 0)
        + len(references),
        "attempted_pulls": attempted,
        "successful_pulls": successful,
        "errors": errors,
    }


async def merge_and_judge_references(
    state: SurveyState, runtime: Runtime[SurveyRuntime]
) -> dict:
    """引用候选复用同一 ID、硬约束和 evidence gate。"""
    context = runtime.context
    ledger = await _load_candidate_ledger(state, context)
    references = await _load_observations(
        context.artifacts, state["reference_observations_ref"]
    )
    all_observations = [
        observation
        for candidate in ledger.candidates
        for observation in candidate.observations
    ]
    all_observations.extend(ledger.quarantined)
    all_observations.extend(references)
    candidates, quarantined = merge_observations(all_observations)
    request = await _request(state, context)
    budget = budget_for(request.mode)
    candidates = candidates[: budget.max_candidates]
    ledger = CandidateLedger(
        candidates=candidates,
        quarantined=quarantined[: max(0, budget.max_candidates - len(candidates))],
        possible_duplicates=possible_duplicate_pairs(candidates),
    )
    eligible = _partition_constraints(ledger, request)
    state = dict(state)
    state["candidate_ledger_ref"] = await _put_model(context.artifacts, ledger)
    state["eligible_candidates_ref"] = await _put_models(context.artifacts, eligible)
    update = await _judge_eligible(state, context, reward_search=False)
    update["candidate_ledger_ref"] = state["candidate_ledger_ref"]
    update["eligible_candidates_ref"] = state["eligible_candidates_ref"]
    return update


async def evolve_queries(state: SurveyState, runtime: Runtime[SurveyRuntime]) -> dict:
    """追加 LangChain 生成的查询，并保留尚未耗尽的检索 arm。"""
    context = runtime.context
    request = await _request(state, context)
    budget = budget_for(request.mode)
    judgments = await _load_judgment_ledger(state, context)
    relevant_total = sum(
        record.judgment.verdict == "relevant" for record in judgments.records.values()
    )
    if state["round_index"] >= budget.max_rounds:
        return {
            "pending_queries_ref": await _put_models(context.artifacts, []),
            **_saturation_update(state, relevant_total),
        }
    plan = await _plan(state, context)
    active = await _load_queries(context.artifacts, state["pending_queries_ref"])
    candidates = await _load_candidates(
        context.artifacts, state["eligible_candidates_ref"]
    )
    accepted = [
        candidate
        for candidate in candidates
        if (record := judgments.records.get(candidate.candidate_id))
        and record.judgment.verdict == "relevant"
    ]
    errors = list(state.get("errors", []))
    diagnostics = list(state.get("query_diagnostics", []))
    try:
        evolution = await _external(
            context,
            lambda: _retry_evolve(
                context.chains,
                request,
                plan,
                accepted,
                state.get("seen_query_texts", []),
            ),
        )
        queries = unique_queries(
            evolution.queries,
            state.get("seen_query_texts", []),
            3,
            threshold=budget.query_overlap_threshold,
            diagnostics=diagnostics,
        )
    except TimeoutError as error:
        _add_error(errors, f"query evolution: {type(error).__name__}: {error}")
        queries = []
    exhausted = set(state.get("exhausted_search_arms", []))
    disabled = set(state.get("disabled_channels", []))
    pending = [
        query
        for query in [*active, *queries]
        if any(
            channel not in disabled and _search_arm_key(query, channel) not in exhausted
            for channel in query.channels
        )
    ]
    pending_ref = await _put_models(context.artifacts, pending)
    seen = [*state.get("seen_query_texts", []), *(item.text for item in queries)]
    return {
        "pending_queries_ref": pending_ref,
        "seen_query_texts": seen,
        "query_diagnostics": diagnostics,
        "errors": errors,
        **_saturation_update(state, relevant_total),
    }


async def update_stop_state(
    state: SurveyState, runtime: Runtime[SurveyRuntime]
) -> dict:
    """在单一节点应用全部停止条件。"""
    context = runtime.context
    _check_cancel(context)
    request = await _request(state, context)
    budget = budget_for(request.mode)
    ledger = await _load_candidate_ledger(state, context)
    judgments = await _load_judgment_ledger(state, context)
    relevant = sum(
        record.judgment.verdict == "relevant" for record in judgments.records.values()
    )
    deadline_reached = time.monotonic() >= context.deadline
    pending = await _load_queries(context.artifacts, state["pending_queries_ref"])
    if (
        state.get("attempted_pulls", 0) > 0
        and state.get("successful_pulls", 0) == 0
        and relevant == 0
        and not pending
        and not deadline_reached
    ):
        detail = "; ".join(state.get("errors", []))
        raise RuntimeError(f"all configured retrieval channels failed: {detail}")

    unique_candidates = len(ledger.candidates) + len(ledger.quarantined)
    stop_reason = ""
    errors = list(state.get("errors", []))
    if deadline_reached:
        stop_reason = "max_seconds"
        _add_error(errors, "survey wall-clock deadline reached")
    elif unique_candidates >= budget.max_candidates:
        stop_reason = "candidate_cap"
    elif state.get("judged_count", 0) >= budget.max_judgments:
        stop_reason = "judgment_cap"
    elif state["round_index"] >= budget.max_rounds:
        stop_reason = "max_rounds"
    elif not pending:
        stop_reason = "no_new_queries"
    elif state.get("saturation_streak", 0) >= 2:
        stop_reason = "saturation"
    return {"stop_reason": stop_reason, "errors": errors}


def route_after_stop_check(state: SurveyState) -> str:
    return "stop" if state.get("stop_reason") else "continue"


async def rank_and_select(state: SurveyState, runtime: Runtime[SurveyRuntime]) -> dict:
    """只对 relevant 且通过硬约束的候选做最终排序。"""
    context = runtime.context
    request = await _request(state, context)
    budget = budget_for(request.mode)
    plan = await _plan(state, context)
    candidates = await _load_candidates(
        context.artifacts, state["eligible_candidates_ref"]
    )
    ledger = await _load_judgment_ledger(state, context)
    relevant = [
        candidate
        for candidate in candidates
        if (record := ledger.records.get(candidate.candidate_id))
        and record.judgment.verdict == "relevant"
    ]
    judgments = {
        candidate.candidate_id: ledger.records[candidate.candidate_id].judgment
        for candidate in relevant
    }
    cap = min(budget.max_final_papers, request.paper_source_policy.max_papers)
    ranked = rank_candidates(relevant, judgments, plan)[:cap]
    references: list[ReferencePaper] = []
    candidate_ids: list[str] = []
    for rank, item in enumerate(ranked, start=1):
        candidate = item.candidate
        record = ledger.records[candidate.candidate_id]
        breakdown_ref = await _put_model(context.artifacts, item.breakdown)
        references.append(
            ReferencePaper(
                paper_id=candidate.identity.paper_key(),
                identity=candidate.identity,
                title=candidate.title,
                abstract=candidate.abstract,
                year=candidate.year,
                venue=candidate.venue,
                rank=rank,
                rank_score=item.breakdown.final_score,
                rank_breakdown_ref=breakdown_ref,
                judgment_ref=record.judgment_ref,
                retrieval_channels=list(
                    dict.fromkeys(obs.channel for obs in candidate.observations)
                ),
                matched_queries=list(
                    dict.fromkeys(obs.query_text for obs in candidate.observations)
                ),
            )
        )
        candidate_ids.append(candidate.candidate_id)
    selection = RankedSelection(references=references, candidate_ids=candidate_ids)
    return {"ranked_selection_ref": await _put_model(context.artifacts, selection)}


async def build_paper_source_request(
    state: SurveyState, runtime: Runtime[SurveyRuntime]
) -> dict:
    """先持久化 corpus，再构造 paper_source 可直接验证的输入。"""
    context = runtime.context
    request = await _request(state, context)
    budget = budget_for(request.mode)
    candidates = await _load_candidate_ledger(state, context)
    judgments = await _load_judgment_ledger(state, context)
    selection = RankedSelection.model_validate_json(
        await context.artifacts.get_text(state["ranked_selection_ref"])
    )
    counts = {"relevant": 0, "irrelevant": 0, "uncertain": 0}
    for record in judgments.records.values():
        counts[record.judgment.verdict] += 1
    plan = await _plan(state, context)
    planned_query_ids = {query.query_id for query in plan.queries}
    executed_query_ids = {
        str(batch["query_id"])
        for batch in state.get("query_channel_batches", [])
        if batch.get("query_id") in planned_query_ids
    }
    planned_queries = len(planned_query_ids)
    stats = SurveyStats(
        rounds=state["round_index"],
        observations=sum(len(item.observations) for item in candidates.candidates)
        + len(candidates.quarantined),
        unique_candidates=len(candidates.candidates),
        quarantined=len(candidates.quarantined),
        constraint_rejected=len(candidates.constraint_rejected),
        constraint_unknown=len(candidates.constraint_unknown),
        possible_duplicates=len(candidates.possible_duplicates),
        relevant=counts["relevant"],
        irrelevant=counts["irrelevant"],
        uncertain=counts["uncertain"],
        channel_pulls=state.get("channel_pulls", {}),
        channel_rewards=state.get("channel_rewards", {}),
        batch_allocations=state.get("batch_allocations", []),
        query_channel_batches=state.get("query_channel_batches", []),
        planned_queries=planned_queries,
        executed_queries=len(executed_query_ids),
        query_coverage=(
            len(executed_query_ids) / planned_queries if planned_queries else 0
        ),
        paginated_pulls=state.get("paginated_pulls", 0),
        query_diagnostics=state.get("query_diagnostics", []),
        cache_hits=context.metrics.get("cache_hits", 0),
        cache_misses=context.metrics.get("cache_misses", 0),
        llm_calls=context.metrics.get("llm_calls", 0),
        llm_tokens=context.metrics.get("llm_tokens", 0),
        refchain_seeds=state.get("refchain_seed_count", 0),
        refchain_observations=state.get("refchain_observation_count", 0),
        saturation_curve=state.get("saturation_curve", []),
        wall_seconds=max(0, time.time() - state["started_at"]),
        stop_reason=state["stop_reason"],
        errors=state.get("errors", []),
    )
    stats_ref = await _put_model(context.artifacts, stats)
    status = "partial" if stats.errors else "complete"
    corpus = SurveyCorpus(
        request_ref=state["request_ref"],
        budget_ref=state["budget_ref"],
        query_plan_ref=state["query_plan_ref"],
        prompt_bundle_version=context.chains.prompt_bundle_version,
        prompt_bundle_ref=state["prompt_bundle_ref"],
        budget_version=budget.version,
        status=status,
        reference_papers=selection.references,
        candidate_ledger_ref=state["candidate_ledger_ref"],
        judgment_ledger_ref=state["judgment_ledger_ref"],
        stats_ref=stats_ref,
    )
    corpus_ref = await _put_model(context.artifacts, corpus)
    source_request_ref = None
    if selection.references:
        by_id = {item.candidate_id: item for item in candidates.candidates}
        papers = [
            _paper_ref(
                reference, by_id[candidate_id], context.chains.prompt_bundle_version
            )
            for reference, candidate_id in zip(
                selection.references, selection.candidate_ids, strict=True
            )
        ]
        source_request = PaperSourceRequest(
            papers=papers,
            policy=request.paper_source_policy,
            corpus_ref=corpus_ref,
        )
        source_request_ref = await _put_model(context.artifacts, source_request)
    await context.emit(
        "academic_survey/saturation",
        stats_ref,
        {"stop_reason": stats.stop_reason},
    )
    return {
        "stats_ref": stats_ref,
        "survey_corpus_ref": corpus_ref,
        "paper_source_request_ref": source_request_ref,
    }


async def persist_result(state: SurveyState, runtime: Runtime[SurveyRuntime]) -> dict:
    """持久化 Athena Turn 的顶层结果。"""
    context = runtime.context
    errors = state.get("errors", [])
    result = AcademicSurveyResult(
        status="partial" if errors else "complete",
        survey_corpus_ref=state["survey_corpus_ref"],
        paper_source_request_ref=state.get("paper_source_request_ref"),
        prompt_bundle_version=context.chains.prompt_bundle_version,
        stats_ref=state["stats_ref"],
        warnings=errors,
    )
    result_ref = await _put_model(context.artifacts, result)
    selection = RankedSelection.model_validate_json(
        await context.artifacts.get_text(state["ranked_selection_ref"])
    )
    await context.emit(
        "academic_survey/completed",
        result_ref,
        {
            "corpus_ref": state["survey_corpus_ref"],
            "papers": len(selection.references),
        },
    )
    return {"result_ref": result_ref}


async def _judge_eligible(
    state: SurveyState,
    context: SurveyRuntime,
    *,
    reward_search: bool,
) -> dict:
    _check_cancel(context)
    request = await _request(state, context)
    budget = budget_for(request.mode)
    plan = await _plan(state, context)
    candidates = await _load_candidates(
        context.artifacts, state["eligible_candidates_ref"]
    )
    ledger = await _load_judgment_ledger(state, context)
    previous = {
        candidate_id: record.judgment.verdict
        for candidate_id, record in ledger.records.items()
    }
    remaining = max(0, budget.max_judgments - state.get("judged_count", 0))
    pending = [
        candidate
        for candidate in candidates
        if (record := ledger.records.get(candidate.candidate_id)) is None
        or record.evidence_fingerprint != _evidence_fingerprint(candidate)
    ][:remaining]
    batches = [
        pending[index : index + budget.judgment_batch_size]
        for index in range(0, len(pending), budget.judgment_batch_size)
    ]
    semaphore = asyncio.Semaphore(budget.judgment_batch_concurrency)

    async def judge_batch(batch: list[SurveyCandidate]):
        async with semaphore:
            _check_cancel(context)
            try:
                draft_batch = await _external(
                    context,
                    lambda: _retry_judge_batch(context.chains, request, plan, batch),
                )
                error = None
            except TimeoutError as exception:
                draft_batch = None
                error = exception
            drafts = (
                {item.candidate_id: item for item in draft_batch.judgments}
                if draft_batch is not None
                else {}
            )
            results = []
            for candidate in batch:
                item = drafts.get(candidate.candidate_id)
                draft = (
                    JudgmentDraft(
                        criteria=item.criteria,
                        relevance_score=item.relevance_score,
                    )
                    if item is not None
                    else JudgmentDraft(criteria=[], relevance_score=0)
                )
                judgment = validate_judgment(
                    plan, candidate, draft, context.chains.prompt_bundle_version
                )
                judgment_ref = await _put_model(context.artifacts, judgment)
                results.append((candidate, judgment, judgment_ref, error))
            return results

    batch_results = await asyncio.gather(*(judge_batch(batch) for batch in batches))
    results = [item for batch in batch_results for item in batch]
    errors = list(state.get("errors", []))
    for candidate, judgment, judgment_ref, error in results:
        if error is not None:
            _add_error(
                errors,
                f"judgment {candidate.candidate_id}: {type(error).__name__}: {error}",
            )
        ledger.records[candidate.candidate_id] = StoredJudgment(
            candidate_id=candidate.candidate_id,
            evidence_fingerprint=_evidence_fingerprint(candidate),
            judgment=judgment,
            judgment_ref=judgment_ref,
        )
    _check_cancel(context)
    new_relevant = [
        candidate.candidate_id
        for candidate in pending
        if ledger.records[candidate.candidate_id].judgment.verdict == "relevant"
        and previous.get(candidate.candidate_id) != "relevant"
    ]
    rewards = dict(state.get("channel_rewards", {}))
    rewarded = list(state.get("rewarded_candidate_ids", []))
    if reward_search:
        by_id = {candidate.candidate_id: candidate for candidate in candidates}
        for candidate_id in new_relevant:
            if candidate_id in rewarded:
                continue
            root_observation = next(
                (item for item in by_id[candidate_id].observations if item.depth == 0),
                None,
            )
            if root_observation is not None:
                rewards[root_observation.channel] = (
                    rewards.get(root_observation.channel, 0) + 1
                )
                rewarded.append(candidate_id)
    ledger_ref = await _put_model(context.artifacts, ledger)
    verdicts = [record.judgment.verdict for record in ledger.records.values()]
    await context.emit(
        "academic_survey/judgment_progress",
        ledger_ref,
        {
            "judged": len(ledger.records),
            "relevant": verdicts.count("relevant"),
            "uncertain": verdicts.count("uncertain"),
            "irrelevant": verdicts.count("irrelevant"),
        },
    )
    return {
        "judgment_ledger_ref": ledger_ref,
        "judged_count": state.get("judged_count", 0) + len(pending),
        "new_relevant_ids": new_relevant,
        "channel_rewards": rewards,
        "rewarded_candidate_ids": rewarded,
        "errors": errors,
    }


async def _retry_judge_batch(
    chains: SurveyChains,
    request: SurveyRequest,
    plan: QueryPlan,
    candidates: list[SurveyCandidate],
):
    try:
        return await chains.judge_batch(request, plan, candidates)
    except Exception:
        return await chains.judge_batch(request, plan, candidates)


async def _retry_understand(chains: SurveyChains, request: SurveyRequest) -> QueryPlan:
    try:
        return await chains.understand(request)
    except Exception:
        return await chains.understand(request)


async def _retry_rewrite(
    chains: SurveyChains,
    request: SurveyRequest,
    plan: QueryPlan,
    query: SearchQuery,
    channel: ChannelName,
) -> RewrittenQuery:
    method = getattr(chains, "rewrite", None)
    if method is None:
        return RewrittenQuery(text=query.text)
    try:
        return await method(request, plan, query, channel)
    except Exception:
        return await method(request, plan, query, channel)


async def _retry_evolve(
    chains: SurveyChains,
    request: SurveyRequest,
    plan: QueryPlan,
    accepted: list[SurveyCandidate],
    searched_queries: list[str],
) -> QueryEvolution:
    try:
        return await chains.evolve(request, plan, accepted, searched_queries)
    except Exception:
        return await chains.evolve(request, plan, accepted, searched_queries)


def _saturation_update(state: SurveyState, relevant: int) -> dict:
    delta = relevant - state.get("last_relevant_total", 0)
    threshold = max(2, int(relevant * 0.05))
    streak = state.get("saturation_streak", 0) + 1 if delta < threshold else 0
    return {
        "last_relevant_total": relevant,
        "saturation_streak": streak,
        "saturation_curve": [*state.get("saturation_curve", []), relevant],
    }


def _search_arm_key(query: SearchQuery, channel: ChannelName) -> str:
    normalized = " ".join(query.text.casefold().split())
    return hashlib.sha256(f"{normalized}\0{channel}".encode()).hexdigest()


def _paper_ref(
    reference: ReferencePaper,
    candidate: SurveyCandidate,
    prompt_bundle_version: str,
) -> PaperRef:
    metadata = {
        "title": candidate.title,
        "rank": str(reference.rank),
        "rank_score": str(reference.rank_score),
        "judgment_ref": reference.judgment_ref,
        "prompt_bundle_version": prompt_bundle_version,
    }
    if candidate.year is not None:
        metadata["year"] = str(candidate.year)
    if candidate.venue:
        metadata["venue"] = candidate.venue
    if candidate.citation_count is not None:
        metadata["citation_count"] = str(candidate.citation_count)
    hints: list[SourceHint] = []
    seen_hints: set[tuple[str, str]] = set()
    for observation in candidate.observations:
        for hint in observation.hints:
            key = (hint.url, hint.kind)
            if key not in seen_hints:
                hints.append(hint)
                seen_hints.add(key)
    return PaperRef(
        identity=reference.identity,
        upstream_metadata=metadata,
        hints=hints,
        retrieval_channels=reference.retrieval_channels,
        matched_queries=reference.matched_queries,
    )


async def _enrich_quarantine(
    quarantined: list[CandidateObservation],
    state: SurveyState,
    context: SurveyRuntime,
    request: SurveyRequest,
    limit: int,
) -> dict:
    """通过 OpenAlex 或 S2 精确标题命中为 title-only observation 补一次 ID。"""
    attempted_keys = list(state.get("quarantine_enrichment_keys", []))
    disabled = list(state.get("disabled_channels", []))
    failures = dict(state.get("retryable_failures", {}))
    errors = list(state.get("errors", []))
    pulls = dict(state.get("channel_pulls", {}))
    attempted = state.get("attempted_pulls", 0)
    successful = state.get("successful_pulls", 0)
    available = [
        name
        for name in ("openalex", "semantic_scholar")
        if name in context.channels and name not in disabled
    ]
    if not available or limit <= 0:
        return {
            "observations": [],
            "quarantine_enrichment_keys": attempted_keys,
        }

    calls = []
    for observation in quarantined:
        key = hashlib.sha256(
            " ".join(observation.title.casefold().split()).encode()
        ).hexdigest()
        if key in attempted_keys:
            continue
        attempted_keys.append(key)
        name = available[0]
        attempted += 1
        pulls[name] = pulls.get(name, 0) + 1
        query = SearchQuery(
            query_id=f"enrich:{key[:12]}",
            text=observation.title,
            channels=[name],
        )
        calls.append((observation.title, name, query, context.channels[name]))
        if len(calls) == limit:
            break

    results = await asyncio.gather(
        *(
            _external(
                context,
                lambda query=query, adapter=adapter: adapter.search(
                    query, request.constraints, 3, context.cancel
                ),
            )
            for _, _, query, adapter in calls
        ),
        return_exceptions=True,
    )
    enriched = []
    for (title, name, _, _), result in zip(calls, results, strict=True):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException):
            _record_channel_failure(name, result, failures, disabled)
            _add_error(errors, f"{name} enrichment: {type(result).__name__}: {result}")
            continue
        successful += 1
        normalized = " ".join(title.casefold().split())
        match = next(
            (
                item
                for item in result
                if " ".join(item.title.casefold().split()) == normalized
                and item.identity.to_paper_identity() is not None
            ),
            None,
        )
        if match is not None:
            enriched.append(
                match.model_copy(
                    update={
                        "channel": name,
                        "query_id": f"enrich:{hashlib.sha256(normalized.encode()).hexdigest()[:12]}",
                        "query_text": title,
                    }
                )
            )
    return {
        "observations": enriched,
        "quarantine_enrichment_keys": attempted_keys,
        "channel_pulls": pulls,
        "attempted_pulls": attempted,
        "successful_pulls": successful,
        "disabled_channels": list(dict.fromkeys(disabled)),
        "retryable_failures": failures,
        "errors": errors,
    }


def _partition_constraints(
    ledger: CandidateLedger, request: SurveyRequest
) -> list[SurveyCandidate]:
    statuses = {
        candidate.candidate_id: constraint_status(candidate, request.constraints)
        for candidate in ledger.candidates
    }
    ledger.constraint_rejected = [
        candidate_id
        for candidate_id, status in statuses.items()
        if status == "rejected"
    ]
    ledger.constraint_unknown = [
        candidate_id for candidate_id, status in statuses.items() if status == "unknown"
    ]
    return [
        candidate
        for candidate in ledger.candidates
        if statuses[candidate.candidate_id] == "eligible"
    ]


async def _request(state: SurveyState, context: SurveyRuntime) -> SurveyRequest:
    return SurveyRequest.model_validate_json(
        await context.artifacts.get_text(state["request_ref"])
    )


async def _plan(state: SurveyState, context: SurveyRuntime) -> QueryPlan:
    return QueryPlan.model_validate_json(
        await context.artifacts.get_text(state["query_plan_ref"])
    )


async def _load_candidate_ledger(
    state: SurveyState, context: SurveyRuntime
) -> CandidateLedger:
    return CandidateLedger.model_validate_json(
        await context.artifacts.get_text(state["candidate_ledger_ref"])
    )


async def _load_judgment_ledger(
    state: SurveyState, context: SurveyRuntime
) -> JudgmentLedger:
    return JudgmentLedger.model_validate_json(
        await context.artifacts.get_text(state["judgment_ledger_ref"])
    )


async def _load_candidates(
    artifacts: ArtifactStore, ref: ArtifactRef
) -> list[SurveyCandidate]:
    return [
        SurveyCandidate.model_validate(item)
        for item in json.loads(await artifacts.get_text(ref))
    ]


async def _load_queries(
    artifacts: ArtifactStore, ref: ArtifactRef
) -> list[SearchQuery]:
    return [
        SearchQuery.model_validate(item)
        for item in json.loads(await artifacts.get_text(ref))
    ]


async def _load_observations(
    artifacts: ArtifactStore, ref: ArtifactRef
) -> list[CandidateObservation]:
    return [
        CandidateObservation.model_validate(item)
        for item in json.loads(await artifacts.get_text(ref))
    ]


async def _put_model(artifacts: ArtifactStore, value: BaseModel) -> ArtifactRef:
    return await artifacts.put_text(value.model_dump_json())


async def _put_models(artifacts: ArtifactStore, values: list[BaseModel]) -> ArtifactRef:
    return await artifacts.put_text(
        json.dumps([value.model_dump(mode="json") for value in values])
    )


def _evidence_fingerprint(candidate: SurveyCandidate) -> str:
    payload = f"{candidate.title}\0{candidate.abstract}".encode()
    return hashlib.sha256(payload).hexdigest()


def _add_error(errors: list[str], message: str) -> None:
    if message not in errors:
        errors.append(message)


def _record_channel_failure(
    channel: ChannelName,
    error: BaseException,
    failures: dict[ChannelName, int],
    disabled: list[ChannelName],
) -> None:
    retryable = not isinstance(error, ChannelFailure) or error.retryable
    failures[channel] = failures.get(channel, 0) + 1
    if not retryable or failures[channel] >= 2:
        disabled.append(channel)


def _reference_channels(
    paper: SurveyCandidate,
    context: SurveyRuntime,
    disabled: list[ChannelName],
) -> list[ChannelName]:
    """按可解析身份选择引用通道，而不局限于首次命中的检索来源。"""
    identity = paper.identity
    names = [item.channel for item in paper.observations]
    if any((identity.s2_paper_id, identity.arxiv_id, identity.doi, identity.pmid)):
        names.append("semantic_scholar")
    if identity.openalex_id or identity.doi:
        names.append("openalex")
    if identity.pmid:
        names.append("pubmed")
    return [
        name
        for name in dict.fromkeys(names)
        if name in context.channels
        and name not in disabled
        and getattr(context.channels[name], "supports_references", True)
    ]


async def _external(
    context: SurveyRuntime,
    factory,
    *,
    reserve_seconds: float = 0,
):
    """把每个网络/模型调用限制在本次 survey 的剩余墙钟预算内。"""
    _check_cancel(context)
    remaining = context.deadline - time.monotonic() - reserve_seconds
    if remaining <= 0:
        raise TimeoutError("survey wall-clock reserve reached")
    async with asyncio.timeout(remaining):
        return await factory()


def _check_cancel(context: SurveyRuntime) -> None:
    if context.cancel.is_set():
        raise asyncio.CancelledError
