"""论文库的回归用例：第二次调研只该为**新论文**付钱。

这些用例盯的是三件必须成立的事：命中时不再调模型、键在该失效时失效、以及**半份内容
一律判未命中**。最后一条是这里最重要的：一份缺了正文 blob 的语料不会报错，只会让检索
安静地返回空结果，而那正是这条链路反复吃亏的那种失败形态。
"""

import tempfile
import unittest
from pathlib import Path

import numpy

from athena.core.artifact_store import LocalArtifactStore
from athena.research.literature.paper_markdown.models import (
    PaperChunk,
    PaperContent,
    PaperProvenance,
    PaperVisual,
    SourceLocator,
)
from athena.research.literature.survey.library import (
    LibraryVectorCache,
    PaperLibrary,
    conversion_key,
    copy_refs,
    library_root,
    retrieval_refs,
    scout_key,
    vectors_key,
)


def _store() -> LocalArtifactStore:
    return LocalArtifactStore(tempfile.mkdtemp(prefix="lib_store_"))


def _library() -> PaperLibrary:
    return PaperLibrary(tempfile.mkdtemp(prefix="lib_root_"))


async def _content(store: LocalArtifactStore, *, visual: bool = False) -> PaperContent:
    """造一篇最小但结构完整的论文，所有 blob 都真实落在 ``store`` 里。"""
    locator = SourceLocator(
        source_kind="tex", file="main.tex", line_start=1, line_end=2
    )
    chunk = PaperChunk(
        chunk_id="chunk-1",
        kind="paragraph",
        content_ref=await store.put_text("body of the chunk"),
        retrieval_text_ref=await store.put_text("retrieval body of the chunk"),
        heading_path=["Introduction"],
        char_start=0,
        char_end=17,
        token_estimate=4,
        locators=[locator],
    )
    visuals = []
    if visual:
        visuals.append(
            PaperVisual(
                visual_id="fig-1",
                kind="figure",
                element_id="e1",
                locator=locator,
                caption="A figure",
                search_text_ref=await store.put_text("figure searchable text"),
                interpretation_ref=await store.put_text("{}"),
                interpretation_status="interpreted",
            )
        )
    return PaperContent(
        paper_id="arxiv:1234.5678",
        title="A Paper",
        provenance=PaperProvenance(
            source_kind="tex",
            source_ref="sha256:" + "0" * 64,
            source_fingerprint="fp",
            converter="paper_markdown/1",
        ),
        markdown_ref=await store.put_text("# A Paper\n\nbody"),
        diagnostics_ref=await store.put_text("[]"),
        chunks=[chunk],
        visuals=visuals,
    )


class CacheKeyTest(unittest.TestCase):
    def test_the_same_source_bytes_give_the_same_conversion_key(self) -> None:
        """源引用本身就是内容散列，所以"同一份源码"天然是同一个键。"""
        left = conversion_key("arxiv:1", "sha256:a", None, "best_effort")
        right = conversion_key("arxiv:1", "sha256:a", None, "best_effort")

        self.assertEqual(left, right)

    def test_the_visual_policy_is_part_of_the_key(self) -> None:
        """``required`` 与 ``best_effort`` 产出不同的 PaperContent；共用一份缓存会让
        策略开关静默失灵。"""
        strict = conversion_key("arxiv:1", "sha256:a", None, "required")
        lenient = conversion_key("arxiv:1", "sha256:a", None, "best_effort")

        self.assertNotEqual(strict, lenient)

    def test_parts_cannot_run_together_into_a_colliding_key(self) -> None:
        """``("ab","c")`` 与 ``("a","bc")`` 必须不同——碰撞的表现是"拿到别的论文的正文"。"""
        self.assertNotEqual(
            conversion_key("ab", "c", None, "p"), conversion_key("a", "bc", None, "p")
        )

    def test_the_embedding_model_is_part_of_the_vector_key(self) -> None:
        """不同模型的向量不在同一个空间里，混用会给出看上去正常、实际无意义的分数。"""
        self.assertNotEqual(
            vectors_key("sha256:a", "model-one"), vectors_key("sha256:a", "model-two")
        )

    def test_the_whole_scout_request_enters_the_key(self) -> None:
        """步数与门槛都会改变交付集合，漏掉任何一个都会让缓存把 A 的结果当成 B 的。"""
        self.assertNotEqual(
            scout_key('{"query":"x","max_steps":6}'),
            scout_key('{"query":"x","max_steps":8}'),
        )


