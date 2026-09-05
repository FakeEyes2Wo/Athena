"""Literature survey lifecycle and corpus access for ``ResearchRuntime``."""

import asyncio
import logging
import traceback
from typing import Any

from athena.core.agent.chat import single_turn_chat
from athena.core.tool import ToolRegistry
from athena.research.literature.paper_rag.models import PaperSummary
from athena.research.literature.paper_rag.search import (
    RetrievalSession,
    corpus_overview,
)
from athena.research.literature.paper_rag.search import (
    corpus_paper_ids as paper_corpus_paper_ids,
)
from athena.research.literature.paper_rag.tool import MAX_OVERVIEW_PAPERS
from athena.research.literature.survey import (
    SurveyRequest,
    SurveyStack,
    build_survey_stack,
    build_survey_tools,
)
from athena.research.literature.survey import run_survey as run_survey_pipeline

logger = logging.getLogger(__name__)

SURVEY_PLAN_LABEL = "survey"
SURVEY_QUERY_PROMPT = (
    "Turn the following machine-learning research task into one English literature "
    "search topic for an academic paper search engine. Name the problem type, data "
    "modality and the methods worth surveying. Drop dataset paths, column names, "
    "file names and metric values. Answer with the topic sentence only, no preamble "
    "and no quotes."
)


def start_survey(runtime: Any) -> None:
    """Start the optional background survey once."""
    survey = runtime.session.survey
    if (
        not runtime.config.research.survey.enabled
        or survey.task is not None
        or runtime.state.corpus_ref is not None
    ):
        return
    survey.task = asyncio.create_task(run_survey(runtime))


async def _survey_topic(runtime: Any) -> str:
    configured = runtime.config.research.survey.query.strip()
    if configured:
        return configured
    task = runtime.task_text.strip()
    if not task or runtime.model is None:
        return task
    try:
        topic = await single_turn_chat(
            task,
            model=runtime.model,
            client=runtime.client,
            system_prompt=SURVEY_QUERY_PROMPT,
        )
    except Exception:
        logger.warning("survey topic rewrite failed; using the raw task", exc_info=True)
        return task
    return topic.strip() or task


def _survey_stack(runtime: Any) -> SurveyStack:
    survey = runtime.session.survey
    if survey.stack is None:
        survey.stack = build_survey_stack(
            artifacts=runtime.store,
            client=runtime.client,
        )
    return survey.stack


async def _publish_survey_event(
    runtime: Any,
    kind: str,
    _ref: str,
    data: dict[str, Any] | None = None,
) -> None:
    payload = data or {}
    messages = {
        "paper_scout/step": (
            f"scout step {payload.get('step', '?')}: pool {payload.get('pool', 0)}"
        ),
        "survey/scouted": (
            f"retrieved {payload.get('retained', 0)} of {payload.get('pool', 0)}"
        ),
        "survey/fetched": (
            f"fetched {payload.get('fetched', 0)} of "
            f"{payload.get('attempted', 0)} attempted"
        ),
        "survey/converted": f"converted {payload.get('converted', 0)} papers",
        "survey/indexed": f"indexed {payload.get('indexed', 0)} papers",
    }
    text = messages.get(kind)
    if text is None:
        return
    await runtime.publish_output(
        source="tool",
        channel="text",
        text=text,
        plan=SURVEY_PLAN_LABEL,
        tool="paper_survey",
    )


async def run_survey(runtime: Any) -> None:
    """Build a corpus without allowing an optional survey to stop SEARCH."""
    try:
        topic = await _survey_topic(runtime)
        await runtime.publish_output(
            source="tool",
            channel="text",
            text=f"literature survey started: {topic}",
            plan=SURVEY_PLAN_LABEL,
            tool="paper_survey",
        )
        report = await run_survey_pipeline(
            _survey_stack(runtime),
            SurveyRequest(
                query=topic,
                max_papers=runtime.config.research.survey.max_papers,
            ),
            emit=lambda kind, ref, data=None: _publish_survey_event(
                runtime, kind, ref, data
            ),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - survey failure must not stop SEARCH
        await runtime.publish_output(
            source="tool",
            channel="error",
            text=f"literature survey failed: {exc}\n\n{traceback.format_exc()}",
            plan=SURVEY_PLAN_LABEL,
            tool="paper_survey",
        )
        return

    if report.corpus_ref is None:
        await runtime.publish_output(
            source="tool",
            channel="error",
            text=(
                f"literature survey built no corpus (status={report.status}); "
                "ideation continues on the dataset alone"
            ),
            plan=SURVEY_PLAN_LABEL,
            tool="paper_survey",
        )
        return
    runtime.state.corpus_ref = report.corpus_ref
    runtime.state.save(runtime.state_path)
    await runtime.publish_output(
        source="tool",
        channel="text",
        text=(
            f"literature corpus ready: {report.converted()} papers "
            f"({report.timings.total_seconds:.0f}s)"
        ),
        plan=SURVEY_PLAN_LABEL,
        tool="paper_survey",
    )


def corpus_tools(runtime: Any, *, for_ideation: bool = False) -> ToolRegistry | None:
    """Return read-only paper operators for the current corpus, if any."""
    if runtime.state.corpus_ref is None:
        return None
    stack = _survey_stack(runtime)
    session = RetrievalSession(stack.corpus_cache)
    if for_ideation:
        # Each Ideator gets its own read ledger, but the runtime owns the list
        # so citation verification can ask "who actually opened what this round".
        runtime.session.survey.corpus_sessions.append(session)
    return build_survey_tools(
        stack,
        include_survey=False,
        include_producers=False,
        session=session,
    )


def start_corpus_round(runtime: Any) -> None:
    """Drop the previous ideation round's read ledger."""
    runtime.session.survey.corpus_sessions.clear()


def corpus_papers_read(runtime: Any) -> set[str]:
    """Paper ids actually opened during this ideation round."""
    opened: set[str] = set()
    for session in runtime.session.survey.corpus_sessions:
        opened |= session.read_papers()
    return opened


async def corpus_passages_read(runtime: Any) -> dict[str, list[str]]:
    """Passages actually read this round, grouped by paper id."""
    corpus_ref = runtime.state.corpus_ref
    if corpus_ref is None:
        return {}
    chunk_ids: set[str] = set()
    for session in runtime.session.survey.corpus_sessions:
        chunk_ids |= session.read_chunk_ids()
    if not chunk_ids:
        return {}
    stack = _survey_stack(runtime)
    corpus = await stack.corpus_cache.load(runtime.store, corpus_ref)
    passages: dict[str, list[str]] = {}
    for chunk_id in sorted(chunk_ids):
        position = corpus.positions.get(chunk_id)
        if position is None:
            continue
        entry = corpus.index.entries[position]
        passages.setdefault(entry.paper_id, []).append(entry.text)
    return passages


async def corpus_summaries(runtime: Any) -> list[PaperSummary]:
    """One-line summaries for every paper in the active corpus."""
    corpus_ref = runtime.state.corpus_ref
    if corpus_ref is None:
        return []
    stack = _survey_stack(runtime)
    corpus = await stack.corpus_cache.load(runtime.store, corpus_ref)
    return corpus_overview(corpus, [], MAX_OVERVIEW_PAPERS).summaries


async def corpus_paper_ids(runtime: Any) -> set[str]:
    """Paper ids that actually exist in the active corpus."""
    corpus_ref = runtime.state.corpus_ref
    if corpus_ref is None:
        return set()
    stack = _survey_stack(runtime)
    corpus = await stack.corpus_cache.load(runtime.store, corpus_ref)
    return paper_corpus_paper_ids(corpus)
