"""Focused tests for the background literature survey and its handoff to Ideators.

调研是可选增益：默认不跑、失败不中断、没建好也绝不让 SEARCH 停等。这三条构成本文件
的主要断言面，因为它们决定的是"接进 loop 之后 loop 还是不是原来那个 loop"。第四条是
引用可核验——``ranker.rubric_prior`` 给"有引用"加分，不校验就是在奖励幻觉。
"""

import asyncio
import tempfile
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.core.research_models import Hypothesis, HypothesisBatch
from athena.core.tool import ToolRegistry, tool
from athena.research import runtime as runtime_module
from athena.research.agent_turn_runner import AgentTurnRunner
from athena.research.paper_source.http import HostRateLimiter
from athena.research.runtime import ResearchRuntime
from athena.research.supervisor.state import ResearchState
from athena.research.survey import SurveyReport, SurveyStack

READ_ONLY_TOOLS = {
    "paper_chunk_read",
    "paper_cites",
    "paper_corpus_overview",
    "paper_keyword_search",
    "paper_section_search",
    "paper_visual_of",
}
PRODUCER_TOOLS = {"paper_survey", "paper_fetch", "paper_markdown"}


def _state(**overrides) -> ResearchState:
    payload = {
        "status": "RUNNING",
        "phase": "PREPARE",
        "search_limit": 10,
        "concurrency": 4,
    }
    payload.update(overrides)
    return ResearchState(**payload)


def _stack() -> SurveyStack:
    return SurveyStack(
        artifacts=LocalArtifactStore(tempfile.mkdtemp(prefix="survey_tools_")),
        client=object(),
        model="m",
        http=HostRateLimiter(),
    )


def _runtime(state: ResearchState, **attributes) -> ResearchRuntime:
    """Build a runtime shell carrying only what the survey paths touch."""
    runtime = ResearchRuntime.__new__(ResearchRuntime)
    runtime._state = state
    runtime._supervisor = SimpleNamespace(state=state, evaluator_ref=None)
    runtime._survey_enabled = False
    runtime._survey_query = ""
    runtime._survey_max_papers = 10
    runtime._survey_stack = None
    runtime._survey_task = None
    runtime._model = "m"
    runtime._client = None
    runtime._ideation = "ideageneration"
    runtime._store = LocalArtifactStore(tempfile.mkdtemp(prefix="survey_store_"))
    runtime._events_bus = SimpleNamespace(
        project_agent_event=_discard_event, publish_output=_discard_event
    )
    for name, value in attributes.items():
        setattr(runtime, name, value)
    return runtime


async def _discard_event(*_args, **_kwargs) -> None:
    """Drop projected agent events; lane tests only inspect the dispatch request."""


@tool
async def _kaggle_stub(slug: str) -> dict:
    """Stand-in for whatever Kaggle contributes to the Ideator's tool set."""
    return {"slug": slug}


def _recorder(sink: list[dict]):
    async def publish_output(**payload) -> None:
        sink.append(payload)

    return publish_output


# ── 生命周期 ────────────────────────────────────────────────────────────


def test_survey_is_off_by_default_so_no_run_pays_for_it_unasked() -> None:
    runtime = _runtime(_state())

    runtime._start_survey()

    assert runtime._survey_task is None


@pytest.mark.asyncio
async def test_enabling_the_survey_starts_exactly_one_background_run() -> None:
    runtime = _runtime(_state(), _survey_enabled=True)
    runs = 0

    async def fake_run(self) -> None:
        nonlocal runs
        runs += 1

    runtime._run_survey = MethodType(fake_run, runtime)

    runtime._start_survey()
    runtime._start_survey()
    await runtime._survey_task

    assert runs == 1


def test_an_existing_corpus_is_never_paid_for_twice() -> None:
    runtime = _runtime(_state(corpus_ref="sha256:cached"), _survey_enabled=True)

    runtime._start_survey()

    assert runtime._survey_task is None


