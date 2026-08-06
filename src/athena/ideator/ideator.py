import asyncio
import json
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_ai import AgentRunResult
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from athena.app_server.thread_manager import RuntimeThreadManager
from athena.core.agent import AgentOutcome
from athena.core.contracts import ArtifactRef, new_id
from athena.core.research_models import Hypothesis
from athena.core.research_tree import ResearchTree
from athena.data.types import DataProfile
from athena.ideator.types import (
    DebateResult,
    _JudgeOutput,
    _ProposalBatch,
    _ReviewBatch,
    _RevisionBatch,
    _TurnRequest,
)
from athena.retrieval.types import HFModelRef, PaperRef
from athena.storage.artifact_store import ArtifactStore


class _StructuredAgent(Protocol):
    async def run(
        self,
        prompt: str,
        *,
        output_type: type[BaseModel],
        message_history: Sequence[ModelMessage] | None = None,
    ) -> object: ...


_OUTPUT_TYPES = {
    "proposal": _ProposalBatch,
    "review": _ReviewBatch,
    "revision": _RevisionBatch,
    "judge": _JudgeOutput,
}


class _DebateRunner:
    def __init__(self, *, agent_factory, artifacts: ArtifactStore) -> None:
        self._agent_factory = agent_factory
        self._artifacts = artifacts
        self._agents: dict[str, _StructuredAgent] = {}
        self._bindings: dict[str, tuple[str, int]] = {}
        self._histories: dict[str, list[ModelMessage]] = {}

    async def put_request(self, stage, role, agent_index, prompt) -> ArtifactRef:
        request = _TurnRequest(
            stage=stage,
            role=role,
            agent_index=agent_index,
            prompt=prompt,
        )
        return await self._artifacts.put_text(request.model_dump_json())

    async def read_result(self, stage, result_ref) -> BaseModel:
        schema = _OUTPUT_TYPES[stage]
        return schema.model_validate_json(await self._artifacts.get_text(result_ref))

    async def __call__(self, thread, turn, emit) -> AgentOutcome:
        return await self.run_with_context(thread, turn, emit, None, asyncio.Event())

    async def run_with_context(
        self, thread, turn, emit, memory, cancel
    ) -> AgentOutcome:
        if cancel.is_set():
            raise asyncio.CancelledError

        request = _TurnRequest.model_validate_json(
            await self._artifacts.get_text(turn.request_ref)
        )
        schema = _OUTPUT_TYPES[request.stage]
        binding = (request.role, request.agent_index)
        current_binding = self._bindings.get(thread.thread_id)
        if current_binding is None:
            self._bindings[thread.thread_id] = binding
            self._agents[thread.thread_id] = self._agent_factory(*binding)
        elif current_binding != binding:
            raise RuntimeError("thread agent binding changed")

        agent = self._agents[thread.thread_id]
        if memory is not None:
            memory.append(ModelRequest(parts=[UserPromptPart(content=request.prompt)]))
        result = await agent.run(
            request.prompt,
            output_type=schema,
            message_history=self._histories.get(thread.thread_id),
        )
        output = schema.model_validate(getattr(result, "output", result))
        output_json = output.model_dump_json()
        if isinstance(result, AgentRunResult):
            self._histories[thread.thread_id] = result.all_messages()
        else:
            history = self._histories.setdefault(thread.thread_id, [])
            history.extend(
                [
                    ModelRequest(parts=[UserPromptPart(content=request.prompt)]),
                    ModelResponse(parts=[TextPart(content=output_json)]),
                ]
            )
        if memory is not None:
            memory.append(ModelResponse(parts=[TextPart(content=output_json)]))
        result_ref = await self._artifacts.put_text(output_json)
        await emit("ideator/stage_completed", result_ref)
        return AgentOutcome(
            result_ref=result_ref,
            next_context_ref=thread.context_ref,
        )


class IdeatorConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    debater_count: int = Field(default=3, ge=2)
    quorum: int = Field(default=2, ge=2)
    stage_timeout_seconds: float = Field(default=120.0, gt=0)
    max_hypotheses: int = Field(default=5, ge=3, le=5)

    @model_validator(mode="after")
    def _validate_quorum(self) -> "IdeatorConfig":
        if self.quorum > self.debater_count:
            raise ValueError("quorum cannot exceed debater count")
        return self


