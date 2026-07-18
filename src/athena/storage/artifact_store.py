"""不可变大对象的统一存储层（最小内容寻址实现）。

该层保存原始/处理后数据、schema、切分清单、运行配置、日志、模型、预测、diff、
评估结果、长上下文和报告，并向调用方返回稳定 ``ArtifactRef``。写入后内容不可被
原地覆盖；派生产物必须以新的引用表达。

本实现采用内容寻址：``ArtifactRef = "sha256:<hexdigest>"``。同一内容恒得同一引用，
天然满足"不可原地覆盖"，并让跨 Session 去重与缓存免费成立（如同一篇 PDF 不会重复
落盘）。远程或异步后端日后可实现相同的 ``ArtifactStore`` 接口替换本地实现。
"""

import hashlib
import os
from typing import Protocol

from athena.core.schemas import ArtifactRef


# ====== 导入的包 ======

# ArtifactRef 前缀，标明内容寻址所用算法。
REF_PREFIX = "sha256:"

# ======


class ArtifactStore(Protocol):
    """工具与工作流交接大对象所依赖的最小异步存储接口。

    输入输出均为字节或文本与 ``ArtifactRef``；工具（如 PDF->Markdown 适配器）据此
    在不把大对象塞进 Agent 消息的前提下交接请求与结果。
    """

    async def put_bytes(self, data: bytes) -> ArtifactRef:
        """写入字节并返回稳定引用。"""

    async def get_bytes(self, ref: ArtifactRef) -> bytes:
        """按引用读回字节。"""

    async def put_text(self, text: str) -> ArtifactRef:
        """写入 UTF-8 文本并返回稳定引用。"""

    async def get_text(self, ref: ArtifactRef) -> str:
        """按引用读回 UTF-8 文本。"""


class LocalArtifactStore:
    """基于本地文件系统的内容寻址 ArtifactStore。

    输入：一个根目录。``put_*`` 返回 ``"sha256:<hex>"`` 引用，内容落在 ``root/<hex>``；
    相同内容重复写入是幂等的，永不原地覆盖不同内容。
    示例：
        store = LocalArtifactStore("./artifacts")
        ref = await store.put_text("hello")      # -> "sha256:2cf24d…"
        text = await store.get_text(ref)          # -> "hello"
    """

    def __init__(self, root: str) -> None:
        # 存储根目录；不存在时创建。
        self._root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, ref: ArtifactRef) -> str:
        """把引用解析为磁盘路径（put 与 get 共用）。"""
        return os.path.join(self._root, ref.removeprefix(REF_PREFIX))

    async def put_bytes(self, data: bytes) -> ArtifactRef:
        """写入字节并返回内容寻址引用；同内容幂等，已存在则跳过写入。"""
        ref = REF_PREFIX + hashlib.sha256(data).hexdigest()
        path = self._path(ref)
        if not os.path.exists(path):
            with open(path, "wb") as handle:
                handle.write(data)
        return ref

    async def get_bytes(self, ref: ArtifactRef) -> bytes:
        """按引用读回字节。"""
        with open(self._path(ref), "rb") as handle:
            return handle.read()

    async def put_text(self, text: str) -> ArtifactRef:
        """写入 UTF-8 文本，语义同 put_bytes。"""
        return await self.put_bytes(text.encode("utf-8"))

    async def get_text(self, ref: ArtifactRef) -> str:
        """按引用读回 UTF-8 文本。"""
        return (await self.get_bytes(ref)).decode("utf-8")
