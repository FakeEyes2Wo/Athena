import asyncio
import json
from collections.abc import Sequence
from dataclasses import asdict

import pytest
from pydantic import ValidationError
from pydantic_ai.messages import ModelMessage, UserPromptPart

from athena.app_server.thread_manager import RuntimeThreadManager
from athena.core.research_models import Hypothesis
from athena.core.research_tree import ResearchTree
from athena.core.thread_models import AthenaTurn
from athena.data.types import ColumnSummary, DataProfile
from athena.ideator import DebateResult, Ideator, IdeatorConfig
from athena.ideator import __all__ as ideator_exports
from athena.ideator.ideator import _DebateRunner
from athena.ideator.types import (
    _JudgeOutput,
    _ProposalBatch,
    _ReviewBatch,
    _RevisionBatch,
)
from athena.retrieval.types import HFModelRef, PaperRef
from athena.storage import LocalArtifactStore


def _hypothesis_payloads(count: int) -> list[dict[str, str]]:
    return [
        {
            "statement": f"Claim {index}",
            "intervention": f"Change {index}",
            "expected_effect": f"Effect {index}",
        }
        for index in range(count)
    ]


def test_ideator_public_exports_are_exact() -> None:
    assert ideator_exports == ["Ideator", "IdeatorConfig", "DebateResult"]


def test_ideator_config_uses_specified_defaults() -> None:
    assert IdeatorConfig() == IdeatorConfig(
        debater_count=3,
        quorum=2,
        stage_timeout_seconds=120.0,
        max_hypotheses=5,
    )


