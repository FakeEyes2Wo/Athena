"""Contract tests for the controller-owned baseline authority capability."""

from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from athena.research.config import RuntimeDependencies
from athena.research.prepare.authority import (
    BaselineAuthorityConflict,
    BaselineAuthorityError,
    BaselineAuthorityStore,
    PrepareAttestation,
    SealedBaseline,
    VerifiedBaselineBundle,
)
from athena.research.prepare.baseline_research import (
    BaselineVerification,
    VerificationAttempt,
    verification_bytes,
)
from athena.research.runtime import ResearchRuntime


def _runtime(root: Path, authority: BaselineAuthorityStore) -> ResearchRuntime:
    return ResearchRuntime(
        project_root=root,
        dependencies=RuntimeDependencies(baseline_authority=authority),
    )


class MemoryBaselineAuthorityStore:
    """Test-only external service with generation compare-and-exchange."""

    def __init__(self) -> None:
        self._sealed: SealedBaseline | None = None

    async def load(self) -> SealedBaseline | None:
        return self._sealed

    async def seal(
        self,
        bundle: VerifiedBaselineBundle,
        *,
        expected_generation: int | None,
    ) -> SealedBaseline:
        current_generation = None if self._sealed is None else self._sealed.generation
        if expected_generation != current_generation:
            raise BaselineAuthorityConflict("baseline generation changed")
        generation = 0 if current_generation is None else current_generation + 1
        self._sealed = SealedBaseline(generation=generation, bundle=bundle)
        return self._sealed

    async def attest_prepare(
        self,
        evidence: PrepareAttestation,
        *,
        expected_generation: int,
    ) -> SealedBaseline:
        if self._sealed is None or self._sealed.generation != expected_generation:
            raise BaselineAuthorityConflict("baseline generation changed")
        self._sealed = SealedBaseline(
            generation=expected_generation + 1,
            bundle=self._sealed.bundle,
            attestation=evidence,
        )
        return self._sealed


def _verification() -> BaselineVerification:
    return BaselineVerification(
        schema_version=2,
        research_sha256="1" * 64,
        design_sha256="2" * 64,
        selected_candidate_id="paper-1",
        route="openalex",
        verified_at=datetime(2026, 9, 3, tzinfo=UTC),
        paper_locator="W123",
        openalex_id="W123",
        title="A sufficiently descriptive authoritative baseline title",
        publication_year=2024,
        cited_by_count=100,
        attempts=[
            VerificationAttempt(route="openalex", success=True, diagnostic="qualified")
        ],
    )


def _bundle() -> VerifiedBaselineBundle:
    verification = _verification()
    return VerifiedBaselineBundle(
        research_bytes=b'{"schema_version":2}\n',
        design_bytes=b"# Baseline design\n",
        verification_bytes=verification_bytes(verification),
        verification=verification,
    )


def _attestation() -> PrepareAttestation:
    return PrepareAttestation(
        research_sha256="1" * 64,
        design_sha256="2" * 64,
        baseline_commit="a" * 40,
        evaluator_ref="sha256:" + "3" * 64,
        evidence_ref="sha256:" + "4" * 64,
    )


def _rewrite_verification(sealed: SealedBaseline) -> None:
    sealed.bundle.verification.selected_candidate_id = "forged-paper"


def _rewrite_attempt(sealed: SealedBaseline) -> None:
    sealed.bundle.verification.attempts[0].diagnostic = "forged proof"


def _clear_attempts(sealed: SealedBaseline) -> None:
    sealed.bundle.verification.attempts.clear()


def _append_attempt(sealed: SealedBaseline) -> None:
    sealed.bundle.verification.attempts.append(
        VerificationAttempt(route="openalex", success=False, diagnostic="forged")
    )


