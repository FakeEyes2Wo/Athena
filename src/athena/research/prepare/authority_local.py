"""Durable same-user implementation of the controller authority protocol."""

import base64
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Any

from athena.research.prepare.authority import (
    BaselineAuthorityConflict,
    BaselineAuthorityError,
    PrepareAttestation,
    SealedBaseline,
    VerifiedBaselineBundle,
)
from athena.research.prepare.baseline_research import BaselineVerification

_SCHEMA_VERSION = 1

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class LocalBaselineAuthorityStore:
    """Persist one authority generation outside an Agent workspace.

    This protects against accidental process failure and stale writers, not a
    second process running under the same OS account.  The controller chooses
    the root; callers only supply project/session identity for an opaque name.
    """

    gui_authority_mode = "local"
    gui_authority_security_level = "same-user local filesystem"

    def __init__(self, root: Path, project_root: Path, session_id: str) -> None:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a non-empty string")
        self.root = Path(root).expanduser().resolve()
        identity = f"{Path(project_root).expanduser().resolve()}\0{session_id}"
        namespace = sha256(identity.encode("utf-8")).hexdigest()
        self.record_path = self.root / f"{namespace}.json"
        self._lock_path = self.root / f"{namespace}.lock"

    async def load(self) -> SealedBaseline | None:
        """Load the exact sealed baseline, rejecting invalid local records."""
        with self._locked():
            return self._load_locked()

    async def seal(
        self,
        bundle: VerifiedBaselineBundle,
        *,
        expected_generation: int | None,
    ) -> SealedBaseline:
        """Create the next sealed generation only when its predecessor matches."""
        with self._locked():
            current = self._load_locked()
            current_generation = None if current is None else current.generation
            if expected_generation != current_generation:
                raise BaselineAuthorityConflict("baseline generation changed")
            sealed = SealedBaseline(
                generation=0 if current is None else current.generation + 1,
                bundle=bundle,
            )
            self._write_locked(sealed)
            return sealed

    async def attest_prepare(
        self,
        evidence: PrepareAttestation,
        *,
        expected_generation: int,
    ) -> SealedBaseline:
        """Attach PREPARE evidence only to the exact current generation."""
        with self._locked():
            current = self._load_locked()
            if current is None or current.generation != expected_generation:
                raise BaselineAuthorityConflict("baseline generation changed")
            sealed = SealedBaseline(
                generation=current.generation + 1,
                bundle=current.bundle,
                attestation=evidence,
            )
            self._write_locked(sealed)
            return sealed

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _load_locked(self) -> SealedBaseline | None:
        try:
            raw = self.record_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise BaselineAuthorityError(
                "local baseline authority record is unavailable"
            ) from exc
        try:
            record = json.loads(raw)
            return self._sealed_from_record(record)
        except (
            TypeError,
            ValueError,
            json.JSONDecodeError,
            BaselineAuthorityError,
        ) as exc:
            raise BaselineAuthorityError(
                "local baseline authority record is invalid"
            ) from exc

    def _write_locked(self, sealed: SealedBaseline) -> None:
        data = json.dumps(self._record_from_sealed(sealed), sort_keys=True) + "\n"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".authority-", suffix=".json", dir=self.root
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, self.record_path)
        except OSError as exc:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise BaselineAuthorityError(
                "local baseline authority record could not be saved"
            ) from exc

    @staticmethod
    def _record_from_sealed(sealed: SealedBaseline) -> dict[str, Any]:
        bundle = sealed.bundle
        return {
            "schema_version": _SCHEMA_VERSION,
            "generation": sealed.generation,
            "bundle": {
                "research_bytes": base64.b64encode(bundle.research_bytes).decode(
                    "ascii"
                ),
                "design_bytes": base64.b64encode(bundle.design_bytes).decode("ascii"),
                "verification_bytes": base64.b64encode(
                    bundle.verification_bytes
                ).decode("ascii"),
                "verification": bundle.verification.model_dump(mode="json"),
            },
            "attestation": (
                None
                if sealed.attestation is None
                else {
                    "research_sha256": sealed.attestation.research_sha256,
                    "design_sha256": sealed.attestation.design_sha256,
                    "baseline_commit": sealed.attestation.baseline_commit,
                    "evaluator_ref": sealed.attestation.evaluator_ref,
                    "evidence_ref": sealed.attestation.evidence_ref,
                }
            ),
        }

    @staticmethod
    def _sealed_from_record(record: Any) -> SealedBaseline:
        if (
            not isinstance(record, dict)
            or record.get("schema_version") != _SCHEMA_VERSION
        ):
            raise BaselineAuthorityError("unsupported record schema")
        bundle_data = record.get("bundle")
        if not isinstance(bundle_data, dict):
            raise BaselineAuthorityError("bundle is missing")
        verification_data = bundle_data.get("verification")
        if not isinstance(verification_data, dict):
            raise BaselineAuthorityError("verification is missing")
        bundle = VerifiedBaselineBundle(
            research_bytes=LocalBaselineAuthorityStore._decode(
                bundle_data.get("research_bytes"), "research_bytes"
            ),
            design_bytes=LocalBaselineAuthorityStore._decode(
                bundle_data.get("design_bytes"), "design_bytes"
            ),
            verification_bytes=LocalBaselineAuthorityStore._decode(
                bundle_data.get("verification_bytes"), "verification_bytes"
            ),
            verification=BaselineVerification.model_validate(verification_data),
        )
        attestation_data = record.get("attestation")
        if attestation_data is None:
            attestation = None
        elif isinstance(attestation_data, dict):
            attestation = PrepareAttestation(**attestation_data)
        else:
            raise BaselineAuthorityError("attestation is invalid")
        return SealedBaseline(
            generation=record.get("generation"), bundle=bundle, attestation=attestation
        )

    @staticmethod
    def _decode(value: Any, field: str) -> bytes:
        if not isinstance(value, str):
            raise BaselineAuthorityError(f"{field} is invalid")
        try:
            return base64.b64decode(value.encode("ascii"), validate=True)
        except (UnicodeEncodeError, ValueError) as exc:
            raise BaselineAuthorityError(f"{field} is invalid") from exc