@pytest.mark.parametrize(
    "values",
    [
        {"debater_count": 1},
        {"debater_count": 3, "quorum": 4},
        {"stage_timeout_seconds": 0},
        {"max_hypotheses": 2},
        {"max_hypotheses": 6},
    ],
)
def test_ideator_config_rejects_invalid_values(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        IdeatorConfig(**values)


def test_ideator_config_rejects_quorum_above_debater_count() -> None:
    with pytest.raises(ValidationError, match="quorum cannot exceed debater count"):
        IdeatorConfig(debater_count=3, quorum=4)


def test_ideator_config_is_frozen() -> None:
    config = IdeatorConfig()

    with pytest.raises(ValidationError):
        config.quorum = 3


def test_ideator_preserves_its_collaborators_and_default_config() -> None:
    factory = object()
    artifacts = object()

    ideator = Ideator(agent_factory=factory, artifacts=artifacts)

    assert ideator._agent_factory is factory
    assert ideator._artifacts is artifacts
    assert ideator._config == IdeatorConfig()


def test_debate_result_requires_an_artifact_reference() -> None:
    with pytest.raises(ValidationError, match="artifact_ref"):
        DebateResult(hypotheses=_hypothesis_payloads(3))


@pytest.mark.parametrize("hypothesis_count", [3, 5])
def test_debate_result_accepts_three_to_five_hypotheses(
    hypothesis_count: int,
) -> None:
    result = DebateResult(
        hypotheses=_hypothesis_payloads(hypothesis_count),
        artifact_ref="debate-result.json",
    )

    assert len(result.hypotheses) == hypothesis_count


@pytest.mark.parametrize("hypothesis_count", [2, 6])
def test_debate_result_rejects_hypothesis_counts_outside_three_to_five(
    hypothesis_count: int,
) -> None:
    with pytest.raises(ValidationError, match="hypotheses"):
        DebateResult(
            hypotheses=_hypothesis_payloads(hypothesis_count),
            artifact_ref="debate-result.json",
        )


def test_debate_result_uses_independent_default_collections() -> None:
    first = DebateResult(
        hypotheses=_hypothesis_payloads(3), artifact_ref="first-result.json"
    )
    second = DebateResult(
        hypotheses=_hypothesis_payloads(3), artifact_ref="second-result.json"
    )

    first.transcript.append({"stage": "proposal"})
    first.failures.append({"stage": "proposal", "message": "timed out"})

    assert second.transcript == []
    assert second.failures == []


def test_debate_result_validates_nested_hypotheses() -> None:
    hypotheses = _hypothesis_payloads(3)
    hypotheses[0]["statement"] = " "

    with pytest.raises(ValidationError, match="hypotheses.0.statement"):
        DebateResult(hypotheses=hypotheses, artifact_ref="debate-result.json")

    result = DebateResult(
        hypotheses=_hypothesis_payloads(3), artifact_ref="debate-result.json"
    )

    assert isinstance(result.hypotheses[0], Hypothesis)


class RecordingAgent:
    def __init__(self, role: str, agent_index: int) -> None:
        self.role = role
        self.agent_index = agent_index
        self.calls: list[tuple[str, type[object]]] = []
        self.message_histories: list[list[ModelMessage]] = []

    async def run(
        self,
        prompt: str,
        *,
        output_type: type[object],
        message_history: Sequence[ModelMessage] | None = None,
    ) -> object:
        self.calls.append((prompt, output_type))
        self.message_histories.append(list(message_history or []))
        if output_type is _ProposalBatch:
            proposal = _ProposalBatch(
                hypotheses=[
                    {
                        "statement": "Measure attendance",
                        "intervention": "Send reminders",
                        "expected_effect": "Attendance increases",
                        "sources": ["study-1"],
                    },
                    {
                        "statement": "Reduce queue time",
                        "intervention": "Add a kiosk",
                        "expected_effect": "Wait time falls",
                        "sources": ["study-2"],
                    },
                    {
                        "statement": "Improve retention",
                        "intervention": "Offer follow-ups",
                        "expected_effect": "Returns increase",
                        "sources": ["study-3"],
                    },
                ]
            )
            if self.agent_index == 1:
                return type("StructuredResult", (), {"output": proposal})()
            return proposal
        if output_type is _ReviewBatch:
            return _ReviewBatch(
                critiques=[
                    {
                        "key": "proposal-1",
                        "concerns": ["Needs a control group"],
                        "recommendation": "revise",
                    }
                ]
            )
        if output_type is _RevisionBatch:
            return _RevisionBatch(
                hypotheses=[
                    {
                        "key": f"idea-0-{ordinal}",
                        "statement": f"Revised claim {ordinal}",
                        "intervention": f"Revised intervention {ordinal}",
                        "expected_effect": f"Revised effect {ordinal}",
                        "sources": [f"study-{ordinal}"],
                    }
                    for ordinal in range(1, 4)
                ]
            )
        raise AssertionError(f"unexpected output schema: {output_type}")


class RecordingAgentFactory:
    def __init__(self) -> None:
        self.creation_order: list[tuple[str, int]] = []
        self.agents: list[RecordingAgent] = []

    def __call__(self, role: str, agent_index: int) -> RecordingAgent:
        self.creation_order.append((role, agent_index))
        agent = RecordingAgent(role, agent_index)
        self.agents.append(agent)
        return agent


@pytest.mark.asyncio
async def test_debate_runner_reuses_agents_per_thread_and_round_trips_results(
    tmp_path,
) -> None:
    factory = RecordingAgentFactory()
    runner = _DebateRunner(
        agent_factory=factory, artifacts=LocalArtifactStore(tmp_path)
    )
    manager = RuntimeThreadManager(runner)
    first_thread = await manager.start("debate-session", "context:first")
    second_thread = await manager.start("debate-session", "context:second")

    first_proposal_request = await runner.put_request(
        "proposal", "debater", 0, "Propose three hypotheses"
    )
    first_proposal_turn = await manager.submit(
        first_thread.thread_id, first_proposal_request
    )
    first_proposal_ref = await manager.wait_turn(
        first_thread.thread_id, first_proposal_turn.turn_id
    )
    first_proposal = await runner.read_result("proposal", first_proposal_ref)

    first_review_request = await runner.put_request(
        "review", "debater", 0, "Review the hypotheses"
    )
    first_review_turn = await manager.submit(
        first_thread.thread_id, first_review_request
    )
    first_review_ref = await manager.wait_turn(
        first_thread.thread_id, first_review_turn.turn_id
    )
    first_review = await runner.read_result("review", first_review_ref)

    second_proposal_request = await runner.put_request(
        "proposal", "debater", 1, "Propose independent hypotheses"
    )
    second_proposal_turn = await manager.submit(
        second_thread.thread_id, second_proposal_request
    )
    second_proposal_ref = await manager.wait_turn(
        second_thread.thread_id, second_proposal_turn.turn_id
    )
    second_proposal = await runner.read_result("proposal", second_proposal_ref)

    first_revision_request = await runner.put_request(
        "revision", "debater", 0, "Revise the hypotheses"
    )
    first_revision_turn = await manager.submit(
        first_thread.thread_id, first_revision_request
    )
    first_revision_ref = await manager.wait_turn(
        first_thread.thread_id, first_revision_turn.turn_id
    )
    first_revision = await runner.read_result("revision", first_revision_ref)

    assert isinstance(first_proposal, _ProposalBatch)
    assert isinstance(first_review, _ReviewBatch)
    assert isinstance(second_proposal, _ProposalBatch)
    assert isinstance(first_revision, _RevisionBatch)
    assert first_proposal.hypotheses[0].statement == "Measure attendance"
    assert first_review.critiques[0].recommendation == "revise"
    assert factory.creation_order == [("debater", 0), ("debater", 1)]
    assert factory.agents[0].calls == [
        ("Propose three hypotheses", _ProposalBatch),
        ("Review the hypotheses", _ReviewBatch),
        ("Revise the hypotheses", _RevisionBatch),
    ]
    assert factory.agents[1].calls == [
        ("Propose independent hypotheses", _ProposalBatch)
    ]
    assert [len(history) for history in factory.agents[0].message_histories] == [
        0,
        2,
        4,
    ]
    assert [len(history) for history in factory.agents[1].message_histories] == [0]
    revision_history_prompts = [
        part.content
        for message in factory.agents[0].message_histories[2]
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ]
    assert revision_history_prompts == [
        "Propose three hypotheses",
        "Review the hypotheses",
    ]
    assert "Propose independent hypotheses" not in revision_history_prompts

    changed_binding_request = await runner.put_request(
        "proposal", "debater", 1, "Change the binding"
    )
    changed_binding_turn = AthenaTurn(
        turn_id="changed-binding",
        thread_id=first_thread.thread_id,
        request_ref=changed_binding_request,
        status="running",
    )

    async def emit(kind: str, event_ref: str) -> None:
        raise AssertionError(f"unexpected event: {kind} {event_ref}")

    with pytest.raises(RuntimeError, match="thread agent binding changed"):
        await runner(first_thread, changed_binding_turn, emit)

    await manager.aclose()


class ProposalBarrier:
    def __init__(self, expected: int, *, blocked: bool = False) -> None:
        self.expected = expected
        self.entered = 0
        self.active = 0
        self.max_active = 0
        self.all_entered = asyncio.Event()
        self.all_cancelled = asyncio.Event()
        self.cancelled = 0
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()

    async def wait(self) -> None:
        self.entered += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.entered == self.expected:
            self.all_entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            if self.cancelled == self.expected:
                self.all_cancelled.set()
            raise
        finally:
            self.active -= 1


class StageBlock:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()

    async def wait(self) -> None:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class DebateAgent:
    def __init__(
        self,
        role: str,
        agent_index: int,
        *,
        debater_count: int,
        barrier: ProposalBarrier,
        duplicate_revision_keys: bool,
        invalid_review_agent: int | None,
        invalid_review_mode: str | None,
        invalid_judge_mode: str | None,
        stage_failures: dict[tuple[str, int, str], BaseException],
        stage_blocks: dict[tuple[str, int, str], StageBlock],
        transient_judge_failures: int = 0,
    ) -> None:
        self.role = role
        self.agent_index = agent_index
        self.debater_count = debater_count
        self.barrier = barrier
        self.duplicate_revision_keys = duplicate_revision_keys
        self.invalid_review_agent = invalid_review_agent
        self.invalid_review_mode = invalid_review_mode
        self.invalid_judge_mode = invalid_judge_mode
        self.stage_failures = stage_failures
        self.stage_blocks = stage_blocks
        self.transient_judge_failures = transient_judge_failures
        self.calls: list[tuple[str, type[object]]] = []

    @staticmethod
    def _draft(agent_index: int, ordinal: int) -> dict[str, object]:
        topics = (
            ("attendance", "queue time", "retention"),
            ("calibration", "minority recall", "precision"),
            ("latency", "throughput", "memory use"),
        )
        topic = topics[agent_index][ordinal - 1]
        return {
            "statement": f"Measure {topic}",
            "intervention": f"Apply the {topic} intervention",
            "expected_effect": f"The {topic} metric improves",
            "sources": [f"source-{topic.replace(' ', '-')}"],
        }

    @staticmethod
    def _payload(prompt: str, heading: str) -> object:
        return json.loads(prompt.split(f"{heading}:\n\n", 1)[1])

    async def run(
        self,
        prompt: str,
        *,
        output_type: type[object],
        message_history: Sequence[ModelMessage] | None = None,
    ) -> object:
        del message_history
        self.calls.append((prompt, output_type))
        stage = {
            _ProposalBatch: "proposal",
            _ReviewBatch: "review",
            _RevisionBatch: "revision",
            _JudgeOutput: "judge",
        }[output_type]
        key = (self.role, self.agent_index, stage)
        if key in self.stage_failures:
            raise self.stage_failures[key]
        if stage == "judge" and self.transient_judge_failures > 0:
            self.transient_judge_failures -= 1
            raise LookupError("judge unavailable")
        if output_type is _ProposalBatch:
            await self.barrier.wait()
        if key in self.stage_blocks:
            await self.stage_blocks[key].wait()
        if output_type is _ProposalBatch:
            return _ProposalBatch(
                hypotheses=[
                    self._draft(self.agent_index, ordinal) for ordinal in range(1, 4)
                ]
            )
        if output_type is _ReviewBatch:
            candidates = self._payload(prompt, "Anonymous candidates")
            keys = [candidate["key"] for candidate in candidates]
            if self.agent_index == self.invalid_review_agent:
                if self.invalid_review_mode == "invented":
                    keys[0] = "candidate-not-offered"
                elif self.invalid_review_mode == "missing":
                    keys.pop()
                elif self.invalid_review_mode == "duplicate":
                    keys.append(keys[0])
            return _ReviewBatch(
                critiques=[
                    {
                        "key": key,
                        "concerns": ["Clarify the comparison baseline"],
                        "recommendation": "revise",
                    }
                    for key in keys
                ]
            )
        if output_type is _RevisionBatch:
            ordinals = [1, 2, 3]
            if self.duplicate_revision_keys and self.agent_index == 0:
                ordinals.append(3)
            return _RevisionBatch(
                hypotheses=[
                    {
                        "key": f"idea-{self.agent_index}-{ordinal}",
                        **self._draft(self.agent_index, ordinal),
                    }
                    for ordinal in ordinals
                ]
            )
        if output_type is _JudgeOutput:
            debate = self._payload(
                prompt, "Proposals, critiques, revisions, and failures"
            )
            surviving_keys = sorted(
                candidate["key"]
                for candidates in debate["revisions"].values()
                for candidate in candidates
            )
            decisions = [
                {
                    "candidate_keys": [surviving_keys[0]],
                    "disposition": "selected",
                    "reason": "Strongest measurable design",
                },
                {
                    "candidate_keys": surviving_keys[1:3],
                    "disposition": "merged",
                    "reason": "Complementary evidence",
                },
                *[
                    {
                        "candidate_keys": [key],
                        "disposition": "rejected",
                        "reason": "A stronger candidate survived",
                    }
                    for key in surviving_keys[3:]
                ],
            ]
            if self.invalid_judge_mode == "nonexistent":
                decisions[-1]["candidate_keys"] = ["idea-99-1"]
            elif self.invalid_judge_mode == "incomplete":
                decisions.pop()
            elif self.invalid_judge_mode == "duplicate":
                decisions.append(
                    {
                        "candidate_keys": [surviving_keys[0]],
                        "disposition": "rejected",
                        "reason": "Duplicate disposition",
                    }
                )
            elif self.invalid_judge_mode == "incomplete_lineage":
                decisions[2]["disposition"] = "selected"
            hypotheses = [
                {
                    "statement": f"Final falsifiable claim {ordinal}",
                    "intervention": f"Run final intervention {ordinal}",
                    "expected_effect": f"Primary metric increases by {ordinal}%",
                    "sources": [f"final-source-{ordinal}"],
                    "candidate_keys": [surviving_keys[ordinal - 1]],
                }
                for ordinal in range(1, 4)
            ]
            if self.invalid_judge_mode == "unlinked":
                hypotheses[0].pop("candidate_keys")
            elif self.invalid_judge_mode == "unknown_lineage":
                hypotheses[0]["candidate_keys"] = ["idea-99-1"]
            elif self.invalid_judge_mode == "rejected_lineage":
                hypotheses[0]["candidate_keys"] = [surviving_keys[3]]
            elif self.invalid_judge_mode == "duplicate_lineage":
                hypotheses[1]["candidate_keys"] = [surviving_keys[0]]
            return _JudgeOutput(hypotheses=hypotheses, decisions=decisions)
        raise AssertionError(f"unexpected output schema: {output_type}")


class DebateAgentFactory:
    def __init__(
        self,
        debater_count: int,
        barrier: ProposalBarrier,
        *,
        duplicate_revision_keys: bool = False,
        invalid_review_agent: int | None = None,
        invalid_review_mode: str | None = None,
        invalid_judge_mode: str | None = None,
        stage_failures: dict[tuple[str, int, str], BaseException] | None = None,
        stage_blocks: dict[tuple[str, int, str], StageBlock] | None = None,
        transient_judge_failures: int = 0,
    ) -> None:
        self.debater_count = debater_count
        self.barrier = barrier
        self.duplicate_revision_keys = duplicate_revision_keys
        self.invalid_review_agent = invalid_review_agent
        self.invalid_review_mode = invalid_review_mode
        self.invalid_judge_mode = invalid_judge_mode
        self.stage_failures = stage_failures or {}
        self.stage_blocks = stage_blocks or {}
        self.transient_judge_failures = transient_judge_failures
        self.agents: list[DebateAgent] = []

    @property
    def judge_calls(self) -> int:
        return sum(
            output_type is _JudgeOutput
            for agent in self.agents
            for _, output_type in agent.calls
        )

    def __call__(self, role: str, agent_index: int) -> DebateAgent:
        agent = DebateAgent(
            role,
            agent_index,
            debater_count=self.debater_count,
            barrier=self.barrier,
            duplicate_revision_keys=self.duplicate_revision_keys,
            invalid_review_agent=self.invalid_review_agent,
            invalid_review_mode=self.invalid_review_mode,
            invalid_judge_mode=self.invalid_judge_mode,
            stage_failures=self.stage_failures,
            stage_blocks=self.stage_blocks,
            transient_judge_failures=self.transient_judge_failures,
        )
        self.agents.append(agent)
        return agent


class RecordingArtifactStore(LocalArtifactStore):
    def __init__(self, root) -> None:
        super().__init__(root)
        self.texts: list[str] = []

    async def put_text(self, text: str) -> str:
        self.texts.append(text)
        return await super().put_text(text)

    def audit_payloads(self) -> list[dict[str, object]]:
        payloads = []
        for text in self.texts:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and "judge_decisions" in payload:
                payloads.append(payload)
        return payloads


class RecordingRuntimeThreadManager(RuntimeThreadManager):
    instances: list["RecordingRuntimeThreadManager"] = []

    def __init__(self, runner) -> None:
        super().__init__(runner)
        self.submitted: dict[tuple[str, str], dict[str, object]] = {}
        self.interrupts: list[tuple[str, str, str]] = []
        self.aclose_calls: list[str] = []
        self.instances.append(self)

    async def submit(self, thread_id: str, request_ref: str):
        turn = await super().submit(thread_id, request_ref)
        request = json.loads(await self._runner._artifacts.get_text(request_ref))
        self.submitted[(thread_id, turn.turn_id)] = request
        return turn

    async def interrupt(self, thread_id: str, turn_id: str, reason: str) -> None:
        self.interrupts.append((thread_id, turn_id, reason))
        await super().interrupt(thread_id, turn_id, reason)

    async def aclose(self, reason: str = "server_shutdown") -> None:
        self.aclose_calls.append(reason)
        await super().aclose(reason)


class FrozenResearchTree(ResearchTree):
    def __init__(self, snapshot: dict[str, object]) -> None:
        super().__init__()
        self.snapshot = snapshot
        self.to_dict_calls = 0
        self.best_id = "exp-best"

    def to_dict(self) -> dict[str, object]:
        self.to_dict_calls += 1
        return self.snapshot

    def best_experiment_id(self) -> str:
        return self.best_id


def _debate_inputs() -> tuple[
    DataProfile,
    list[PaperRef],
    list[HFModelRef],
    FrozenResearchTree,
    dict[str, object],
]:
    profile = DataProfile(
        row_count=120,
        col_count=2,
        columns=[
            ColumnSummary(
                name="target",
                dtype="int64",
                n_unique=2,
                sample_values=["0", "1"],
            )
        ],
        task_type_hint="classification",
        target_col="target",
        issue_summary="Minority recall is low",
    )
    papers = [
        PaperRef(
            title="Calibrated Classification",
            source="arxiv",
            url="https://example.test/paper",
            key_findings="Calibration improves minority recall",
            markdown_ref="sha256:paper-body-ref",
        )
    ]
    models = [
        HFModelRef(
            repo="example/calibrated-model",
            revision="abc123",
            license="apache-2.0",
            param_count=42_000_000,
            task_match="classification",
            artifact="sha256:model-card-ref",
        )
    ]
    snapshot: dict[str, object] = {
        "version": 2,
        "sota_id": "exp-best",
        "hypotheses": {
            "hyp-existing": {
                "statement": "Existing hypothesis with unicode: 测试",
                "intervention": "Existing intervention",
                "expected_effect": "Existing expected effect",
                "status": "SUPPORTED",
                "sources": ["existing-source"],
            }
        },
        "experiments": {"exp-best": {"artifact_ref": "sha256:experiment-ref"}},
    }
    tree = FrozenResearchTree(snapshot)
    expected_context = {
        "profile": profile.model_dump(mode="json"),
        "papers": [asdict(paper) for paper in papers],
        "models": [asdict(model) for model in models],
        "tree": snapshot,
    }
    return profile, papers, models, tree, expected_context


async def _generate_successfully(
    tmp_path,
    *,
    blocked: bool = False,
    duplicate_revision_keys: bool = False,
    invalid_review_agent: int | None = None,
    invalid_review_mode: str | None = None,
    invalid_judge_mode: str | None = None,
    stage_failures: dict[tuple[str, int, str], BaseException] | None = None,
    stage_blocks: dict[tuple[str, int, str], StageBlock] | None = None,
    quorum: int = 2,
    stage_timeout_seconds: float = 120.0,
    transient_judge_failures: int = 0,
):
    debater_count = 3
    barrier = ProposalBarrier(debater_count, blocked=blocked)
    factory = DebateAgentFactory(
        debater_count,
        barrier,
        duplicate_revision_keys=duplicate_revision_keys,
        invalid_review_agent=invalid_review_agent,
        invalid_review_mode=invalid_review_mode,
        invalid_judge_mode=invalid_judge_mode,
        stage_failures=stage_failures,
        stage_blocks=stage_blocks,
        transient_judge_failures=transient_judge_failures,
    )
    artifacts = RecordingArtifactStore(tmp_path)
    ideator = Ideator(
        agent_factory=factory,
        artifacts=artifacts,
        config=IdeatorConfig(
            debater_count=debater_count,
            quorum=quorum,
            stage_timeout_seconds=stage_timeout_seconds,
            max_hypotheses=4,
        ),
    )
    profile, papers, models, tree, expected_context = _debate_inputs()
    task = asyncio.create_task(ideator.generate(profile, papers, models, tree))
    return task, barrier, factory, artifacts, tree, expected_context


@pytest.mark.asyncio
async def test_generate_runs_proposals_concurrently_and_reuses_thread_agents(
    tmp_path,
) -> None:
    task, barrier, factory, _, tree, _ = await _generate_successfully(
        tmp_path, blocked=True
    )

    await asyncio.wait_for(barrier.all_entered.wait(), timeout=2)
    assert barrier.entered == 3
    assert barrier.max_active == 3
    tree.best_id = "exp-changed-after-snapshot"
    barrier.release.set()
    result = await task

    debaters = [agent for agent in factory.agents if agent.role == "debater"]
    judges = [agent for agent in factory.agents if agent.role == "judge"]
    assert len(debaters) == 3
    assert len(judges) == 1
    assert [output_type for _, output_type in judges[0].calls] == [_JudgeOutput]
    assert all(
        [output_type for _, output_type in agent.calls]
        == [_ProposalBatch, _ReviewBatch, _RevisionBatch]
        for agent in debaters
    )
    assert {entry["stage"] for entry in result.transcript} == {
        "proposal",
        "review",
        "revision",
        "judge",
    }
    assert all(hypothesis.parent_id == "exp-best" for hypothesis in result.hypotheses)


@pytest.mark.asyncio
async def test_generate_sends_identical_full_research_context_to_every_agent(
    tmp_path,
) -> None:
    task, _, factory, _, tree, expected_context = await _generate_successfully(tmp_path)

    await task

    expected_json = json.dumps(expected_context, ensure_ascii=False, sort_keys=True)
    prompts = [prompt for agent in factory.agents for prompt, _ in agent.calls]
    assert len(prompts) == 10
    assert tree.to_dict_calls == 1
    assert all(expected_json in prompt for prompt in prompts)
    assert "sha256:paper-body-ref" in expected_json
    assert "sha256:model-card-ref" in expected_json


@pytest.mark.asyncio
async def test_generate_keeps_peer_review_anonymous_and_excludes_own_candidates(
    tmp_path,
) -> None:
    task, _, factory, _, _, _ = await _generate_successfully(tmp_path)

    await task

    debaters = [agent for agent in factory.agents if agent.role == "debater"]
    aliases_by_statement: dict[str, set[str]] = {}
    for agent in debaters:
        review_prompt = next(
            prompt for prompt, output_type in agent.calls if output_type is _ReviewBatch
        )
        candidates = DebateAgent._payload(review_prompt, "Anonymous candidates")
        assert "author" not in review_prompt.lower()
        assert "agent_index" not in review_prompt
        assert "debater-" not in review_prompt
        assert "idea-" not in review_prompt
        assert [candidate["key"] for candidate in candidates] == [
            f"candidate-{ordinal}" for ordinal in range(1, 7)
        ]
        for ordinal in range(1, 4):
            own_statement = DebateAgent._draft(agent.agent_index, ordinal)["statement"]
            assert own_statement not in review_prompt
        for other_index in range(3):
            if other_index == agent.agent_index:
                continue
            for ordinal in range(1, 4):
                statement = DebateAgent._draft(other_index, ordinal)["statement"]
                assert statement in review_prompt
        for candidate in candidates:
            aliases_by_statement.setdefault(candidate["statement"], set()).add(
                candidate["key"]
            )

    assert any(len(aliases) > 1 for aliases in aliases_by_statement.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_review_mode", ["invented", "missing", "duplicate"])
async def test_generate_rejects_invalid_review_alias_coverage(
    tmp_path, invalid_review_mode: str
) -> None:
    task, _, _, _, _, _ = await _generate_successfully(
        tmp_path,
        invalid_review_agent=0,
        invalid_review_mode=invalid_review_mode,
    )

    result = await task

    failure = next(
        item
        for item in result.failures
        if item["stage"] == "review" and item["agent"] == "debater-0"
    )
    assert failure["error"] == "ValueError"
    assert not any(
        entry["stage"] == "revision" and entry["agent"] == "debater-0"
        for entry in result.transcript
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("invalid_judge_mode", "message"),
    [
        ("nonexistent", "judge decisions contain unknown candidate keys"),
        ("incomplete", "judge decisions must cover every surviving candidate key"),
        ("duplicate", "judge decisions cover candidate keys more than once"),
    ],
)
async def test_generate_rejects_invalid_judge_candidate_coverage(
    tmp_path, invalid_judge_mode: str, message: str
) -> None:
    task, _, _, _, _, _ = await _generate_successfully(
        tmp_path, invalid_judge_mode=invalid_judge_mode
    )

    with pytest.raises(RuntimeError, match="^ideator judge failed$") as raised:
        await task

    assert isinstance(raised.value.__cause__, ValueError)
    assert str(raised.value.__cause__) == message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_judge_mode",
    [
        "unlinked",
        "unknown_lineage",
        "rejected_lineage",
        "duplicate_lineage",
        "incomplete_lineage",
    ],
)
async def test_generate_wraps_invalid_final_hypothesis_lineage(
    tmp_path, invalid_judge_mode: str
) -> None:
    task, _, _, artifacts, _, _ = await _generate_successfully(
        tmp_path, invalid_judge_mode=invalid_judge_mode
    )

    with pytest.raises(RuntimeError, match="^ideator judge failed$"):
        await task

    assert artifacts.audit_payloads() == []


@pytest.mark.asyncio
async def test_generate_returns_linked_hypotheses_and_unlinked_audit_artifact(
    tmp_path,
) -> None:
    task, _, _, artifacts, _, expected_context = await _generate_successfully(tmp_path)

    result = await task
    audit = json.loads(await artifacts.get_text(result.artifact_ref))

    assert 3 <= len(result.hypotheses) <= 4
    assert all(hypothesis.parent_id == "exp-best" for hypothesis in result.hypotheses)
    assert all(hypothesis.status == "PROPOSED" for hypothesis in result.hypotheses)
    assert all(hypothesis.sources for hypothesis in result.hypotheses)
    assert all(
        hypothesis.evidence_refs == [result.artifact_ref]
        for hypothesis in result.hypotheses
    )
    assert audit["transcript"] == result.transcript
    assert audit["failures"] == result.failures == []
    assert audit["context"] == expected_context
    assert audit["hypotheses"] == [
        {**hypothesis.model_dump(mode="json"), "evidence_refs": []}
        for hypothesis in result.hypotheses
    ]
    assert audit["hypothesis_lineage"] == [
        {"hypothesis_id": result.hypotheses[0].id, "candidate_keys": ["idea-0-1"]},
        {"hypothesis_id": result.hypotheses[1].id, "candidate_keys": ["idea-0-2"]},
        {"hypothesis_id": result.hypotheses[2].id, "candidate_keys": ["idea-0-3"]},
    ]
    assert result.artifact_ref not in json.dumps(audit, sort_keys=True)


@pytest.mark.asyncio
async def test_generate_audit_rejects_duplicate_revision_keys(tmp_path) -> None:
    task, _, _, artifacts, _, _ = await _generate_successfully(
        tmp_path, duplicate_revision_keys=True
    )

    result = await task
    audit = json.loads(await artifacts.get_text(result.artifact_ref))

    assert any(
        item["stage"] == "revision" and item["agent"] == "debater-0"
        for item in result.failures
    )
    assert not any(
        item["stage"] == "revision" and item["agent"] == "debater-0"
        for item in result.transcript
    )
    assert audit["failures"] == result.failures


@pytest.mark.asyncio
async def test_proposal_failure_preserves_quorum_and_stable_audit_record(
    tmp_path,
) -> None:
    expected_failure = {
        "agent": "debater-2",
        "stage": "proposal",
        "error": "RuntimeError",
    }
    task, _, factory, artifacts, _, _ = await _generate_successfully(
        tmp_path,
        stage_failures={
            ("debater", 2, "proposal"): RuntimeError("private failure detail")
        },
    )

    result = await task

    assert factory.judge_calls == 1
    assert result.failures == [expected_failure]
    assert artifacts.audit_payloads()[0]["failures"] == [expected_failure]


@pytest.mark.asyncio
async def test_review_failures_below_quorum_skip_judge_and_final_audit(
    tmp_path,
) -> None:
    task, _, factory, artifacts, _, _ = await _generate_successfully(
        tmp_path,
        stage_failures={
            ("debater", 0, "review"): RuntimeError("first"),
            ("debater", 1, "review"): ValueError("second"),
        },
    )

    with pytest.raises(RuntimeError, match="^ideator quorum not met$"):
        await task

    assert factory.judge_calls == 0
    assert not any(agent.role == "judge" for agent in factory.agents)
    assert all(
        _RevisionBatch not in [output_type for _, output_type in agent.calls]
        for agent in factory.agents
    )
    assert artifacts.audit_payloads() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["proposal", "revision"])
