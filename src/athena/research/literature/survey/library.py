"""跨运行、跨项目的论文库：让第二次调研只为**新论文**付钱。

在此之前每一次调研都从零开始：同一篇论文重新下载、重新转换（含视觉调用）、重新编码。
一次 10 篇的调研实测 13 分 45 秒，其中取源 68s、转换 141s（93 次视觉调用）、索引 29s
（257 批编码）——这三段的输入完全由内容决定，重算一遍不会得到任何新东西。

本模块把这三段的产物按**内容键**存下来。键里带转换器版本与视觉策略，因此改了解析器
或换了策略会自动失效，不需要手动清缓存——那种需要人记得清的缓存迟早会喂回过期结果。

## 为什么库和输出用两个存储

语料索引要落在**调用方给的那个** store 里（loop 用项目目录下那份，命令行用
``~/.athena/artifacts``），否则 ``corpus_ref`` 在宿主里解析不到——这条链路已经为这件事
吃过一次亏。而缓存要跨项目共享才有意义。所以库是独立的一份存储，命中时把索引真正需要
的那几个 blob 复制进目标 store；内容寻址让复制不改变任何 ref。

复制的是**检索需要的文本**，不含图像原件：``load_retrieval_units`` 只读 chunk 正文与
视觉的 ``search_text_ref``，图像字节留在库里即可，把几十兆 PNG 抄进每个项目没有意义。
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path

from athena.core.artifact_store import (
    ArtifactNotFoundError,
    LocalArtifactStore,
    digest_ref,
)
from athena.core.contracts import ArtifactRef, ArtifactStore
from athena.research.literature.paper_markdown.models import PaperContent
from athena.research.literature.paper_rag.index import pack_vectors, unpack_vectors

LIBRARY_ROOT_ENV = "ATHENA_LIBRARY_ROOT"
DEFAULT_LIBRARY_ROOT = Path.home() / ".athena" / "library"

CONVERTER_VERSION = "1"
"""转换产物的缓存代。

改了 ``paper_markdown`` 的解析、切块或质量门禁就把它 +1：缓存键里带上它，旧产物立刻
失效而不必手动清。不带版本的缓存会在解析器改好之后继续喂回旧的坏结果，而那种错误极难
察觉——它看起来只是"这次改动没效果"。
"""

INDEX_VERSION = "1"
"""句向量的缓存代；``paper_rag.index`` 的切句规则变了就 +1。

与转换分开计：切句改了不必重付视觉调用，转换改了才要。
"""

SEPARATOR = "\x1f"
"""缓存键各部件之间的分隔符（ASCII unit separator）。