class Ideator:
    def __init__(self, *, agent_factory, artifacts, config=None) -> None:
        self._agent_factory = agent_factory
        self._artifacts = artifacts
        self._config = config or IdeatorConfig()

    async def _invoke(
        self,
        manager: RuntimeThreadManager,
        runner: _DebateRunner,
        thread_id: str,
        *,
        stage: str,
        role: str,
        agent_index: int,
        prompt: str,
    ) -> BaseModel:
        request_ref = await runner.put_request(stage, role, agent_index, prompt)
        turn = await manager.submit(thread_id, request_ref)
        try:
            async with asyncio.timeout(self._config.stage_timeout_seconds):
                result_ref = await manager.wait_turn(thread_id, turn.turn_id)
        except TimeoutError:
            try:
                await manager.interrupt(
                    thread_id, turn.turn_id, "ideator stage timeout"
                )
            except RuntimeError as exc:
                if str(exc) != f"no active turn {turn.turn_id}":
                    raise
            raise
        return await runner.read_result(stage, result_ref)

    @staticmethod
    def _prompt(
        task: str,
        context_json: str,
        payload_name: str | None = None,
        payload: object | None = None,
    ) -> str:
        sections = [task, "Frozen research context:", context_json]
        if payload_name is not None:
            sections.extend(
                [
                    f"{payload_name}:",
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ]
            )
        return "\n\n".join(sections)

    @staticmethod
    def _failure(stage: str, agent: str, error: BaseException) -> dict[str, str]:
        return {
            "agent": agent,
            "stage": stage,
            "error": type(error).__name__,
        }

    def _handle_stage_failure(
        self,
        result: object,
        stage: str,
        agent_index: int,
        failures: list[dict[str, str]],
    ) -> bool:
        """阶段结果若为异常 → 记录失败并返回 True，调用方应 continue。"""
        if isinstance(result, BaseException):
            if isinstance(result, asyncio.CancelledError):
                raise result
            failures.append(self._failure(stage, f"debater-{agent_index}", result))
            return True
        return False

    def _validated_hypotheses(
        self, output: _JudgeOutput, parent_id: str | None
    ) -> list[Hypothesis]:
        drafts = output.hypotheses
        if not 3 <= len(drafts) <= self._config.max_hypotheses:
            raise ValueError("judge must return between 3 and max_hypotheses drafts")

        statements: set[str] = set()
        hypotheses: list[Hypothesis] = []
        for draft in drafts:
            text_fields = (
                draft.statement,
                draft.intervention,
                draft.expected_effect,
            )
            if any(not value.strip() for value in text_fields):
                raise ValueError("judge draft text fields must be nonblank")
            if not draft.sources or any(not source.strip() for source in draft.sources):
                raise ValueError("judge draft sources must be nonblank")
            if draft.statement in statements:
                raise ValueError("judge draft statements must be unique")
            statements.add(draft.statement)
            hypotheses.append(
                Hypothesis(
                    id=new_id("hyp"),
                    status="PROPOSED",
                    parent_id=parent_id,
                    statement=draft.statement,
                    intervention=draft.intervention,
                    expected_effect=draft.expected_effect,
                    sources=draft.sources,
                )
            )
        return hypotheses

    async def generate(
        self,
        profile: DataProfile,
        papers: list[PaperRef],
        models: list[HFModelRef],
        tree: ResearchTree,
    ) -> DebateResult:
        context_payload = {
            "profile": profile.model_dump(mode="json"),
            "papers": [asdict(paper) for paper in papers],
            "models": [asdict(model) for model in models],
            "tree": tree.to_dict(),
        }
        parent_id = tree.best_experiment_id()
        context_json = json.dumps(context_payload, ensure_ascii=False, sort_keys=True)
        frozen_context = json.loads(context_json)
        context_ref = await self._artifacts.put_text(context_json)

        runner = _DebateRunner(
            agent_factory=self._agent_factory, artifacts=self._artifacts
        )
        manager = RuntimeThreadManager(runner)
        transcript: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        proposals: dict[int, list[dict[str, Any]]] = {}
        reviews: dict[int, list[dict[str, Any]]] = {}
        revisions: dict[int, list[dict[str, Any]]] = {}

        try:
            debater_threads = [
                await manager.start("ideator-debate", context_ref)
                for _ in range(self._config.debater_count)
            ]
            proposal_prompt = self._prompt(
                "Propose 3 to "
                f"{self._config.max_hypotheses} falsifiable hypotheses. Each must "
                "have exactly one intervention, a measurable expected effect, "
                "non-empty source references, and must not duplicate any hypothesis "
                "already in the research tree.",
                context_json,
            )
            proposal_results = await asyncio.gather(
                *(
                    self._invoke(
                        manager,
                        runner,
                        thread.thread_id,
                        stage="proposal",
                        role="debater",
                        agent_index=agent_index,
                        prompt=proposal_prompt,
                    )
                    for agent_index, thread in enumerate(debater_threads)
                ),
                return_exceptions=True,
            )
            for agent_index, result in enumerate(proposal_results):
                if self._handle_stage_failure(
                    result, "proposal", agent_index, failures
                ):
                    continue
                batch = _ProposalBatch.model_validate(result)
                candidates = [
                    {
                        "key": f"idea-{agent_index}-{ordinal}",
                        **draft.model_dump(mode="json"),
                    }
                    for ordinal, draft in enumerate(batch.hypotheses, start=1)
                ]
                proposals[agent_index] = candidates
                transcript.append(
                    {
                        "stage": "proposal",
                        "agent": f"debater-{agent_index}",
                        "result": candidates,
                    }
                )

            proposal_survivors = sorted(proposals)
            if len(proposal_survivors) < self._config.quorum:
                raise RuntimeError("ideator quorum not met")
            candidates_by_key = {
                candidate["key"]: candidate
                for candidates in proposals.values()
                for candidate in candidates
            }
            review_aliases = {
                agent_index: {
                    f"candidate-{ordinal}": candidate["key"]
                    for ordinal, candidate in enumerate(
                        (
                            candidate
                            for other_index in proposal_survivors
                            if other_index != agent_index
                            for candidate in proposals[other_index]
                        ),
                        start=1,
                    )
                }
                for agent_index in proposal_survivors
            }
            review_results = await asyncio.gather(
                *(
                    self._invoke(
                        manager,
                        runner,
                        debater_threads[agent_index].thread_id,
                        stage="review",
                        role="debater",
                        agent_index=agent_index,
                        prompt=self._prompt(
                            "Review every anonymous candidate below for falsifiability, "
                            "intervention isolation, evidence quality, and measurable "
                            "effects. Return a critique for each candidate key.",
                            context_json,
                            "Anonymous candidates",
                            [
                                {
                                    **candidate,
                                    "key": alias,
                                }
                                for alias, internal_key in review_aliases[
                                    agent_index
                                ].items()
                                for candidate in (candidates_by_key[internal_key],)
                            ],
                        ),
                    )
                    for agent_index in proposal_survivors
                ),
                return_exceptions=True,
            )
            review_survivors: list[int] = []
            for agent_index, result in zip(proposal_survivors, review_results):
                if self._handle_stage_failure(result, "review", agent_index, failures):
                    continue
                batch = _ReviewBatch.model_validate(result)
                aliases = review_aliases[agent_index]
                critique_aliases = [critique.key for critique in batch.critiques]
                if len(critique_aliases) != len(aliases) or set(
                    critique_aliases
                ) != set(aliases):
                    error = ValueError(
                        "review must critique every offered alias exactly once"
                    )
                    failures.append(
                        self._failure("review", f"debater-{agent_index}", error)
                    )
                    continue
                critiques = [
                    {
                        **critique.model_dump(mode="json"),
                        "key": aliases[critique.key],
                    }
                    for critique in batch.critiques
                ]
                reviews[agent_index] = critiques
                review_survivors.append(agent_index)
                transcript.append(
                    {
                        "stage": "review",
                        "agent": f"debater-{agent_index}",
                        "result": critiques,
                    }
                )

            if len(review_survivors) < self._config.quorum:
                raise RuntimeError("ideator quorum not met")
            revision_results = await asyncio.gather(
                *(
                    self._invoke(
                        manager,
                        runner,
                        debater_threads[agent_index].thread_id,
                        stage="revision",
                        role="debater",
                        agent_index=agent_index,
                        prompt=self._prompt(
                            "Revise your original candidates using all critiques received. "
                            "Preserve every candidate key exactly.",
                            context_json,
                            "Own candidates and received critiques",
                            {
                                "candidates": proposals[agent_index],
                                "critiques": [
                                    critique
                                    for reviewer_critiques in reviews.values()
                                    for critique in reviewer_critiques
                                    if critique["key"].startswith(
                                        f"idea-{agent_index}-"
                                    )
                                ],
                            },
                        ),
                    )
                    for agent_index in review_survivors
                ),
                return_exceptions=True,
            )
            for agent_index, result in zip(review_survivors, revision_results):
                if self._handle_stage_failure(
                    result, "revision", agent_index, failures
                ):
                    continue
                batch = _RevisionBatch.model_validate(result)
                revised_candidates = [
                    hypothesis.model_dump(mode="json")
                    for hypothesis in batch.hypotheses
                ]
                expected_keys = {item["key"] for item in proposals[agent_index]}
                revised_keys = [item["key"] for item in revised_candidates]
                if (
                    len(revised_keys) != len(expected_keys)
                    or set(revised_keys) != expected_keys
                ):
                    error = ValueError("revision must preserve every candidate key")
                    failures.append(
                        self._failure("revision", f"debater-{agent_index}", error)
                    )
                    continue
                revisions[agent_index] = revised_candidates
                transcript.append(
                    {
                        "stage": "revision",
                        "agent": f"debater-{agent_index}",
                        "result": revised_candidates,
                    }
                )

            if len(revisions) < self._config.quorum:
                raise RuntimeError("ideator quorum not met")
            judge_thread = await manager.start("ideator-debate", context_ref)
            judge_prompt = self._prompt(
                "Judge the complete debate record. Return 3 to "
                f"{self._config.max_hypotheses} final drafts with candidate-key "
                "lineage and decisions covering selected, rejected, or merged "
                "candidate keys.",
                context_json,
                "Proposals, critiques, revisions, and failures",
                {
                    "proposals": proposals,
                    "critiques": reviews,
                    "revisions": revisions,
                    "failures": failures,
                },
            )
            try:
                judge_result = await self._invoke(
                    manager,
                    runner,
                    judge_thread.thread_id,
                    stage="judge",
                    role="judge",
                    agent_index=0,
                    prompt=judge_prompt,
                )
                judge_output = _JudgeOutput.model_validate(judge_result)
                surviving_keys = {
                    candidate["key"]
                    for candidates in revisions.values()
                    for candidate in candidates
                }
                decided_keys = [
                    key
                    for decision in judge_output.decisions
                    for key in decision.candidate_keys
                ]
                if not set(decided_keys).issubset(surviving_keys):
                    raise ValueError("judge decisions contain unknown candidate keys")
                if len(decided_keys) != len(set(decided_keys)):
                    raise ValueError(
                        "judge decisions cover candidate keys more than once"
                    )
                if set(decided_keys) != surviving_keys:
                    raise ValueError(
                        "judge decisions must cover every surviving candidate key"
                    )

                dispositions = {
                    key: decision.disposition
                    for decision in judge_output.decisions
                    for key in decision.candidate_keys
                }
                lineage_keys = [
                    key
                    for draft in judge_output.hypotheses
                    for key in draft.candidate_keys
                ]
                if not set(lineage_keys).issubset(surviving_keys):
                    raise ValueError("judge lineage contains unknown candidate keys")
                if any(dispositions[key] == "rejected" for key in lineage_keys):
                    raise ValueError("judge lineage contains rejected candidate keys")
                if len(lineage_keys) != len(set(lineage_keys)):
                    raise ValueError(
                        "judge lineage covers candidate keys more than once"
                    )
                selected_or_merged_keys = {
                    key
                    for key, disposition in dispositions.items()
                    if disposition in {"selected", "merged"}
                }
                if set(lineage_keys) != selected_or_merged_keys:
                    raise ValueError(
                        "judge lineage must cover every selected or merged "
                        "candidate key"
                    )
                hypotheses = self._validated_hypotheses(judge_output, parent_id)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                raise RuntimeError("ideator judge failed") from exc
            transcript.append(
                {
                    "stage": "judge",
                    "agent": "judge-0",
                    "result": judge_output.model_dump(mode="json"),
                }
            )
            audit_payload = {
                "context": frozen_context,
                "hypotheses": [
                    hypothesis.model_dump(mode="json") for hypothesis in hypotheses
                ],
                "transcript": transcript,
                "failures": failures,
                "judge_decisions": [
                    decision.model_dump(mode="json")
                    for decision in judge_output.decisions
                ],
                "hypothesis_lineage": [
                    {
                        "hypothesis_id": hypothesis.id,
                        "candidate_keys": draft.candidate_keys,
                    }
                    for hypothesis, draft in zip(
                        hypotheses, judge_output.hypotheses, strict=True
                    )
                ],
            }
            audit_ref = await self._artifacts.put_text(
                json.dumps(audit_payload, ensure_ascii=False, sort_keys=True)
            )
            linked_hypotheses = [
                Hypothesis.model_validate(
                    {
                        **hypothesis.model_dump(mode="json"),
                        "evidence_refs": [audit_ref],
                    }
                )
                for hypothesis in hypotheses
            ]
            return DebateResult(
                hypotheses=linked_hypotheses,
                transcript=transcript,
                failures=failures,
                artifact_ref=audit_ref,
            )
        finally:
            await manager.aclose("ideator round finished")