async def test_quorum_is_checked_after_each_debater_stage(tmp_path, stage: str) -> None:
    task, _, factory, artifacts, _, _ = await _generate_successfully(
        tmp_path,
        stage_failures={
            ("debater", 0, stage): RuntimeError("first"),
            ("debater", 1, stage): RuntimeError("second"),
        },
    )

    with pytest.raises(RuntimeError, match="^ideator quorum not met$"):
        await task

    if stage == "proposal":
        assert all(
            _ReviewBatch not in [output_type for _, output_type in agent.calls]
            for agent in factory.agents
        )
    assert not any(agent.role == "judge" for agent in factory.agents)
    assert artifacts.audit_payloads() == []


@pytest.mark.asyncio
async def test_judge_failure_is_wrapped_once_without_fallback_or_audit(
    tmp_path,
) -> None:
    task, _, factory, artifacts, _, _ = await _generate_successfully(
        tmp_path,
        stage_failures={("judge", 0, "judge"): LookupError("judge unavailable")},
    )

    with pytest.raises(RuntimeError, match="^ideator judge failed$") as raised:
        await task

    # A persistent judge failure exhausts all bounded retry attempts before surfacing.
    assert str(raised.value.__cause__) == "turn failed: LookupError"
    assert factory.judge_calls == 3
    assert artifacts.audit_payloads() == []


