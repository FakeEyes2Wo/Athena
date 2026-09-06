"""Persistence contract for the single-machine baseline authority."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from athena.research.prepare.authority import (
    BaselineAuthorityConflict,
    BaselineAuthorityError,
    PrepareAttestation,
    VerifiedBaselineBundle,
)
from athena.research.prepare.authority_local import LocalBaselineAuthorityStore
from athena.research.prepare.baseline_research import (
    BaselineVerification,
    VerificationAttempt,
    verification_bytes,
)


def _bundle() -> VerifiedBaselineBundle:
    verification = BaselineVerification(
        schema_version=2,
        research_sha256="1" * 64,
        design_sha256="2" * 64,
        selected_candidate_id="paper-1",
        route="openalex",
        verified_at=datetime(2026, 9, 6, tzinfo=UTC),
        paper_locator="W123",
        openalex_id="W123",
        title="A sufficiently descriptive authoritative baseline title",
        publication_year=2024,
        cited_by_count=100,
        attempts=[
            VerificationAttempt(route="openalex", success=True, diagnostic="qualified")
        ],
    )
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
async def test_local_authority_persists_exact_bundle_and_attestation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "controller"
    project = tmp_path / "project"
    first = LocalBaselineAuthorityStore(root, project, "session-1")

    sealed = await first.seal(_bundle(), expected_generation=None)
    attested = await first.attest_prepare(_attestation(), expected_generation=0)
    reloaded = await LocalBaselineAuthorityStore(root, project, "session-1").load()

    assert sealed.generation == 0
    assert attested.generation == 1
    assert reloaded == attested
    assert reloaded is not None
    assert reloaded.bundle.research_bytes == b'{"schema_version":2}\n'


@pytest.mark.asyncio
async def test_local_authority_rejects_stale_or_corrupt_records(tmp_path: Path) -> None:
    store = LocalBaselineAuthorityStore(
        tmp_path / "controller", tmp_path / "project", "one"
    )
    await store.seal(_bundle(), expected_generation=None)

    with pytest.raises(BaselineAuthorityConflict, match="generation changed"):
        await store.seal(_bundle(), expected_generation=None)

    record = json.loads(store.record_path.read_text(encoding="utf-8"))
    record["bundle"]["research_bytes"] = "not-base64!"
    store.record_path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(BaselineAuthorityError, match="record is invalid"):
        await store.load()


@pytest.mark.asyncio
async def test_local_authority_namespaces_project_and_session(tmp_path: Path) -> None:
    root = tmp_path / "controller"
    first = LocalBaselineAuthorityStore(root, tmp_path / "project-a", "one")
    different_project = LocalBaselineAuthorityStore(root, tmp_path / "project-b", "one")
    different_session = LocalBaselineAuthorityStore(root, tmp_path / "project-a", "two")

    await first.seal(_bundle(), expected_generation=None)

    assert first.record_path != different_project.record_path
    assert first.record_path != different_session.record_path
    assert await different_project.load() is None
    assert await different_session.load() is None
