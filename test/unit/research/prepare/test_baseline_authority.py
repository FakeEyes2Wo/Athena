"""Contract tests for the controller-owned baseline authority capability."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

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


@pytest.mark.asyncio
async def test_one_external_capability_survives_two_fresh_runtimes(
    tmp_path: Path,
) -> None:
    authority = MemoryBaselineAuthorityStore()
    assert isinstance(authority, BaselineAuthorityStore)

    first = ResearchRuntime(
        project_root=tmp_path / "workspace", baseline_authority=authority
    )
    created = await first.baseline_authority.seal(_bundle(), expected_generation=None)
    second = ResearchRuntime(
        project_root=tmp_path / "workspace", baseline_authority=authority
    )

    assert created.generation == 0
    assert second.baseline_authority is authority
    assert second.services.infrastructure.baseline_authority is authority
    assert await second.baseline_authority.load() == created


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


def test_non_integer_generation_raises_the_typed_boundary_error() -> None:
    with pytest.raises(BaselineAuthorityError, match="generation"):
        SealedBaseline(generation=0.5, bundle=_bundle())


def test_malformed_attestation_raises_the_typed_boundary_error() -> None:
    with pytest.raises(BaselineAuthorityError, match="research_sha256"):
        replace(_attestation(), research_sha256=None)


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