@pytest.mark.asyncio
async def test_transient_judge_failure_recovers_within_retry_bound(
    tmp_path,
) -> None:
    task, _, factory, artifacts, _, _ = await _generate_successfully(
        tmp_path,
        transient_judge_failures=1,
    )

    result = await task

    assert result.hypotheses
    assert factory.judge_calls == 2
    assert artifacts.audit_payloads()


@pytest.mark.asyncio
async def test_proposal_timeout_interrupts_exact_turn_and_records_failure(
    tmp_path, monkeypatch
) -> None:
    RecordingRuntimeThreadManager.instances = []
    monkeypatch.setattr(
        "athena.ideator.ideator.RuntimeThreadManager", RecordingRuntimeThreadManager
    )
    block = StageBlock()
    task, _, _, _, _, _ = await _generate_successfully(
        tmp_path,
        stage_blocks={("debater", 2, "proposal"): block},
        stage_timeout_seconds=0.5,
    )

    result = await task

    manager = RecordingRuntimeThreadManager.instances[0]
    assert block.started.is_set()
    assert block.cancelled.is_set()
    assert result.failures == [
        {"agent": "debater-2", "stage": "proposal", "error": "TimeoutError"}
    ]
    assert len(manager.interrupts) == 1
    thread_id, turn_id, reason = manager.interrupts[0]
    interrupted_request = manager.submitted[(thread_id, turn_id)]
    assert interrupted_request["stage"] == "proposal"
    assert interrupted_request["role"] == "debater"
    assert interrupted_request["agent_index"] == 2
    assert reason == "ideator stage timeout"


