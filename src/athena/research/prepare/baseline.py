"""Research-gated baseline design and trusted PREPARE execution."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from athena.agents.ideator_agent import (
    BASELINE_IDEATOR_PROFILE,
    register_ideator_agent,
)
from athena.agents.prepare_agent import register_prepare_agent
from athena.agents.supervisor_agent import MAX_PLAN_TURNS
from athena.research.prepare.baseline_research import (
    BaselineArtifacts,
    BaselineResearchError,
    VerifiedBaseline,
    assert_verified_files,
    load_baseline_artifacts,
    load_cached_verified_baseline,
    research_sha256,
    write_verification,
)
from athena.research.prepare.source_verification import build_default_source_verifier
from athena.research.supervisor.prepare import PrepareResult, run_prepare_plan

logger = logging.getLogger(__name__)

HandoffFn = Callable[..., Awaitable[str]]


def directory_candidate_task(task: str) -> str:
    """Add the directory-data evaluation split contract to a candidate task."""
    return (
        f"{task}\n\nFor directory data, read ATHENA_EVALUATION_SPLIT from "
        "os.environ. It is 'search' during SEARCH and 'final' during VALIDATE. "
        "Use it to select the matching deterministic partition; never hardcode "
        "one split or let groups cross partitions."
    )


def _register_ideator(runtime: Any, workspace: Path) -> None:
    """Register the baseline ideator once for the EDA workspace."""
    profile = BASELINE_IDEATOR_PROFILE
    if runtime.registry.contains(profile.agent_type):
        return
    register_ideator_agent(
        runtime.registry,
        provider=runtime.provider,
        artifacts=runtime.store,
        workspace=workspace,
        runtime=runtime.execution,
        extra_tools=runtime.baseline_ideator_tools(),
        gated=True,
        profile=profile,
    )


def _initial_research_request(task: str) -> str:
    """Build the first baseline evidence request."""
    return (
        f"{task}\n\nRead the task/data contract and EDA_HANDOFF.md. Use web and "
        "paper tools, then write complete BASELINE_RESEARCH.json and "
        "BASELINE_DESIGN.md. Do not write BASELINE_RESEARCH_VERIFICATION.json."
    )


def _repair_request(error: BaselineResearchError) -> str:
    """Build one bounded repair request from deterministic gate diagnostics."""
    diagnostics = "\n".join(f"- {item}" for item in error.diagnostics)
    return (
        "The deterministic baseline evidence gate rejected the artifacts.\n"
        f"{diagnostics}\n"
        "Correct the source or locator and rewrite both complete artifacts. "
        "Do not write BASELINE_RESEARCH_VERIFICATION.json."
    )


def verified_baseline_task(task: str, verified: VerifiedBaseline) -> str:
    """Append compact source provenance to the prepare-agent task."""
    evidence = verified.verification
    revision = evidence.commit or evidence.openalex_id or "unavailable"
    return (
        f"{task}\n\nPlatform-verified baseline artifacts:\n"
        "- BASELINE_RESEARCH.json\n"
        "- BASELINE_RESEARCH_VERIFICATION.json\n"
        "- BASELINE_DESIGN.md\n"
        f"Selected candidate: {verified.artifacts.selected.candidate_id}\n"
        f"Training strategy: {verified.artifacts.design.training_strategy}\n"
        f"Verification route: {evidence.route}\n"
        f"Verified revision: {revision}\n"
    )


def _research_error(error: Exception, fallback: str) -> BaselineResearchError:
    """Convert an agent/provider error into a bounded gate diagnostic."""
    if isinstance(error, BaselineResearchError):
        raw = error.diagnostics or (str(error),)
        diagnostics = tuple(str(item)[:1000] for item in raw[:4])
        return BaselineResearchError(str(error)[:500], diagnostics)
    return BaselineResearchError(fallback, (str(error)[:4000],))


async def _verify(verifier: Any, artifacts: BaselineArtifacts) -> VerifiedBaseline:
    """Verify artifacts and persist only evidence bound to their current bytes."""
    verification = await verifier.verify(artifacts)
    if verification.research_sha256 != research_sha256(artifacts.raw_research):
        raise BaselineResearchError("verification digest does not match research")
    if verification.selected_candidate_id != artifacts.selected.candidate_id:
        raise BaselineResearchError("verification candidate does not match research")
    write_verification(artifacts.root, verification)
    return VerifiedBaseline(artifacts, verification)


async def _reap_ideator(runtime: Any) -> None:
    """Release the one-shot baseline ideator without masking gate errors."""
    try:
        await asyncio.wait_for(
            runtime.agents.reap(BASELINE_IDEATOR_PROFILE.agent_type), 5.0
        )
    except Exception:
        # Agent cleanup failure → preserve the research gate result.
        logger.warning("failed to reap baseline ideator", exc_info=True)


async def prepare_baseline_design(
    runtime: Any,
    workspace: Any,
    task: str,
    eda_ready: bool,
    handoff_agent: HandoffFn,
    *,
    verifier: Any | None = None,
) -> VerifiedBaseline:
    """Research, verify, and return the baseline before PREPARE implementation."""
    root = Path(workspace.path)
    if not eda_ready:
        error = BaselineResearchError(
            "PREPARE requires a completed EDA handoff before baseline research",
            ("EDA_HANDOFF.md is unavailable",),
        )
        await runtime.publish_output(
            source="supervisor", channel="error", text=str(error)
        )
        raise error
    verifier = verifier or build_default_source_verifier()

    try:
        cached = load_cached_verified_baseline(root)
    except BaselineResearchError:
        # Stale or malformed cache → continue with artifact verification or repair.
        cached = None
    if cached is not None:
        return cached

    research_path = root / "BASELINE_RESEARCH.json"
    design_path = root / "BASELINE_DESIGN.md"
    complete_artifacts = research_path.is_file() and design_path.is_file()
    first_error: BaselineResearchError | None = None
    if not complete_artifacts and (research_path.exists() or design_path.exists()):
        first_error = BaselineResearchError(
            "baseline artifacts are incomplete",
            ("BASELINE_RESEARCH.json and BASELINE_DESIGN.md are both required",),
        )
    if complete_artifacts:
        try:
            artifacts = load_baseline_artifacts(root)
            return await _verify(verifier, artifacts)
        except Exception as error:
            # Existing artifacts failed parsing or verification → one repair turn.
            first_error = _research_error(
                error, "baseline artifacts failed verification"
            )

    resumed = first_error is not None
    try:
        _register_ideator(runtime, root)
        turn_limit = 1 if resumed else 2
        for turn in range(turn_limit):
            (root / "BASELINE_RESEARCH_VERIFICATION.json").unlink(missing_ok=True)
            content = (
                _repair_request(first_error)
                if first_error
                else _initial_research_request(task)
            )
            try:
                await handoff_agent(
                    agent_id=BASELINE_IDEATOR_PROFILE.agent_type,
                    agent_type=BASELINE_IDEATOR_PROFILE.agent_type,
                    workspace=str(root),
                    output_file="BASELINE_DESIGN.md",
                    content=content,
                    reap_after=False,
                )
                artifacts = load_baseline_artifacts(root)
                return await _verify(verifier, artifacts)
            except Exception as error:
                # Agent, parse, or verifier failure → retain only bounded diagnostics.
                first_error = _research_error(error, "baseline research was rejected")
                if turn == turn_limit - 1:
                    await runtime.publish_output(
                        source="supervisor",
                        channel="error",
                        text=f"PREPARE baseline research failed: {first_error}",
                    )
                    raise first_error from error
    finally:
        await _reap_ideator(runtime)

    raise BaselineResearchError("baseline research failed without a result")


def _register_prepare_agent(runtime: Any, workspace: Path) -> None:
    """Register the baseline implementation agent once."""
    if runtime.registry.contains("prepare"):
        return
    register_prepare_agent(
        runtime.registry,
        provider=runtime.provider,
        artifacts=runtime.store,
        workspace=workspace,
        runtime=runtime.execution,
        extra_tools=runtime.kaggle_tools("prepare"),
    )


async def run_baseline(
    runtime: Any,
    workspace: Any,
    evaluator_ref: Any,
    task: str,
    predict_features: Path | None,
    verified: VerifiedBaseline,
) -> PrepareResult:
    """Implement and score only a currently verified baseline design."""
    root = Path(workspace.path)
    assert_verified_files(root, verified)
    await runtime.publish_output(
        source="supervisor", channel="text", text="PREPARE: implementing baseline."
    )
    _register_prepare_agent(runtime, root)
    tree_ref = await runtime.store.put_text(
        json.dumps(runtime.tree.to_dict(), ensure_ascii=False, sort_keys=True)
    )
    return await run_prepare_plan(
        agents=runtime.agents,
        evaluator=runtime.evaluator,
        git=runtime.git,
        workspace=workspace,
        execution=runtime.execution,
        store=runtime.store,
        evaluator_ref=evaluator_ref,
        tree_ref=tree_ref,
        task=verified_baseline_task(task, verified),
        max_turns=MAX_PLAN_TURNS,
        publish=lambda kind, ref, data: runtime.events.project_agent_event(
            "prepare", kind, ref, data
        ),
        predict_features=predict_features,
    )
