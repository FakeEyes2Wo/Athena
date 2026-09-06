"""Content-addressed ArtifactStore tests."""

import asyncio
import tempfile
import unittest

from athena.core.artifact_store import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    InvalidArtifactRefError,
    LocalArtifactStore,
)


class LocalArtifactStoreTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = LocalArtifactStore(self.temporary.name)

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_bytes_and_text_round_trip(self) -> None:
        bytes_ref = await self.store.put_bytes(b"\x00paper")
        text_ref = await self.store.put_text("论文 text")

        self.assertEqual(b"\x00paper", await self.store.get_bytes(bytes_ref))
        self.assertEqual("论文 text", await self.store.get_text(text_ref))

    async def test_content_addressing_is_idempotent_under_concurrency(self) -> None:
        refs = await asyncio.gather(*(self.store.put_bytes(b"same") for _ in range(20)))

        self.assertEqual(1, len(set(refs)))
        self.assertTrue(self.store.path_for(refs[0]).is_file())

    async def test_rejects_invalid_reference_before_path_resolution(self) -> None:
        with self.assertRaises(InvalidArtifactRefError):
            await self.store.get_bytes("sha256:../../outside")

    async def test_missing_and_corrupt_artifacts_are_distinct(self) -> None:
        missing = "sha256:" + "0" * 64
        with self.assertRaises(ArtifactNotFoundError):
            await self.store.get_bytes(missing)

        ref = await self.store.put_bytes(b"valid")
        self.store.path_for(ref).write_bytes(b"corrupt")
        with self.assertRaises(ArtifactIntegrityError):
            await self.store.get_bytes(ref)