class TimeoutRaceManager:
    def __init__(
        self, timeout_error: TimeoutError, interrupt_error: RuntimeError
    ) -> None:
        self.timeout_error = timeout_error
        self.interrupt_error = interrupt_error

    async def submit(self, thread_id: str, request_ref: str) -> AthenaTurn:
        return AthenaTurn(
            turn_id="turn:race",
            thread_id=thread_id,
            request_ref=request_ref,
            status="running",
        )

    async def wait_turn(self, thread_id: str, turn_id: str) -> str:
        del thread_id, turn_id
        raise self.timeout_error

    async def interrupt(self, thread_id: str, turn_id: str, reason: str) -> None:
        del thread_id, turn_id, reason
        raise self.interrupt_error


class TimeoutRaceRunner:
    async def put_request(self, stage, role, agent_index, prompt) -> str:
        del stage, role, agent_index, prompt
        return "artifact:request"

    async def read_result(self, stage, result_ref):
        raise AssertionError(f"unexpected result: {stage} {result_ref}")


@pytest.mark.asyncio
async def test_invoke_preserves_timeout_when_interrupt_races_with_completion() -> None:
    timeout_error = TimeoutError("stage deadline")
    manager = TimeoutRaceManager(
        timeout_error, RuntimeError("no active turn turn:race")
    )
    ideator = Ideator(agent_factory=None, artifacts=None)

    with pytest.raises(TimeoutError) as raised:
        await ideator._invoke(
            manager,
            TimeoutRaceRunner(),
            "thread:race",
            stage="proposal",
            role="debater",
            agent_index=0,
            prompt="prompt",
        )

    assert raised.value is timeout_error