@pytest.mark.asyncio
async def test_the_corpus_ref_lands_on_the_state_the_supervisor_will_persist(
    tmp_path, monkeypatch
) -> None:
    """recover() 会用 model_copy 换掉状态对象，写在旧对象上的字段会被覆盖掉。"""
    stale = _state()
    live = stale.model_copy()
    runtime = _runtime(stale, _survey_query="tabular deep learning")
    runtime._supervisor = SimpleNamespace(state=live)
    runtime._state_path = tmp_path / "state.json"
    runtime._survey_stack = _stack()
    runtime.publish_output = _recorder([])

    async def fake_survey(_stack, _request, *, emit=None):
        return SurveyReport(query="q", status="complete", corpus_ref="sha256:corpus")

    monkeypatch.setattr(runtime_module, "run_survey", fake_survey)

    await runtime._run_survey()

    assert live.corpus_ref == "sha256:corpus"
    assert stale.corpus_ref is None
    assert ResearchState.load(runtime._state_path).corpus_ref == "sha256:corpus"


@pytest.mark.asyncio
async def test_a_failed_survey_is_reported_and_leaves_search_untouched(
    monkeypatch,
) -> None:
    published: list[dict] = []
    runtime = _runtime(_state(), _survey_query="topic", _survey_stack=_stack())
    runtime.publish_output = _recorder(published)

    async def fake_survey(_stack, _request, *, emit=None):
        raise RuntimeError("upstream is down")

    monkeypatch.setattr(runtime_module, "run_survey", fake_survey)

    await runtime._run_survey()

    assert runtime.state.corpus_ref is None
    assert [item["channel"] for item in published] == ["text", "error"]
    assert "upstream is down" in published[-1]["text"]


@pytest.mark.asyncio
async def test_an_empty_corpus_is_reported_rather_than_recorded(monkeypatch) -> None:
    published: list[dict] = []
    runtime = _runtime(_state(), _survey_query="topic", _survey_stack=_stack())
    runtime.publish_output = _recorder(published)

    async def fake_survey(_stack, _request, *, emit=None):
        return SurveyReport(query="q", status="empty")

    monkeypatch.setattr(runtime_module, "run_survey", fake_survey)

    await runtime._run_survey()

    assert runtime.state.corpus_ref is None
    assert published[-1]["channel"] == "error"


@pytest.mark.asyncio
async def test_aclose_cancels_a_survey_that_is_still_running() -> None:
    runtime = _runtime(_state())
    runtime._events_bus = SimpleNamespace(_subscriber_ready={}, _subscribers={})
    runtime._task = None
    started = asyncio.Event()

    async def never_finishes() -> None:
        started.set()
        await asyncio.Event().wait()

    runtime._survey_task = asyncio.create_task(never_finishes())
    await started.wait()
    stopped = asyncio.Event()

    async def stop() -> None:
        stopped.set()

    runtime._supervisor = SimpleNamespace(state=runtime._state, stop=stop)
    runtime._agents = SimpleNamespace(aclose=stop)

    await runtime.aclose()

    assert runtime._survey_task.cancelled()


# ── 交给 Ideator ────────────────────────────────────────────────────────


def test_the_ideator_gets_read_only_operators_and_no_producers() -> None:
    runtime = _runtime(_state(corpus_ref="sha256:corpus"), _survey_stack=_stack())

    tools = runtime.corpus_tools()

    names = {spec.name for spec in tools.specs}
    assert READ_ONLY_TOOLS <= names
    assert not (PRODUCER_TOOLS & names)


def test_no_paper_tools_are_offered_before_a_corpus_exists() -> None:
    runtime = _runtime(_state(), _survey_stack=_stack())

    assert runtime.corpus_tools() is None
    assert runtime.survey_corpus_ref() is None


def test_the_ideator_tool_set_merges_kaggle_and_corpus_lazily() -> None:
    """两者都会随时间出现或消失，因此合并必须在每次创建实例时重算。

    ideator 只注册一次，而语料要十几分钟才建好——把注册时刻的工具表冻结下来，语料就
    永远接不进来。
    """
    runtime = _runtime(_state(corpus_ref="sha256:corpus"), _survey_stack=_stack())
    kaggle = ToolRegistry()
    kaggle.register(_kaggle_stub)
    runtime.kaggle_tools = lambda _agent_type: kaggle
    runner = AgentTurnRunner(runtime)

    both = {spec.name for spec in runner._ideator_tools().specs}
    assert READ_ONLY_TOOLS <= both
    assert "_kaggle_stub" in both

    runtime._supervisor.state.corpus_ref = None
    kaggle_only = {spec.name for spec in runner._ideator_tools().specs}
    assert kaggle_only == {"_kaggle_stub"}


