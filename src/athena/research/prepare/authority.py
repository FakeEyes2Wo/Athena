"""Controller-owned capability for durable baseline verification authority."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from athena.research.prepare.baseline_research import BaselineVerification


class BaselineAuthorityError(RuntimeError):
    """The external baseline authority could not provide a trusted result."""


class BaselineAuthorityConflict(BaselineAuthorityError):
    """The external baseline generation changed before an atomic update."""


def _exact_bytes(value: bytes | bytearray | memoryview, *, field: str) -> bytes:
    """Copy one non-empty bytes-like payload into an immutable value."""
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise BaselineAuthorityError(f"{field} must be bytes-like")
    try:
        copied = bytes(value)
    except (TypeError, ValueError) as exc:
        raise BaselineAuthorityError(f"{field} must be readable bytes-like") from exc
    if not copied:
        raise BaselineAuthorityError(f"{field} must not be empty")
    return copied


def _sha256(value: str, *, field: str) -> str:
    """Validate one lowercase SHA-256 digest carried by an attestation."""
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise BaselineAuthorityError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _substantive(value: str, *, field: str) -> str:
    """Reject missing authority references without changing their identity."""
    if not isinstance(value, str) or not value.strip():
        raise BaselineAuthorityError(f"{field} must not be blank")
    return value


@dataclass(frozen=True, slots=True)
class VerifiedBaselineBundle:
    """Exact baseline bytes and their parsed platform verification."""

    research_bytes: bytes
    design_bytes: bytes
    verification_bytes: bytes
    verification: "BaselineVerification"

    def __post_init__(self) -> None:
        if type(self.verification) is not BaselineVerification:
            raise BaselineAuthorityError(
                "verification must be exactly BaselineVerification"
            )
        object.__setattr__(
            self,
            "research_bytes",
            _exact_bytes(self.research_bytes, field="research_bytes"),
        )
        object.__setattr__(
            self,
            "design_bytes",
            _exact_bytes(self.design_bytes, field="design_bytes"),
        )
        object.__setattr__(
            self,
            "verification_bytes",
            _exact_bytes(self.verification_bytes, field="verification_bytes"),
        )


@dataclass(frozen=True, slots=True)
class PrepareAttestation:
    """Trusted evidence binding a scored PREPARE result to baseline artifacts."""

    research_sha256: str
    design_sha256: str
    baseline_commit: str
    evaluator_ref: str
    evidence_ref: str

    def __post_init__(self) -> None:
        _sha256(self.research_sha256, field="research_sha256")
        _sha256(self.design_sha256, field="design_sha256")
        _substantive(self.baseline_commit, field="baseline_commit")
        _substantive(self.evaluator_ref, field="evaluator_ref")
        _substantive(self.evidence_ref, field="evidence_ref")


@dataclass(frozen=True, slots=True)
class SealedBaseline:
    """One immutable authority generation with optional PREPARE completion."""

    generation: int
    bundle: VerifiedBaselineBundle
    attestation: PrepareAttestation | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.generation, int)
            or isinstance(self.generation, bool)
            or self.generation < 0
        ):
            raise BaselineAuthorityError("generation must be a non-negative integer")
        if type(self.bundle) is not VerifiedBaselineBundle:
            raise BaselineAuthorityError(
                "bundle must be exactly VerifiedBaselineBundle"
            )
        if self.attestation is None:
            return
        if type(self.attestation) is not PrepareAttestation:
            raise BaselineAuthorityError(
                "attestation must be exactly PrepareAttestation"
            )
        if (
            self.attestation.research_sha256 != self.bundle.verification.research_sha256
            or self.attestation.design_sha256 != self.bundle.verification.design_sha256
        ):
            raise BaselineAuthorityError(
                "PREPARE attestation does not match the sealed baseline digests"
            )


@runtime_checkable
class BaselineAuthorityStore(Protocol):
    """Workspace-bound external authority exposed to Athena by its controller."""

    async def load(self) -> SealedBaseline | None:
        """Load the current sealed generation, if one exists."""

    async def seal(
        self,
        bundle: VerifiedBaselineBundle,
        *,
        expected_generation: int | None,
    ) -> SealedBaseline:
        """Atomically seal exact verified bytes at the expected generation."""

    async def attest_prepare(
        self,
        evidence: PrepareAttestation,
        *,
        expected_generation: int,
    ) -> SealedBaseline:
        """Atomically attach trusted PREPARE evidence and advance generation."""


__all__ = [
    "BaselineAuthorityConflict",
    "BaselineAuthorityError",
    "BaselineAuthorityStore",
    "PrepareAttestation",
    "SealedBaseline",
    "VerifiedBaselineBundle",
]
