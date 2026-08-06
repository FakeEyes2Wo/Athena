"""Test retrieval and pinned model provenance."""

from pathlib import Path
import threading

import pytest
from athena.retrieval.types import PaperRef, HFModelRef
from athena.retrieval import store as retrieval_store
from athena.retrieval.store import Retriever


class _PaperAdapter:
    def __init__(self, name: str, calls: list[str], result: list[PaperRef] | Exception):
        self.name = name
        self._calls = calls
        self._result = result

    async def search(self, query: str) -> list[PaperRef]:
        self._calls.append(self.name)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _HFClient:
    def __init__(self, path: Path):
        self.path = path
        self.calls: list[tuple[str, str, bool]] = []

    def metadata(self, repo: str, revision: str) -> dict[str, str]:
        return {"license": "apache-2.0"}

    def download(self, repo: str, revision: str, *, trust_remote_code: bool) -> Path:
        self.calls.append((repo, revision, trust_remote_code))
        return self.path


@pytest.mark.asyncio
async def test_retriever_returns_list(monkeypatch):
    r = Retriever()

    async def no_papers(_query: str, _n: int) -> list[PaperRef]:
        return []

    monkeypatch.setattr(r, "_search_arxiv", no_papers)
    monkeypatch.setattr(r, "_search_semantic_scholar", no_papers)
    monkeypatch.setattr(r, "_search_web", no_papers)
    papers = await r.search_papers("machine learning classification", n=3)
    assert isinstance(papers, list)
    for p in papers:
        assert isinstance(p, PaperRef)


@pytest.mark.asyncio
async def test_retriever_search_models(monkeypatch):
    r = Retriever()

    async def fake_models(_query: str, _n: int) -> list[HFModelRef]:
        return [HFModelRef(repo="org/model", revision="a1b2c3")]

    monkeypatch.setattr(r, "_search_huggingface", fake_models)
    model_refs = await r.search_models("bert text classification", n=2)
    assert isinstance(model_refs, list)
    for m in model_refs:
        assert isinstance(m, HFModelRef)


def test_paper_ref_fields():
    p = PaperRef(title="Test", source="arxiv", url="https://arxiv.org/abs/9999.99999")
    assert p.source == "arxiv"


@pytest.mark.asyncio
async def test_retrieval_falls_back_in_declared_order() -> None:
    """A recoverable failure must advance through every declared provider."""
    calls: list[str] = []
    papers = await retrieval_store.retrieve_papers(
        "tabular",
        adapters=[
            _PaperAdapter(
                "arxiv",
                calls,
                getattr(retrieval_store, "RecoverableRetrievalError", RuntimeError)(
                    "offline"
                ),
            ),
            _PaperAdapter("semantic_scholar", calls, []),
            _PaperAdapter("web", calls, [PaperRef(title="Web paper", source="web")]),
        ],
    )

    assert calls == ["arxiv", "semantic_scholar", "web"]
    assert papers[0].source == "web"


@pytest.mark.asyncio
async def test_retrieval_does_not_hide_undeclared_errors() -> None:
    calls: list[str] = []

    with pytest.raises(RuntimeError, match="programming error"):
        await retrieval_store.retrieve_papers(
            "tabular",
            adapters=[_PaperAdapter("arxiv", calls, RuntimeError("programming error"))],
        )

    assert calls == ["arxiv"]


@pytest.mark.asyncio
async def test_retrieval_rejects_non_paper_result() -> None:
    with pytest.raises(TypeError, match=r"list\[PaperRef\]"):
        await retrieval_store.retrieve_papers(
            "tabular",
            adapters=[
                _PaperAdapter("arxiv", [], (PaperRef(title="Paper", source="arxiv"),))
            ],
        )


@pytest.mark.asyncio
async def test_retrieval_marks_common_knowledge_fallback() -> None:
    papers = await retrieval_store.retrieve_papers(
        "tabular",
        adapters=[_PaperAdapter("arxiv", [], [])],
    )

    assert [paper.source for paper in papers] == ["common_knowledge"]


@pytest.mark.asyncio
async def test_hf_download_is_revision_pinned(tmp_path: Path) -> None:
    """A download must use its supplied immutable revision and retain provenance."""
    payload = tmp_path / "weights.bin"
    payload.write_bytes(b"model-weights")
    client = _HFClient(payload)

    ref = await retrieval_store.download_model(
        "org/model",
        "a1b2c3",
        client=client,
        put_path=lambda path: f"artifact://{path.name}",
    )

    assert client.calls == [("org/model", "a1b2c3", False)]
    assert ref.revision == "a1b2c3"
    assert ref.license and ref.digest
    assert ref.artifact == "artifact://weights.bin"


@pytest.mark.asyncio
async def test_hf_download_runs_sync_put_path_off_event_loop(tmp_path: Path) -> None:
    payload = tmp_path / "weights.bin"
    payload.write_bytes(b"model-weights")
    client = _HFClient(payload)
    event_loop_thread = threading.get_ident()
    put_path_threads: list[int] = []

    def put_path(path: Path) -> str:
        put_path_threads.append(threading.get_ident())
        return f"artifact://{path.name}"

    await retrieval_store.download_model(
        "org/model",
        "a1b2c3",
        client=client,
        put_path=put_path,
    )

    assert put_path_threads and put_path_threads != [event_loop_thread]