@pytest.mark.asyncio
async def test_ideation_runs_without_waiting_when_the_corpus_is_not_ready(
    monkeypatch,
) -> None:
    requests = _lane_harness(monkeypatch)
    runtime = _runtime(_state())
    runtime._agents = _AgentSpy(requests)

    await AgentTurnRunner(runtime)._run_ideator_lane("ideator-1", 2, _eda_dir())

    assert runtime.survey_corpus_ref() is None
    assert "corpus_ref" not in requests[0]["content"]


@pytest.mark.asyncio
async def test_a_ready_corpus_reaches_every_lane_with_a_citation_instruction(
    monkeypatch,
) -> None:
    requests = _lane_harness(monkeypatch)
    runtime = _runtime(_state(corpus_ref="sha256:corpus"))
    runtime._agents = _AgentSpy(requests)

    await AgentTurnRunner(runtime)._run_ideator_lane("ideator-1", 2, _eda_dir())

    content = requests[0]["content"]
    assert "sha256:corpus" in content
    assert "paper_corpus_overview" in content
    assert "sources" in content


# ── 引用可核验 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_citation_naming_no_corpus_paper_is_dropped_before_ranking() -> None:
    """ranker 给"有引用"加分；不校验的话编一个 id 就能让候选往前排。"""
    published: list[dict] = []
    runtime = _runtime(_state(corpus_ref="sha256:corpus"))
    runtime.publish_output = _recorder(published)

    async def known() -> set[str]:
        return {"arxiv:1706.03762"}

    runtime.corpus_paper_ids = known
    proposed = Hypothesis(
        statement="s",
        intervention="i",
        expected_effect="e",
        sources=["arxiv:1706.03762", "arxiv:9999.99999"],
    )

    kept = await AgentTurnRunner(runtime)._verify_sources([proposed])

    assert kept[0].sources == ["arxiv:1706.03762"]
    assert published[-1]["channel"] == "error"
    assert "1 citation" in published[-1]["text"]


@pytest.mark.asyncio
async def test_sources_are_left_alone_when_no_corpus_was_offered() -> None:
    """没有语料就没有比对基准；清空只会误伤别的来源写进去的内容。"""
    runtime = _runtime(_state())
    proposed = Hypothesis(
        statement="s", intervention="i", expected_effect="e", sources=["manual-note"]
    )

    kept = await AgentTurnRunner(runtime)._verify_sources([proposed])

    assert kept[0].sources == ["manual-note"]


@pytest.mark.asyncio
async def test_the_baseline_arm_verifies_citations_too(monkeypatch) -> None:
    """消融对照组不过门禁，但引用照样要能核验——排序对两条臂是同一个。"""
    runtime = _runtime(_state(corpus_ref="sha256:corpus"), _ideation="baseline")
    runtime.publish_output = _recorder([])

    async def known() -> set[str]:
        return {"arxiv:1706.03762"}

    runtime.corpus_paper_ids = known
    batch = HypothesisBatch(
        hypotheses=[
            Hypothesis(
                statement="s",
                intervention="i",
                expected_effect="e",
                sources=["invented"],
            )
        ]
    )

    kept = await AgentTurnRunner(runtime)._finish_ideator_batch(batch)

    assert kept[0].sources == []


# ── 测试脚手架 ──────────────────────────────────────────────────────────


def _eda_dir() -> Path:
    """Throwaway EDA directory path for lane request construction."""
    return Path(tempfile.mkdtemp(prefix="eda_"))


def _lane_harness(monkeypatch) -> list[dict]:
    """Short out the agent kernel so a lane run only exercises request assembly."""

    async def wait(_agents, _run_id, _publish):
        return SimpleNamespace(error=None)

    async def load(_summary, _store, _schema):
        return SimpleNamespace(hypotheses=[])

    monkeypatch.setattr(runtime_module, "run_survey", _unused_survey)
    monkeypatch.setattr("athena.research.agent_turn_runner.wait_run_events", wait)
    monkeypatch.setattr("athena.research.agent_turn_runner.load_agent_result", load)
    return []


async def _unused_survey(*_args, **_kwargs):
    raise AssertionError("lane tests must never trigger a survey run")


class _AgentSpy:
    """Capture the request each Ideator lane is dispatched with."""

    def __init__(self, sink: list[dict]) -> None:
        self._sink = sink

    async def create_root(self, agent_type: str, request: dict, *, name: str):
        assert agent_type == "ideator"
        self._sink.append(request)
        return name, f"run-{name}"
