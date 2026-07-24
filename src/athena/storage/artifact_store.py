"""不可变大对象的内容寻址存储。

``LocalArtifactStore`` 用 SHA-256 引用数据，以原子替换完成首次写入，并在读取时
校验内容摘要。论文处理器依靠这一边界保存源码、Markdown、RAG chunk、图像与模型
解释；结构化状态只持有短小的 ``ArtifactRef``。
"""

import asyncio
import hashlib
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Protocol

from athena.core.schemas import ArtifactRef

REF_PREFIX = "sha256:"
REF_PATTERN = re.compile(r"^sha256:([0-9a-f]{64})$")


class InvalidArtifactRefError(ValueError):
    """ArtifactRef 不符合 ``sha256:<64 hex>`` 格式。"""


class ArtifactNotFoundError(FileNotFoundError):
    """内容寻址引用在存储中不存在。"""


class ArtifactIntegrityError(OSError):
    """落盘内容与引用中的 SHA-256 不一致。"""


class ArtifactStore(Protocol):
    """论文工具与工作流共享的最小异步 artifact 契约。"""

    async def put_bytes(self, data: bytes) -> ArtifactRef:
        """保存字节并返回稳定内容引用。"""

    async def get_bytes(self, ref: ArtifactRef) -> bytes:
        """读取并校验引用对应的字节。"""

    async def put_text(self, text: str) -> ArtifactRef:
        """按 UTF-8 保存文本。"""

    async def get_text(self, ref: ArtifactRef) -> str:
        """读取 UTF-8 文本。"""


def digest_ref(data: bytes) -> ArtifactRef:
    """计算内容引用；例如 ``digest_ref(b'a')`` 返回 ``sha256:ca978...``。"""
    return REF_PREFIX + hashlib.sha256(data).hexdigest()


def digest_from_ref(ref: ArtifactRef) -> str:
    """校验并取出十六进制摘要；非法引用抛出 ``InvalidArtifactRefError``。"""
    match = REF_PATTERN.fullmatch(ref)
    if not match:
        raise InvalidArtifactRefError(f"Invalid artifact reference: {ref}")
    return match.group(1)


class LocalArtifactStore:
    """本地分片式内容寻址存储。

    文件放在 ``root/ab/cdef...``，同内容写入幂等。临时文件与最终文件位于同一目录，
    因此 ``os.replace`` 保持原子性；并发写入同一内容不会产生半写文件。
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()

    def path_for(self, ref: ArtifactRef) -> Path:
        """返回受校验引用的磁盘路径，主要用于诊断与本地工具集成。"""
        digest = digest_from_ref(ref)
        return self._root / digest[:2] / digest[2:]

    def _put_bytes_sync(self, data: bytes) -> ArtifactRef:
        ref = digest_ref(data)
        path = self.path_for(ref)
        with self._write_lock:
            if path.exists():
                if digest_ref(path.read_bytes()) != ref:
                    raise ArtifactIntegrityError(f"Artifact digest mismatch: {ref}")
                return ref
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".artifact-", dir=path.parent
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.replace(temporary_name, path)
                except PermissionError:
                    # Windows 并发写入已创建目标文件 → 接受摘要一致的现有内容
                    if not path.exists() or digest_ref(path.read_bytes()) != ref:
                        raise
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
        return ref

    def _get_bytes_sync(self, ref: ArtifactRef) -> bytes:
        path = self.path_for(ref)
        try:
            data = path.read_bytes()
        except FileNotFoundError as error:
            # 引用合法但本地内容不存在 → 转换为稳定的存储层异常
            raise ArtifactNotFoundError(f"Artifact not found: {ref}") from error
        if digest_ref(data) != ref:
            raise ArtifactIntegrityError(f"Artifact digest mismatch: {ref}")
        return data

    async def put_bytes(self, data: bytes) -> ArtifactRef:
        """在线程中原子写入字节，避免阻塞异步调度器。"""
        return await asyncio.to_thread(self._put_bytes_sync, data)

    async def get_bytes(self, ref: ArtifactRef) -> bytes:
        """在线程中读取并校验字节。"""
        return await asyncio.to_thread(self._get_bytes_sync, ref)

    async def put_text(self, text: str) -> ArtifactRef:
        """以 UTF-8 保存文本；相同文本始终返回同一引用。"""
        return await self.put_bytes(text.encode("utf-8"))

    async def get_text(self, ref: ArtifactRef) -> str:
        """读取 UTF-8 文本；编码错误直接暴露给调用方。"""
        return (await self.get_bytes(ref)).decode("utf-8")
