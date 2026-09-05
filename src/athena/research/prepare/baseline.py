"""Baseline design and trusted PREPARE execution."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athena.agents.ideator_agent import (
    BASELINE_IDEATOR_PROFILE,
    register_ideator_agent,
)
from athena.agents.prepare_agent import register_prepare_agent
from athena.agents.supervisor_agent import MAX_PLAN_TURNS
from athena.research.prepare.authority import (
    BaselineAuthorityError,
    BaselineAuthorityStore,
    PrepareAttestation,
    SealedBaseline,
    VerifiedBaselineBundle,
)
from athena.research.prepare.baseline_research import (
    DESIGN_FILENAME,
    RESEARCH_FILENAME,
    VERIFICATION_FILENAME,
    BaselineResearchError,
    VerifiedBaseline,
    _parse_baseline_artifacts,
    _parse_canonical_verification,
    _write_verification_bytes,
    assert_verification_matches_artifacts,
    load_baseline_artifacts,
    verification_bytes,
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


@dataclass(frozen=True, slots=True)
class BaselineDesignRequest:
    """Inputs for independently researching and verifying one baseline."""

    task: str
    eda_ready: bool
    handoff_agent: HandoffFn
    verifier: BaselineSourceVerifier | None = None


@dataclass(frozen=True, slots=True)
class BaselineRunRequest:
    """Inputs for implementing and scoring one verified baseline."""

    evaluator_ref: Any
    task: str
    predict_features: Path | None
    verified: VerifiedBaseline


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


def _validated_sealed_baseline(root: Path, sealed: SealedBaseline) -> VerifiedBaseline:
    """Validate an external authority response without consulting local mirrors."""
    if type(sealed) is not SealedBaseline:
        raise BaselineAuthorityError("authority returned an invalid sealed baseline")
    bundle = sealed.bundle
    try:
        canonical, parsed_verification = _parse_canonical_verification(
            bundle.verification_bytes, bundle.verification
        )
        artifacts = _parse_baseline_artifacts(
            root, bundle.research_bytes, bundle.design_bytes
        )
        assert_verification_matches_artifacts(artifacts, parsed_verification)
    except BaselineResearchError as exc:
        raise BaselineAuthorityError(
            "authority returned baseline bytes that do not match verification"
        ) from exc
    return VerifiedBaseline(
        artifacts=artifacts,
        verification=parsed_verification,
        verification_bytes=canonical,
        authority_generation=sealed.generation,
    )


def _assert_valid_authority_lifecycle(sealed: SealedBaseline) -> None:
    """Accept only the two baseline generations defined by the authority protocol."""

    if type(sealed) is not SealedBaseline:
        raise BaselineAuthorityError("authority returned an invalid sealed baseline")
    attestation = sealed.attestation
    if sealed.generation == 0 and attestation is None:
        return
    if (
        sealed.generation == 1
        and type(attestation) is PrepareAttestation
        and attestation.research_sha256 == sealed.bundle.verification.research_sha256
        and attestation.design_sha256 == sealed.bundle.verification.design_sha256
    ):
        return
    raise BaselineAuthorityError("baseline authority lifecycle is invalid")


def _read_required_mirror(root: Path, filename: str) -> bytes:
    try:
        return (root / filename).read_bytes()
    except OSError as exc:
        raise BaselineResearchError(f"unable to read {filename}", (str(exc),)) from exc


def _assert_mirror_matches(root: Path, filename: str, expected: bytes) -> None:
    if _read_required_mirror(root, filename) != expected:
        raise BaselineResearchError(
            f"workspace mirror {filename} does not match external authority"
        )


async def _load_authoritative_baseline_record(
    root: Path, authority: BaselineAuthorityStore
) -> tuple[VerifiedBaseline, SealedBaseline] | None:
    """Load one external record and validate its exact workspace mirrors."""
    try:
        sealed = await authority.load()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - external capability boundary
        raise BaselineAuthorityError("baseline authority load failed") from None
    if sealed is None:
        return None

    _assert_valid_authority_lifecycle(sealed)
    verified = _validated_sealed_baseline(root, sealed)
    bundle = sealed.bundle
    _assert_mirror_matches(root, RESEARCH_FILENAME, bundle.research_bytes)
    _assert_mirror_matches(root, DESIGN_FILENAME, bundle.design_bytes)

    mirror = root / VERIFICATION_FILENAME
    try:
        local_verification = mirror.read_bytes()
    except FileNotFoundError:
        try:
            _write_verification_bytes(root, bundle.verification_bytes)
        except OSError as exc:
            raise BaselineResearchError(
                f"unable to restore {VERIFICATION_FILENAME}", (str(exc),)
            ) from exc
    except OSError as exc:
        raise BaselineResearchError(
            f"unable to read {VERIFICATION_FILENAME}", (str(exc),)
        ) from exc
    else:
        if local_verification != bundle.verification_bytes:
            raise BaselineResearchError(
                f"workspace mirror {VERIFICATION_FILENAME} does not match external authority"
            )
    return verified, sealed


async def load_authoritative_baseline(
    root: Path, authority: BaselineAuthorityStore
) -> VerifiedBaseline | None:
    """Load only externally sealed evidence and validate every present mirror."""
    loaded = await _load_authoritative_baseline_record(root, authority)
    return None if loaded is None else loaded[0]


async def seal_verified_baseline(
    authority: BaselineAuthorityStore, verified: VerifiedBaseline
) -> VerifiedBaseline:
    """Seal freshly verified exact bytes with initial compare-and-exchange."""
    bundle = _verified_bundle(verified)
    try:
        sealed = await authority.seal(bundle, expected_generation=None)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - external capability boundary
        raise BaselineAuthorityError("baseline authority seal failed") from None
    accepted = _validated_sealed_baseline(verified.artifacts.root, sealed)
    if (
        sealed.generation != 0
        or sealed.bundle != bundle
        or sealed.attestation is not None
    ):
        raise BaselineAuthorityError(
            "baseline authority returned unexpected sealed evidence"
        )
    return accepted


def _verified_bundle(verified: VerifiedBaseline) -> VerifiedBaselineBundle:
    """Return the exact authority payload carried by one verified baseline."""
    return VerifiedBaselineBundle(
        research_bytes=verified.artifacts.raw_research,
        design_bytes=verified.artifacts.raw_design,
        verification_bytes=verified.verification_bytes,
        verification=verified.verification,
    )


async def _attest_prepare_completion(
    authority: BaselineAuthorityStore,
    verified: VerifiedBaseline,
    result: PrepareResult,
    current: SealedBaseline,
) -> None:
    """Attach exact trusted scoring evidence to the in-memory generation."""
    bundle = _verified_bundle(verified)
    evidence = PrepareAttestation(
        research_sha256=verified.verification.research_sha256,
        design_sha256=verified.verification.design_sha256,
        baseline_commit=result.commit,
        evaluator_ref=result.evaluator_ref,
        evidence_ref=result.evidence_ref,
    )
    if current.generation != verified.authority_generation or current.bundle != bundle:
        raise BaselineAuthorityError(
            "external baseline authority changed during PREPARE"
        )
    if current.generation == 1:
        if current.attestation == evidence:
            return
        raise BaselineAuthorityError(
            "existing completion attestation does not match trusted PREPARE result"
        )
    if current.generation != 0 or current.attestation is not None:
        raise BaselineAuthorityError(
            "baseline authority completion generation is invalid"
        )
    try:
        sealed = await authority.attest_prepare(
            evidence,
            expected_generation=0,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - external capability boundary
        raise BaselineAuthorityError(
            "baseline authority completion attestation failed"
        ) from None
    if (
        type(sealed) is not SealedBaseline
        or sealed.generation != verified.authority_generation + 1
        or sealed.bundle != bundle
        or sealed.attestation != evidence
    ):
        raise BaselineAuthorityError(
            "baseline authority returned an unexpected completion attestation"
        )


@dataclass(slots=True)
class _BaselineResearchRun:
    """Resources shared by verification and both ideator attempts."""

    root: Path
    handoff_agent: HandoffFn
    verifier: BaselineSourceVerifier
    authority: BaselineAuthorityStore

    async def verify(self) -> VerifiedBaseline:
        """Verify stable current artifacts, seal them, and write the audit mirror."""
        artifacts = load_baseline_artifacts(self.root)
        verification = await self.verifier.verify(artifacts)
        current = load_baseline_artifacts(self.root)
        if current.raw_research != artifacts.raw_research:
            raise BaselineResearchError(
                f"{RESEARCH_FILENAME} changed during source verification"
            )
        if current.raw_design != artifacts.raw_design:
            raise BaselineResearchError(
                f"{DESIGN_FILENAME} changed during source verification"
            )
        assert_verification_matches_artifacts(current, verification)
        verified = VerifiedBaseline(
            artifacts=current,
            verification=verification,
            verification_bytes=verification_bytes(verification),
        )
        sealed = await seal_verified_baseline(self.authority, verified)
        try:
            _write_verification_bytes(self.root, sealed.verification_bytes)
        except OSError as error:
            raise BaselineAuthorityError(
                f"unable to write authoritative mirror {VERIFICATION_FILENAME}"
            ) from error
        return sealed

    async def turn(self, content: str) -> VerifiedBaseline | BaselineResearchError:
        """Run one logical-thread turn, then parse and verify its files."""
        _remove_verification(self.root)
        try:
            await self.handoff_agent(
                agent_id=BASELINE_IDEATOR_PROFILE.agent_type,
                agent_type=BASELINE_IDEATOR_PROFILE.agent_type,
                workspace=str(self.root),
                output_file=DESIGN_FILENAME,
                content=content,
                reap_after=False,
            )
        except Exception as error:  # noqa: BLE001 - Agent response boundary
            _remove_verification(self.root)
            return BaselineResearchError(
                "baseline ideator response failed", (_bounded_diagnostic(error),)
            )
        except BaseException:
            try:
                _remove_verification(self.root)
            except Exception:
                logger.warning(
                    "failed to remove baseline verification during interruption",
                    exc_info=True,
                )
            raise

        _remove_verification(self.root)
        try:
            return await self.verify()
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
    except Exception:  # noqa: BLE001 - reap observation boundary
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
    request: BaselineDesignRequest,
) -> VerifiedBaseline:
    """Return independently verified research, allowing at most one repair turn."""
    authority = getattr(runtime, "baseline_authority", None)
    if authority is None:
        raise BaselineAuthorityError(
            "PREPARE requires an external baseline authority capability"
        )
    if not request.eda_ready:
        error = BaselineResearchError(
            "EDA is unavailable; baseline research cannot be verified"
        )
        await _publish_terminal_research_error(runtime, error)
        raise error

    root = Path(workspace.path)
    ideator_active = False

    try:
        cached = await load_authoritative_baseline(root, authority)
        if cached is not None:
            return cached

        source_verifier = request.verifier or build_default_source_verifier()
        research_run = _BaselineResearchRun(
            root, request.handoff_agent, source_verifier, authority
        )
        _remove_verification(root)
        complete_artifacts = all(
            (root / filename).is_file()
            for filename in (RESEARCH_FILENAME, DESIGN_FILENAME)
        )
        if complete_artifacts:
            try:
                return await research_run.verify()
            except BaselineResearchError as error:
                first_error = _repairable(error)
        else:
            first_error = None

        _register_ideator(runtime, root)
        ideator_active = True
        if complete_artifacts:
            prompt = _repair_request(first_error)
        else:
            prompt = _initial_research_request(request.task)

        first_result = await research_run.turn(prompt)
        if isinstance(first_result, VerifiedBaseline):
            return first_result

        if complete_artifacts:
            terminal_error = first_result
        else:
            second_result = await research_run.turn(_repair_request(first_result))
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
        f"{verified.artifacts.training_strategy}\n"
        f"Verification route: {verification.route}\n"
        f"Verified revision: {revision}"
    )


async def run_baseline(
    runtime: Any,
    workspace: Any,
    request: BaselineRunRequest,
) -> PrepareResult:
    """Implement and score the trusted baseline through the frozen evaluator."""
    root = Path(workspace.path)
    authority = getattr(runtime, "baseline_authority", None)
    if authority is None:
        raise BaselineAuthorityError(
            "PREPARE requires an external baseline authority capability"
        )

    async def assert_baseline() -> None:
        """Reject authority or mirror drift from the in-memory generation."""
        await current_sealed_baseline()

    async def current_sealed_baseline() -> SealedBaseline:
        """Return the still-matching sealed authority record."""
        loaded = await _load_authoritative_baseline_record(root, authority)
        if loaded is None:
            raise BaselineAuthorityError(
                "external baseline authority has no sealed baseline"
            )
        current, sealed = loaded
        if (
            current.authority_generation != request.verified.authority_generation
            or current.artifacts.raw_research != request.verified.artifacts.raw_research
            or current.artifacts.raw_design != request.verified.artifacts.raw_design
            or current.verification_bytes != request.verified.verification_bytes
        ):
            raise BaselineAuthorityError(
                "external baseline authority changed during PREPARE"
            )
        return sealed

    await assert_baseline()
    await runtime.publish_output(
        source="supervisor", channel="text", text="PREPARE: implementing baseline."
    )
    # Register the implementation agent and freeze the current research tree.
    _register_prepare_agent(runtime, root)
    tree_ref = await runtime.store.put_text(
        json.dumps(runtime.tree.to_dict(), ensure_ascii=False, sort_keys=True)
    )
    # Delegate execution and trusted scoring to the supervisor plan.
    result = await run_prepare_plan(
        agents=runtime.agents,
        evaluator=runtime.evaluator,
        git=runtime.git,
        workspace=workspace,
        execution=runtime.execution,
        store=runtime.store,
        evaluator_ref=request.evaluator_ref,
        tree_ref=tree_ref,
        task=verified_baseline_task(request.task, request.verified),
        max_turns=MAX_PLAN_TURNS,
        publish=lambda kind, ref, data: runtime.events.project_agent_event(
            "prepare", kind, ref, data
        ),
        predict_features=request.predict_features,
        assert_baseline=assert_baseline,
    )
    current = await current_sealed_baseline()
    await _attest_prepare_completion(authority, request.verified, result, current)
    return result