@pytest.mark.asyncio
async def test_invoke_propagates_nonterminal_interrupt_error_after_timeout() -> None:
    interrupt_error = RuntimeError("interrupt transport failed")
    manager = TimeoutRaceManager(TimeoutError("stage deadline"), interrupt_error)
    ideator = Ideator(agent_factory=None, artifacts=None)

    with pytest.raises(RuntimeError, match="^interrupt transport failed$") as raised:
        await ideator._invoke(
            manager,
            TimeoutRaceRunner(),
            "thread:race",
            stage="proposal",
            role="debater",
            agent_index=0,
            prompt="prompt",
        )

    assert raised.value is interrupt_error


@pytest.mark.asyncio
async def test_cancel_closes_owned_manager_and_cancels_all_active_agents(
    tmp_path, monkeypatch
) -> None:
    RecordingRuntimeThreadManager.instances = []
    monkeypatch.setattr(
        "athena.ideator.ideator.RuntimeThreadManager", RecordingRuntimeThreadManager
    )
    task, barrier, factory, _, _, _ = await _generate_successfully(
        tmp_path, blocked=True
    )
    await asyncio.wait_for(barrier.all_entered.wait(), timeout=2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    manager = RecordingRuntimeThreadManager.instances[0]
    assert barrier.cancelled == 3
    assert barrier.all_cancelled.is_set()
    assert not any(agent.role == "judge" for agent in factory.agents)
    assert manager.state == "closed"
    assert manager.aclose_calls == ["ideator round finished"]