不能直接相接：``("ab", "c")`` 与 ``("a", "bc")`` 必须是不同的键。也不能用普通标点——
paper id、DOI 与 artifact ref 里什么符号都可能出现，而一次键碰撞的表现是"偶尔拿到别
的论文的正文"，几乎不可能被察觉。
"""


def library_root(root: str | Path = "") -> Path:
    """论文库根目录：显式参数 > ``ATHENA_LIBRARY_ROOT`` > ``~/.athena/library``。"""
    return Path(root or os.environ.get(LIBRARY_ROOT_ENV, "") or DEFAULT_LIBRARY_ROOT)


def cache_key(*parts: str) -> str:
    """把若干部件拼成一个稳定的缓存键。

    分隔符的选择见 ``SEPARATOR``。
    """
    joined = SEPARATOR.join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def conversion_key(
    paper_id: str,
    tex_source_ref: ArtifactRef | None,
    pdf_ref: ArtifactRef | None,
    visual_policy: str,
) -> str:
    """一次转换的内容键。

    源引用本身就是内容散列，所以"同一篇论文的同一份源码"天然是同一个键，不必另外指纹。
    ``visual_policy`` 进键是因为 ``required`` 与 ``best_effort`` 会产出不同的
    ``PaperContent``（前者失败即整篇作废），共用一份缓存会让策略开关静默失灵。
    """
    return cache_key(
        "conversion",
        CONVERTER_VERSION,
        paper_id,
        tex_source_ref or "",
        pdf_ref or "",
        visual_policy,
    )


def vectors_key(paper_content_ref: ArtifactRef, embedding_model: str) -> str:
    """一篇论文句向量的内容键。

    以 ``PaperContent`` 的引用为键：切句规则是确定的，同一份内容切出的句子序列必然相同，
    因此向量可以整篇复用。编码器进键是硬要求——不同模型的向量不在同一个空间里，混用会
    让检索给出看上去正常、实际毫无意义的分数。
    """
    return cache_key("vectors", INDEX_VERSION, paper_content_ref, embedding_model)


def scout_key(request_json: str, scorer: str = "") -> str:
    """一次检索的内容键：整份 ``ScoutRequest`` 的规范 JSON **加上打分器指纹**。

    整份请求进键而不是只取 query——步数、深度、门槛都会改变交付集合。

    但请求本身**不含打分器配置**：模型名与打分遍数都由组合根决定，不在 ``ScoutRequest``
    里。所以它们必须单独进键，否则换了打分器之后同一个查询会被原样重放，改动静默不生效。
    这一条是踩出来的：把打分默认改成两遍取均值之后，库里已有的单遍结果仍然命中，而"没生
    效"和"生效了但没用"在报告上长得一模一样。
    """
    return cache_key("scout", request_json, scorer)


def retrieval_refs(content: PaperContent) -> list[ArtifactRef]:
    """建索引与写报告真正会读到的 artifact 引用。

    刻意不含 ``asset_ref``/``preview_ref`` 这些图像原件：``load_retrieval_units`` 读的是
    视觉单元的 ``search_text_ref``（模型解读出来的可检索文本），图像本身没有下游消费者。
    """
    refs: list[ArtifactRef] = [content.markdown_ref, content.diagnostics_ref]
    for optional in (content.abstract_ref, content.bibliography_ref):
        if optional:
            refs.append(optional)
    for chunk in content.chunks:
        refs.append(chunk.retrieval_text_ref or chunk.content_ref)
        if chunk.retrieval_text_ref and chunk.content_ref:
            refs.append(chunk.content_ref)
    for visual in content.visuals:
        if visual.interpretation_status != "unavailable":
            refs.append(visual.search_text_ref)
        refs.append(visual.interpretation_ref)
    return list(dict.fromkeys(ref for ref in refs if ref))


async def copy_refs(
    source: ArtifactStore, target: ArtifactStore, refs: list[ArtifactRef]
) -> int:
    """把若干 artifact 从一个存储复制到另一个，返回实际复制的条数。

    内容寻址让复制成为幂等操作：目标已有同一内容时 ``put_bytes`` 直接返回同一个 ref。
    源里缺失的引用跳过而不是报错——那表示库被外部清理过，此时应当回退到重新生成，而不
    是让整轮调研失败。
    """
    copied = 0
    for ref in refs:
        try:
            data = await source.get_bytes(ref)
        except (ArtifactNotFoundError, FileNotFoundError):
            # 库被清理或从未存过这一条 → 交由调用方按"未命中"处理
            continue
        await target.put_bytes(data)
        copied += 1
    return copied


class PaperLibrary:
    """键 → artifact 引用的持久映射，外加一份自己的内容寻址存储。

    索引是一个 JSON 文件而不是数据库：条目是"每篇论文一条"的量级，而一个额外的进程外
    依赖会让首次接入多一件要装的东西。写入走原子替换，并发写不会留下半个文件。
    """

    def __init__(self, root: str | Path = "") -> None:
        self._root = library_root(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self.store = LocalArtifactStore(self._root / "artifacts")
        self._index_path = self._root / "index.json"
        self._entries: dict[str, str] = self._load()
        self._lock = asyncio.Lock()
        self.hits = 0
        self.misses = 0

    @property
    def root(self) -> Path:
        """库根目录，用于诊断与报告。"""
        return self._root

    def _load(self) -> dict[str, str]:
        """读索引；文件缺失或损坏时从空表开始。

        损坏不抛错：缓存是纯加速，一个坏掉的索引文件不该让调研跑不起来——重建的代价
        是再付一次钱，而抛错的代价是完全跑不了。
        """
        try:
            payload = json.loads(self._index_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return {
            key: value
            for key, value in payload.items()
            if isinstance(key, str) and isinstance(value, str)
        }

    def _save(self) -> None:
        """原子替换写回索引。"""
        temporary = self._index_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self._entries, ensure_ascii=False, indent=0), encoding="utf-8"
        )
        os.replace(temporary, self._index_path)

    def get(self, key: str) -> ArtifactRef | None:
        """查缓存；命中与未命中都计数，供报告核对"这次到底省了多少"。"""
        found = self._entries.get(key)
        if found is None:
            self.misses += 1
        else:
            self.hits += 1
        return found

    async def put(self, key: str, ref: ArtifactRef) -> None:
        """登记一条缓存并立刻落盘。

        每条都写盘而不是退出时统一写：调研是十几分钟的长流程，中途被打断时已经付过钱
        的那些论文必须留下来——恰恰是被打断的运行最需要缓存。
        """
        async with self._lock:
            if self._entries.get(key) == ref:
                return
            self._entries[key] = ref
            await asyncio.to_thread(self._save)

    async def load_paper(self, key: str, target: ArtifactStore) -> PaperContent | None:
        """取出缓存的 ``PaperContent`` 并把索引需要的 blob 复制进目标存储。

        任何一个 blob 缺失都判为未命中：半份内容比没有内容危险得多——它会建出一个正文
        取不到的语料，而检索层只会安静地返回空结果。
        """
        ref = self.get(key)
        if ref is None:
            return None
        try:
            content = PaperContent.model_validate_json(await self.store.get_text(ref))
        except (ArtifactNotFoundError, FileNotFoundError, ValueError):
            # 库被清理或内容与当前 schema 不兼容 → 当作未命中重新生成
            return None
        refs = retrieval_refs(content)
        copied = await copy_refs(self.store, target, refs)
        if copied != len(refs):
            return None
        await target.put_text(content.model_dump_json())
        return content

    async def save_paper(
        self, key: str, content: PaperContent, source: ArtifactStore
    ) -> ArtifactRef:
        """把一篇论文的转换产物存进库，返回它在库里的引用。"""
        await copy_refs(source, self.store, retrieval_refs(content))
        ref = await self.store.put_text(content.model_dump_json())
        await self.put(key, ref)
        return ref

    async def load_bytes(self, key: str, target: ArtifactStore) -> bytes | None:
        """取出缓存的字节（句向量走这条），顺带复制进目标存储。"""
        ref = self.get(key)
        if ref is None:
            return None
        try:
            data = await self.store.get_bytes(ref)
        except (ArtifactNotFoundError, FileNotFoundError):
            return None
        await target.put_bytes(data)
        return data

    async def save_bytes(self, key: str, data: bytes) -> ArtifactRef:
        """把字节存进库并登记。"""
        ref = await self.store.put_bytes(data)
        await self.put(key, ref)
        return ref

    async def load_text(self, key: str) -> str | None:
        """取出缓存的文本（scout 结果走这条）。"""
        ref = self.get(key)
        if ref is None:
            return None
        try:
            return await self.store.get_text(ref)
        except (ArtifactNotFoundError, FileNotFoundError):
            return None

    async def save_text(self, key: str, text: str) -> ArtifactRef:
        """把文本存进库并登记。"""
        ref = await self.store.put_text(text)
        await self.put(key, ref)
        return ref

    def stats(self) -> dict[str, int]:
        """本次运行的命中账，直接进 ``SurveyReport``。"""
        return {"hits": self.hits, "misses": self.misses, "entries": len(self._entries)}


class LibraryVectorCache:
    """``VectorCache`` 的库实现：按篇存取句向量。

    条数不符一律判未命中并重新编码。向量与句子的一一对应是语义检索唯一的正确性前提，
    而错位**不会报错**——它只会让之后每一次检索都返回错的句子，是这条链路最典型的那种
    "安静地成功"。宁可多付一次编码，也不能交出一份对不齐的矩阵。
    """

    def __init__(self, library: PaperLibrary, store: ArtifactStore, model: str) -> None:
        self._library = library
        self._store = store
        self._model = model

    def _key(self, paper: PaperContent) -> str:
        """以论文内容的规范 JSON 为键：切句从它确定性导出，因此可整篇复用。"""
        return vectors_key(
            digest_ref(paper.model_dump_json().encode("utf-8")), self._model
        )

    async def load(self, paper: PaperContent, start: int, end: int):
        """取回该论文的句向量矩阵；未缓存或条数不符时返回 ``None``。"""
        data = await self._library.load_bytes(self._key(paper), self._store)
        if data is None:
            return None
        matrix = unpack_vectors(data)
        if matrix.ndim != 2 or matrix.shape[0] != end - start:
            # 切句规则变了而 INDEX_VERSION 忘了跟着改 → 当作未命中，别交出错位的矩阵
            return None
        return matrix

    async def save(self, paper: PaperContent, vectors) -> None:
        """存下该论文的句向量矩阵。"""
        await self._library.save_bytes(self._key(paper), pack_vectors(vectors))
