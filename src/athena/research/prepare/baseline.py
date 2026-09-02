"""Baseline design and trusted PREPARE execution."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from athena.agents.ideator_agent import (
    BASELINE_IDEATOR_PROFILE,
    register_ideator_agent,
)
from athena.agents.prepare_agent import register_prepare_agent
from athena.agents.supervisor_agent import MAX_PLAN_TURNS
from athena.research.prepare.baseline_research import (
    DESIGN_FILENAME,
    RESEARCH_FILENAME,
    VERIFICATION_FILENAME,
    BaselineResearchError,
    BaselineVerification,
    VerifiedBaseline,
    assert_verified_files,
    load_baseline_artifacts,
    load_cached_verified_baseline,
    research_sha256,
    write_verification,
)
from athena.research.prepare.source_verification import (
    BaselineSourceVerifier,
    build_default_source_verifier,
)
from athena.research.supervisor.prepare import PrepareResult, run_prepare_plan

logger = logging.getLogger(__name__)

HandoffFn = Callable[..., Awaitable[str]]
_IDEATOR_REAP_TIMEOUT_SECONDS = 5.0
_DIAGNOSTIC_LIMIT = 4000
_PENDING_IDEATOR_REAPS: set[asyncio.Task[None]] = set()


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
    return (
        f"{task}\n\nRead the task/data contract and EDA_HANDOFF.md. Use web and "
        "paper tools, then write complete BASELINE_RESEARCH.json and "
        "BASELINE_DESIGN.md."
    )


def _repair_request(error: BaselineResearchError) -> str:
    diagnostics = error.diagnostics or (str(error),)
    rendered = "\n".join(f"- {item}" for item in diagnostics)
    return (
        "The deterministic baseline evidence gate rejected the artifacts.\n"
        f"{rendered}\n"
        "Correct the source or locator and rewrite both complete artifacts. "
        "Do not write BASELINE_RESEARCH_VERIFICATION.json."
    )


def _bounded_diagnostic(error: Exception) -> str:
    return " ".join(str(error).split())[:_DIAGNOSTIC_LIMIT] or type(error).__name__


def _remove_verification(root: Path) -> None:
    """Remove any non-authoritative verification before independent checks."""
    try:
        (root / VERIFICATION_FILENAME).unlink(missing_ok=True)
    except OSError as error:
        raise BaselineResearchError(
            f"unable to remove {VERIFICATION_FILENAME}",
            (_bounded_diagnostic(error),),
        ) from error


def _repairable(error: BaselineResearchError) -> BaselineResearchError:
    """Ensure the repair prompt includes the gate's high-level failure."""
    if str(error) in error.diagnostics:
        return error
    return BaselineResearchError(str(error), (str(error), *error.diagnostics))


async def _verify_current_artifacts(
    root: Path, verifier: BaselineSourceVerifier
) -> VerifiedBaseline:
    artifacts = load_baseline_artifacts(root)
    verification = await verifier.verify(artifacts)
    verified = VerifiedBaseline(artifacts=artifacts, verification=verification)
    assert_verified_files(root, verified)
    try:
        write_verification(root, verification)
    except OSError as error:
        raise BaselineResearchError(
            f"unable to write {VERIFICATION_FILENAME}",
            (_bounded_diagnostic(error),),
        ) from error
    return verified


async def _run_ideator_turn(
    *,
    handoff_agent: HandoffFn,
    root: Path,
    content: str,
    verifier: BaselineSourceVerifier,
) -> VerifiedBaseline | BaselineResearchError:
    """Run one logical-thread turn, then independently parse and verify its files."""
    _remove_verification(root)
    try:
        await handoff_agent(
            agent_id=BASELINE_IDEATOR_PROFILE.agent_type,
            agent_type=BASELINE_IDEATOR_PROFILE.agent_type,
            workspace=str(root),
            output_file=DESIGN_FILENAME,
            content=content,
            reap_after=False,
        )
    except Exception as error:
        _remove_verification(root)
        return BaselineResearchError(
            "baseline ideator response failed", (_bounded_diagnostic(error),)
        )
    except BaseException:
        try:
            _remove_verification(root)
        except Exception:
            logger.warning(
                "failed to remove baseline verification during interruption",
                exc_info=True,
            )
        raise

    _remove_verification(root)
    try:
        return await _verify_current_artifacts(root, verifier)
    except BaselineResearchError as error:
        return _repairable(error)


def _observe_ideator_reap(task: asyncio.Task[None]) -> None:
    """Retain and consume delayed reap results without replacing primary work."""
    if task not in _PENDING_IDEATOR_REAPS:
        return
    _PENDING_IDEATOR_REAPS.remove(task)
    if task.cancelled():
        logger.warning("baseline ideator reap was cancelled")
        return
    error = task.exception()
    if error is not None:
        logger.warning(
            "failed to reap baseline ideator",
            exc_info=(type(error), error, error.__traceback__),
        )


