"""Background literature-survey helpers for ``ResearchRuntime``.

The survey is an optional boost for ideation: it runs in parallel with PREPARE,
must never block SEARCH, and must never take the whole research loop down.
Keeping these helpers separate lets the composition root stay small while the
failure semantics stay in one place.
"""

import asyncio
import logging
from typing import Any

from athena.research.survey import (
    SurveyRequest,
    SurveyStack,
    build_survey_stack,
    run_survey as run_survey_pipeline,
)
from athena.utils.single_turn_chat import single_turn_chat

logger = logging.getLogger(__name__)

SURVEY_PLAN_LABEL = "survey"
# 由研究任务提炼文献检索式的提示。任务原文不能直接当检索式：它带着数据集路径、
# 目标列名这些只对本机有意义的行，而 PaperScout 会把整段原样交给相关性打分模型。
SURVEY_QUERY_PROMPT = (
    "Turn the following machine-learning research task into one English literature "
    "search topic for an academic paper search engine. Name the problem type, data "
    "modality and the methods worth surveying. Drop dataset paths, column names, "
    "file names and metric values. Answer with the topic sentence only, no preamble "
    "and no quotes."
)


def start_survey(runtime: Any) -> None:
    """Start the background survey once, unless a corpus already exists."""
    if not runtime._survey_enabled or runtime._survey_task is not None:
        return
    if runtime.state.corpus_ref is not None:
        return
    runtime._survey_task = asyncio.create_task(runtime._run_survey())


async def survey_topic(runtime: Any) -> str:
    """Choose the survey query: explicit setting first, task-derived otherwise."""
    if runtime._survey_query.strip():
        return runtime._survey_query.strip()
    task = runtime._task_text.strip()
    if not task or runtime._model is None:
        return task
    try:
        topic = await single_turn_chat(
            task,
            model=runtime._model,
            client=runtime._client,
            system_prompt=SURVEY_QUERY_PROMPT,
        )
    except Exception:
        logger.warning("survey topic rewrite failed; using the raw task", exc_info=True)
        return task
    return topic.strip() or task


async def run_survey(runtime: Any) -> None:
    """Run one full survey and record the resulting corpus reference.

    Any failure is published as an error and swallowed: literature is an
    optional gain for ideation, not a prerequisite for the research loop.
    """
    try:
        topic = await survey_topic(runtime)
        stack = ensure_survey_stack(runtime)
        await runtime.publish_output(
            source="tool",
            channel="text",
            text=f"literature survey started: {topic}",
            plan=SURVEY_PLAN_LABEL,
            tool="paper_survey",
        )
        report = await run_survey_pipeline(
            stack,
            SurveyRequest(query=topic, max_papers=runtime._survey_max_papers),
            emit=lambda kind, ref, data=None: project_survey_event(
                runtime, kind, ref, data
            ),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await runtime.publish_output(
            source="tool",
            channel="error",
            text=f"literature survey failed: {exc}",
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
    runtime.state.save(runtime._state_path)
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


def ensure_survey_stack(runtime: Any) -> SurveyStack:
    """Build and cache the survey dependency stack on this runtime's store."""
    if runtime._survey_stack is None:
        runtime._survey_stack = build_survey_stack(
            artifacts=runtime._store, client=runtime._client
        )
    return runtime._survey_stack


async def project_survey_event(
    runtime: Any, kind: str, _ref: str, data: dict[str, Any] | None = None
) -> None:
    """Project a survey progress event into a human-readable output line."""
    payload = data or {}
    if kind == "paper_scout/step":
        text = (
            f"scout step {payload.get('step', '?')}: " f"pool {payload.get('pool', 0)}"
        )
    elif kind == "survey/scouted":
        text = f"retrieved {payload.get('retained', 0)} of {payload.get('pool', 0)}"
    elif kind == "survey/fetched":
        text = (
            f"fetched {payload.get('fetched', 0)} of "
            f"{payload.get('attempted', 0)} attempted"
        )
    elif kind == "survey/converted":
        text = f"converted {payload.get('converted', 0)} papers"
    elif kind == "survey/indexed":
        text = f"indexed {payload.get('indexed', 0)} papers"
    else:
        return
    await runtime.publish_output(
        source="tool",
        channel="text",
        text=text,
        plan=SURVEY_PLAN_LABEL,
        tool="paper_survey",
    )