@pytest.mark.asyncio
async def test_one_external_capability_survives_two_fresh_runtimes(
    tmp_path: Path,
) -> None:
    authority = MemoryBaselineAuthorityStore()
    assert isinstance(authority, BaselineAuthorityStore)

    first = _runtime(tmp_path / "workspace", authority)
    created = await first.baseline_authority.seal(_bundle(), expected_generation=None)
    second = _runtime(tmp_path / "workspace", authority)

    assert created.generation == 0
    assert second.baseline_authority is authority
    assert second.services.infrastructure.baseline_authority is authority
    assert await second.baseline_authority.load() == created


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate", "error_type"),
    [
        (_rewrite_verification, ValidationError),
        (_rewrite_attempt, ValidationError),
        (_clear_attempts, AttributeError),
        (_append_attempt, AttributeError),
    ],
)
async def test_sealed_verification_is_deeply_immutable_across_fresh_runtimes(
    tmp_path: Path,
    mutate: Callable[[SealedBaseline], None],
    error_type: type[Exception],
) -> None:
    authority = MemoryBaselineAuthorityStore()
    first = _runtime(tmp_path / "workspace", authority)
    await first.baseline_authority.seal(_bundle(), expected_generation=None)
    loaded = await first.baseline_authority.load()
    assert loaded is not None
    original = verification_bytes(loaded.bundle.verification)

    with pytest.raises(error_type):
        mutate(loaded)

    second = _runtime(tmp_path / "workspace", authority)
    reloaded = await second.baseline_authority.load()
    assert reloaded is not None
    assert verification_bytes(reloaded.bundle.verification) == original


@pytest.mark.asyncio
async def test_seal_rejects_a_stale_generation_and_advances_the_current_one() -> None:
    authority = MemoryBaselineAuthorityStore()
    created = await authority.seal(_bundle(), expected_generation=None)

    with pytest.raises(BaselineAuthorityConflict, match="generation changed"):
        await authority.seal(_bundle(), expected_generation=None)

    replaced = await authority.seal(_bundle(), expected_generation=created.generation)
    assert replaced.generation == 1


def test_authority_values_defensively_copy_bytes_and_are_frozen() -> None:
    research = bytearray(b"research")
    design = bytearray(b"design")
    canonical_verification = bytearray(b"verification")
    bundle = VerifiedBaselineBundle(
        research_bytes=research,
        design_bytes=design,
        verification_bytes=canonical_verification,
        verification=_verification(),
    )
    research[:] = b"tampered"
    design[:] = b"tamper"
    canonical_verification[:] = b"tampered!!!"

    assert bundle.research_bytes == b"research"
    assert bundle.design_bytes == b"design"
    assert bundle.verification_bytes == b"verification"
    with pytest.raises(FrozenInstanceError):
        bundle.research_bytes = b"replacement"

    attestation = _attestation()
    sealed = SealedBaseline(generation=0, bundle=bundle, attestation=attestation)
    with pytest.raises(FrozenInstanceError):
        attestation.baseline_commit = "b" * 40
    with pytest.raises(FrozenInstanceError):
        sealed.generation = 1


@pytest.mark.parametrize(
    "field", ["research_bytes", "design_bytes", "verification_bytes"]
)
@pytest.mark.parametrize("value", [1, True, [65], 10**100])
def test_authority_bundle_rejects_non_bytes_payloads_with_typed_error(
    field: str, value: object
) -> None:
    bundle = _bundle()
    values = {
        "research_bytes": bundle.research_bytes,
        "design_bytes": bundle.design_bytes,
        "verification_bytes": bundle.verification_bytes,
        "verification": bundle.verification,
    }
    values[field] = value

    with pytest.raises(BaselineAuthorityError, match=field):
        VerifiedBaselineBundle(**values)


@pytest.mark.parametrize(
    "field", ["research_bytes", "design_bytes", "verification_bytes"]
)
def test_authority_bundle_wraps_released_memoryview_errors(field: str) -> None:
    released = memoryview(b"released")
    released.release()
    bundle = _bundle()
    values = {
        "research_bytes": bundle.research_bytes,
        "design_bytes": bundle.design_bytes,
        "verification_bytes": bundle.verification_bytes,
        "verification": bundle.verification,
    }
    values[field] = released

    with pytest.raises(BaselineAuthorityError, match=field):
        VerifiedBaselineBundle(**values)


def test_non_integer_generation_raises_the_typed_boundary_error() -> None:
    with pytest.raises(BaselineAuthorityError, match="generation"):
        SealedBaseline(generation=0.5, bundle=_bundle())