class RetrievalRefsTest(unittest.IsolatedAsyncioTestCase):
    async def test_every_ref_the_index_reads_is_listed(self) -> None:
        """漏掉任何一条，缓存命中就会建出一个正文取不到的语料。"""
        store = _store()
        content = await _content(store, visual=True)

        refs = retrieval_refs(content)

        self.assertIn(content.markdown_ref, refs)
        self.assertIn(content.chunks[0].retrieval_text_ref, refs)
        self.assertIn(content.chunks[0].content_ref, refs)
        self.assertIn(content.visuals[0].search_text_ref, refs)

    async def test_unavailable_visuals_contribute_no_search_text(self) -> None:
        """未被解释的视觉单元根本不进语料，为它复制正文只是浪费。"""
        store = _store()
        content = await _content(store, visual=True)
        content.visuals[0].interpretation_status = "unavailable"

        self.assertNotIn(content.visuals[0].search_text_ref, retrieval_refs(content))


class PaperRoundTripTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_cached_paper_comes_back_with_its_text_in_the_target_store(
        self,
    ) -> None:
        """库和输出是两个存储；命中必须把索引要读的 blob 复制过去。"""
        source, target = _store(), _store()
        library = _library()
        content = await _content(source, visual=True)
        key = conversion_key("arxiv:1234.5678", "sha256:a", None, "best_effort")

        await library.save_paper(key, content, source)
        restored = await library.load_paper(key, target)

        self.assertIsNotNone(restored)
        self.assertEqual(content.paper_id, restored.paper_id)
        units = await restored.load_retrieval_units(target)
        self.assertEqual(2, len(units))
        self.assertIn("retrieval body", units[0].text)

    async def test_a_miss_returns_none_rather_than_an_empty_paper(self) -> None:
        library = _library()

        self.assertIsNone(await library.load_paper("never-stored", _store()))
        self.assertEqual(1, library.misses)

    async def test_half_a_paper_is_a_miss_not_a_silent_empty_corpus(self) -> None:
        """库被清理掉一个 blob 之后，宁可重转一遍也不能交出取不到正文的语料。"""
        source, target = _store(), _store()
        library = _library()
        content = await _content(source)
        key = conversion_key("arxiv:1234.5678", "sha256:a", None, "best_effort")
        await library.save_paper(key, content, source)
        library.store.path_for(content.chunks[0].retrieval_text_ref).unlink()

        self.assertIsNone(await library.load_paper(key, target))

    async def test_the_index_survives_a_new_library_instance(self) -> None:
        """缓存的价值全在跨进程；只活在进程内等于没有。"""
        source, target = _store(), _store()
        root = tempfile.mkdtemp(prefix="lib_root_")
        content = await _content(source)
        key = conversion_key("arxiv:1234.5678", "sha256:a", None, "best_effort")
        await PaperLibrary(root).save_paper(key, content, source)

        restored = await PaperLibrary(root).load_paper(key, target)

        self.assertIsNotNone(restored)

    async def test_a_corrupt_index_file_degrades_to_empty_instead_of_failing(
        self,
    ) -> None:
        """缓存是纯加速：一个坏掉的索引文件不该让调研跑不起来。"""
        root = Path(tempfile.mkdtemp(prefix="lib_root_"))
        (root / "index.json").write_text("{not json", encoding="utf-8")

        library = PaperLibrary(root)

        self.assertEqual(0, library.stats()["entries"])