async def _reap_ideator(runtime: Any) -> None:
    """Bound the caller's wait while retaining the reusable-thread cleanup."""
    try:
        task = asyncio.create_task(
            runtime.agents.reap(BASELINE_IDEATOR_PROFILE.agent_type)
        )
    except Exception:
        logger.warning("failed to start baseline ideator reap", exc_info=True)
        return

    _PENDING_IDEATOR_REAPS.add(task)
    task.add_done_callback(_observe_ideator_reap)
    try:
        await asyncio.wait_for(
            asyncio.shield(task),
            timeout=_IDEATOR_REAP_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning(
            "timed out reaping baseline ideator after %.1f seconds",
            _IDEATOR_REAP_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is None or current.cancelling():
            raise
        if task.cancelled():
            _observe_ideator_reap(task)
            return
        raise
    except Exception:
        _observe_ideator_reap(task)
    else:
        _observe_ideator_reap(task)


async def _publish_terminal_research_error(
    runtime: Any, error: BaselineResearchError
) -> None:
    await runtime.publish_output(
        source="supervisor",
        channel="error",
        text=f"PREPARE: baseline research failed ({error}).",
    )


async def prepare_baseline_design(
    runtime: Any,
    workspace: Any,
    task: str,
    eda_ready: bool,
    handoff_agent: HandoffFn,
    *,
    verifier: BaselineSourceVerifier | None = None,
) -> VerifiedBaseline:
    """Return independently verified research, allowing at most one repair turn."""
    if not eda_ready:
        error = BaselineResearchError(
            "EDA is unavailable; baseline research cannot be verified"
        )
        await _publish_terminal_research_error(runtime, error)
        raise error

    root = Path(workspace.path)
    complete_artifacts = all(
        (root / filename).is_file() for filename in (RESEARCH_FILENAME, DESIGN_FILENAME)
    )
    ideator_active = False

    try:
        try:
            cached = load_cached_verified_baseline(root)
        except BaselineResearchError as error:
            cache_error = _repairable(error)
        else:
            if cached is not None:
                return cached
            cache_error = None

        source_verifier = verifier or build_default_source_verifier()
        _remove_verification(root)
        if complete_artifacts:
            try:
                return await _verify_current_artifacts(root, source_verifier)
            except BaselineResearchError as error:
                first_error = _repairable(error)
        else:
            first_error = cache_error

        _register_ideator(runtime, root)
        ideator_active = True
        if complete_artifacts:
            request = _repair_request(first_error)
        else:
            request = _initial_research_request(task)

        first_result = await _run_ideator_turn(
            handoff_agent=handoff_agent,
            root=root,
            content=request,
            verifier=source_verifier,
        )
        if isinstance(first_result, VerifiedBaseline):
            return first_result

        if complete_artifacts:
            terminal_error = first_result
        else:
            second_result = await _run_ideator_turn(
                handoff_agent=handoff_agent,
                root=root,
                content=_repair_request(first_result),
                verifier=source_verifier,
            )
            if isinstance(second_result, VerifiedBaseline):
                return second_result
            terminal_error = second_result

        _remove_verification(root)
        await _publish_terminal_research_error(runtime, terminal_error)
        raise terminal_error
    finally:
        if ideator_active:
            await _reap_ideator(runtime)


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


def verified_baseline_task(task: str, verified: VerifiedBaseline) -> str:
    """Append deterministic platform provenance for the PREPARE agent."""
    verification = verified.verification
    revision = (
        verification.commit if verification.route == "git" else verification.openalex_id
    )
    return (
        f"{task}\n\nPlatform-verified baseline artifacts:\n"
        f"- {RESEARCH_FILENAME}\n"
        f"- {VERIFICATION_FILENAME}\n"
        f"- {DESIGN_FILENAME}\n"
        f"Selected candidate: {verified.artifacts.selected.candidate_id}\n"
        "Training strategy: "
        f"{verified.artifacts.design.training_strategy}\n"
        f"Verification route: {verification.route}\n"
        f"Verified revision: {revision}"
    )


def _assert_prepare_evidence(root: Path, verified: VerifiedBaseline) -> None:
    """Reload and match all three platform-verified PREPARE artifacts."""
    try:
        assert_verified_files(root, verified)
    except BaselineResearchError as error:
        try:
            actual_digest = research_sha256((root / RESEARCH_FILENAME).read_bytes())
        except OSError:
            raise error
        expected_digest = verified.verification.research_sha256
        if actual_digest != expected_digest:
            raise BaselineResearchError(
                "research artifact digest does not match verification",
                (f"expected={expected_digest}", f"actual={actual_digest}"),
            ) from error
        raise

    verification_path = root / VERIFICATION_FILENAME
    try:
        raw_verification = verification_path.read_bytes()
    except OSError as error:
        raise BaselineResearchError(
            f"unable to read verification artifact {VERIFICATION_FILENAME}",
            (_bounded_diagnostic(error),),
        ) from error
    try:
        on_disk = BaselineVerification.model_validate_json(raw_verification)
    except (ValidationError, ValueError) as error:
        raise BaselineResearchError(
            f"invalid verification artifact {VERIFICATION_FILENAME}",
            (_bounded_diagnostic(error),),
        ) from error
    if on_disk != verified.verification:
        raise BaselineResearchError(
            "verification artifact does not match verified evidence"
        )


async def run_baseline(
    runtime: Any,
    workspace: Any,
    evaluator_ref: Any,
    task: str,
    predict_features: Path | None,
    verified: VerifiedBaseline,
) -> PrepareResult:
    """Implement and score the trusted baseline through the frozen evaluator."""
    root = Path(workspace.path)
    _assert_prepare_evidence(root, verified)
    await runtime.publish_output(
        source="supervisor", channel="text", text="PREPARE: implementing baseline."
    )
    # Register the implementation agent and freeze the current research tree.
    _register_prepare_agent(runtime, root)
    tree_ref = await runtime.store.put_text(
        json.dumps(runtime.tree.to_dict(), ensure_ascii=False, sort_keys=True)
    )
    # Delegate execution and trusted scoring to the supervisor plan.
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
