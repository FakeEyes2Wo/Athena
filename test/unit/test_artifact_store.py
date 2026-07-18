"""``athena.storage.LocalArtifactStore`` 的单元测试。"""

import os
import tempfile
import unittest

from athena.storage import LocalArtifactStore


# ====== 导入的包 ======


class LocalArtifactStoreTest(unittest.IsolatedAsyncioTestCase):
    """本地内容寻址存储的读写、幂等与稳定性测试。"""

    async def asyncSetUp(self) -> None:
        """在临时目录上建店。"""
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LocalArtifactStore(os.path.join(self.tmp.name, "artifacts"))

    async def asyncTearDown(self) -> None:
        """清理临时目录。"""
        self.tmp.cleanup()

    async def test_bytes_round_trip(self) -> None:
        """put_bytes/get_bytes 原样往返。"""
        ref = await self.store.put_bytes(b"\x00\x01binary")
        self.assertTrue(ref.startswith("sha256:"))
        self.assertEqual(b"\x00\x01binary", await self.store.get_bytes(ref))

    async def test_text_round_trip(self) -> None:
        """put_text/get_text 以 UTF-8 往返。"""
        ref = await self.store.put_text("héllo 论文")
        self.assertEqual("héllo 论文", await self.store.get_text(ref))

    async def test_content_addressed_and_idempotent(self) -> None:
        """相同内容得同一引用（去重），不同内容得不同引用。"""
        first = await self.store.put_text("same")
        again = await self.store.put_text("same")
        other = await self.store.put_text("different")
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)


if __name__ == "__main__":
    unittest.main()