class VectorCacheTest(unittest.IsolatedAsyncioTestCase):
    async def test_vectors_round_trip_as_a_float32_matrix(self) -> None:
        source = _store()
        library = _library()
        content = await _content(source)
        cache = LibraryVectorCache(library, source, "model-one")
        block = numpy.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=numpy.float32)

        await cache.save(content, block)
        restored = await cache.load(content, 0, 2)

        self.assertIsNotNone(restored)
        numpy.testing.assert_allclose(block, restored)

    async def test_a_row_count_mismatch_is_a_miss_not_a_misaligned_matrix(self) -> None:
        """向量与句子的一一对应是语义检索唯一的正确性前提，而错位不会报错。"""
        source = _store()
        library = _library()
        content = await _content(source)
        cache = LibraryVectorCache(library, source, "model-one")
        await cache.save(content, numpy.zeros((2, 3), dtype=numpy.float32))

        self.assertIsNone(await cache.load(content, 0, 5))

    async def test_switching_the_embedder_invalidates_the_cache(self) -> None:
        source = _store()
        library = _library()
        content = await _content(source)
        await LibraryVectorCache(library, source, "model-one").save(
            content, numpy.zeros((2, 3), dtype=numpy.float32)
        )

        other = LibraryVectorCache(library, source, "model-two")

        self.assertIsNone(await other.load(content, 0, 2))


class CopyRefsTest(unittest.IsolatedAsyncioTestCase):
    async def test_copying_is_idempotent_and_keeps_the_ref_identical(self) -> None:
        """内容寻址让复制不改变任何 ref——这正是库/输出两个存储能配合的前提。"""
        source, target = _store(), _store()
        ref = await source.put_text("hello")

        self.assertEqual(1, await copy_refs(source, target, [ref]))
        self.assertEqual(1, await copy_refs(source, target, [ref]))
        self.assertEqual("hello", await target.get_text(ref))

    async def test_a_missing_ref_is_skipped_and_reported_by_the_count(self) -> None:
        source, target = _store(), _store()
        present = await source.put_text("hello")
        absent = "sha256:" + "0" * 64

        self.assertEqual(1, await copy_refs(source, target, [present, absent]))


class LibraryRootTest(unittest.TestCase):
    def test_an_explicit_root_beats_the_environment(self) -> None:
        self.assertEqual(Path("/tmp/x"), library_root("/tmp/x"))

    def test_the_default_lives_outside_any_project(self) -> None:
        """库要跨项目共享才有意义，因此不能落在某个 checkout 里。"""
        self.assertIn(".athena", str(library_root()))


if __name__ == "__main__":
    unittest.main()


class ScoutKeyTest(unittest.TestCase):
    """打分器配置不在 ScoutRequest 里，却会改变交付集合——必须单独进键。"""

    def test_changing_the_pass_count_invalidates_the_cached_search(self) -> None:
        """把打分从 1 遍改成 2 遍之后，库里的旧结果不能再命中。

        这一条是踩出来的：改完默认值之后同一个查询仍然命中缓存，多遍打分静默不发生，
        而"没生效"和"生效了但没用"在报告上长得一模一样。
        """
        request = '{"query":"x","max_steps":6}'

        self.assertNotEqual(
            scout_key(request, "flash/1"), scout_key(request, "flash/2")
        )

    def test_changing_the_scorer_model_invalidates_it_too(self) -> None:
        request = '{"query":"x","max_steps":6}'

        self.assertNotEqual(scout_key(request, "flash/2"), scout_key(request, "plus/2"))

    def test_the_same_request_and_scorer_still_hit(self) -> None:
        request = '{"query":"x","max_steps":6}'

        self.assertEqual(scout_key(request, "flash/2"), scout_key(request, "flash/2"))