def test_malformed_attestation_raises_the_typed_boundary_error() -> None:
    with pytest.raises(BaselineAuthorityError, match="research_sha256"):
        replace(_attestation(), research_sha256=None)


def test_authority_values_reject_corrupt_nested_values_with_typed_errors() -> None:
    invalid_builders = [
        (
            "verification",
            lambda: replace(_bundle(), verification=object()),
        ),
        (
            "bundle",
            lambda: SealedBaseline(generation=0, bundle=object()),
        ),
        (
            "attestation",
            lambda: SealedBaseline(
                generation=0,
                bundle=_bundle(),
                attestation=object(),
            ),
        ),
    ]

    for field, build in invalid_builders:
        with pytest.raises(BaselineAuthorityError, match=field):
            build()


def test_authority_values_require_exact_nested_types() -> None:
    class DerivedVerification(BaselineVerification):
        pass

    class DerivedBundle(VerifiedBaselineBundle):
        pass

    class DerivedAttestation(PrepareAttestation):
        pass

    derived_verification = DerivedVerification.model_validate(
        _verification().model_dump()
    )
    base_bundle = _bundle()
    derived_bundle = DerivedBundle(
        research_bytes=base_bundle.research_bytes,
        design_bytes=base_bundle.design_bytes,
        verification_bytes=base_bundle.verification_bytes,
        verification=base_bundle.verification,
    )
    base_attestation = _attestation()
    derived_attestation = DerivedAttestation(
        research_sha256=base_attestation.research_sha256,
        design_sha256=base_attestation.design_sha256,
        baseline_commit=base_attestation.baseline_commit,
        evaluator_ref=base_attestation.evaluator_ref,
        evidence_ref=base_attestation.evidence_ref,
    )
    invalid_builders = [
        (
            "verification",
            lambda: replace(_bundle(), verification=derived_verification),
        ),
        (
            "bundle",
            lambda: SealedBaseline(generation=0, bundle=derived_bundle),
        ),
        (
            "attestation",
            lambda: SealedBaseline(
                generation=0,
                bundle=_bundle(),
                attestation=derived_attestation,
            ),
        ),
    ]

    for field, build in invalid_builders:
        with pytest.raises(BaselineAuthorityError, match=field):
            build()


def test_bundle_revalidates_and_copies_list_backed_verification_attempts() -> None:
    source = _verification()
    mutable_attempts = list(source.attempts)
    unvalidated = source.model_copy(update={"attempts": mutable_attempts})

    bundle = replace(_bundle(), verification=unvalidated)
    mutable_attempts.append(
        VerificationAttempt(route="openalex", success=False, diagnostic="tampered")
    )

    assert isinstance(bundle.verification.attempts, tuple)
    assert len(bundle.verification.attempts) == 1


@pytest.mark.parametrize(
    "attempts",
    [
        (),
        (
            VerificationAttempt(
                route="openalex", success=False, diagnostic="not qualified"
            ),
        ),
    ],
)
def test_bundle_rejects_unvalidated_invalid_verification_attempts(
    attempts: tuple[VerificationAttempt, ...],
) -> None:
    unvalidated = _verification().model_copy(update={"attempts": attempts})

    with pytest.raises(BaselineAuthorityError, match="verification"):
        replace(_bundle(), verification=unvalidated)


@pytest.mark.asyncio
async def test_prepare_attestation_advances_the_sealed_generation() -> None:
    authority = MemoryBaselineAuthorityStore()
    created = await authority.seal(_bundle(), expected_generation=None)
    attestation = _attestation()

    attested = await authority.attest_prepare(
        attestation, expected_generation=created.generation
    )

    assert attested.generation == 1
    assert attested.bundle == created.bundle
    assert attested.attestation == attestation
    with pytest.raises(BaselineAuthorityConflict, match="generation changed"):
        await authority.attest_prepare(
            attestation, expected_generation=created.generation
        )


def test_none_authority_does_not_create_a_local_fallback(tmp_path: Path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)

    assert runtime.baseline_authority is None
    assert runtime.services.infrastructure.baseline_authority is None
