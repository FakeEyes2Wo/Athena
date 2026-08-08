"""内容寻址目录 Bundle 测试（设计 data-analysis-agent-workflow §4.2）。"""

import json

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.core.bundle import (
    DirectoryBundle,
    InvalidBundleError,
    OwnershipError,
    StaleParentError,
    UnknownAnalysisError,
    VersionedBundle,
    validate_data_analysis,
)


def _store(tmp_path) -> LocalArtifactStore:
    return LocalArtifactStore(tmp_path / "artifacts")


@pytest.mark.asyncio
async def test_commit_and_read_round_trip(tmp_path) -> None:
    store = _store(tmp_path)
    report_ref = await store.put_text("# 报告")
    fig_ref = await store.put_bytes(b"png-bytes")
    manifest_ref = await DirectoryBundle.commit(
        store, {"report.md": report_ref, "figures/a.png": fig_ref}
    )
    assert manifest_ref.startswith("sha256:")
    files = await DirectoryBundle.files(store, manifest_ref)
    assert files == {"report.md": report_ref, "figures/a.png": fig_ref}
    assert await DirectoryBundle.read(store, manifest_ref, "report.md") == (
        "# 报告".encode("utf-8")
    )


@pytest.mark.asyncio
async def test_parent_ref_round_trip(tmp_path) -> None:
    store = _store(tmp_path)
    v1 = await DirectoryBundle.commit(
        store,
        {
            "report.md": await store.put_text("v1"),
            "figures/a.png": await store.put_bytes(b"a"),
        },
    )
    v2 = await DirectoryBundle.commit(
        store,
        {
            "report.md": await store.put_text("v2"),
            "figures/a.png": await store.put_bytes(b"a"),
        },
        parent_ref=v1,
    )
    manifest = json.loads(await store.get_text(v2))
    assert manifest["parent_ref"] == v1


@pytest.mark.asyncio
async def test_rejects_missing_report(tmp_path) -> None:
    store = _store(tmp_path)
    ref = await DirectoryBundle.commit(
        store, {"figures/a.png": await store.put_bytes(b"a")}
    )
    with pytest.raises(InvalidBundleError):
        await validate_data_analysis(store, ref)


@pytest.mark.asyncio
async def test_rejects_missing_figures(tmp_path) -> None:
    store = _store(tmp_path)
    ref = await DirectoryBundle.commit(store, {"report.md": await store.put_text("x")})
    with pytest.raises(InvalidBundleError):
        await validate_data_analysis(store, ref)


@pytest.mark.asyncio
async def test_versioned_bundle_rejects_non_data_analysis_structure(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    ledger = VersionedBundle(store)
    with pytest.raises(InvalidBundleError):
        await ledger.create("agent_1", {"report.md": await store.put_text("x")})


@pytest.mark.parametrize(
    "bad_path",
    ["../escape.md", "a/../../b.md", "/abs/report.md", "figures//a.png", "", "."],
)
@pytest.mark.asyncio
async def test_rejects_non_normalized_paths(tmp_path, bad_path: str) -> None:
    store = _store(tmp_path)
    files = {
        "report.md": await store.put_text("x"),
        "figures/a.png": await store.put_bytes(b"a"),
    }
    files[bad_path] = await store.put_bytes(b"y")
    with pytest.raises(InvalidBundleError):
        await DirectoryBundle.commit(store, files)


@pytest.mark.asyncio
async def test_read_non_directory_manifest_is_rejected(tmp_path) -> None:
    store = _store(tmp_path)
    not_bundle = await store.put_text(json.dumps({"kind": "file"}))
    with pytest.raises(InvalidBundleError):
        await DirectoryBundle.files(store, not_bundle)


async def _files(store, report_text: str) -> dict:
    return {
        "report.md": await store.put_text(report_text),
        "figures/a.png": await store.put_bytes(b"a"),
    }


@pytest.mark.asyncio
async def test_create_tracks_owner_and_latest(tmp_path) -> None:
    store = _store(tmp_path)
    ledger = VersionedBundle(store)
    analysis_id, v1 = await ledger.create("agent_1", await _files(store, "v1"))
    assert ledger.owner(analysis_id) == "agent_1"
    assert ledger.latest(analysis_id) == v1


@pytest.mark.asyncio
async def test_same_owner_commits_v2(tmp_path) -> None:
    store = _store(tmp_path)
    ledger = VersionedBundle(store)
    analysis_id, v1 = await ledger.create("agent_1", await _files(store, "v1"))
    v2 = await ledger.commit(analysis_id, "agent_1", await _files(store, "v2"), v1)
    assert ledger.latest(analysis_id) == v2
    assert v2 != v1


@pytest.mark.asyncio
async def test_other_owner_rejected(tmp_path) -> None:
    store = _store(tmp_path)
    ledger = VersionedBundle(store)
    analysis_id, v1 = await ledger.create("agent_1", await _files(store, "v1"))
    with pytest.raises(OwnershipError):
        await ledger.commit(analysis_id, "agent_2", await _files(store, "v2"), v1)


@pytest.mark.asyncio
async def test_stale_parent_rejected(tmp_path) -> None:
    store = _store(tmp_path)
    ledger = VersionedBundle(store)
    analysis_id, v1 = await ledger.create("agent_1", await _files(store, "v1"))
    v2 = await ledger.commit(analysis_id, "agent_1", await _files(store, "v2"), v1)
    with pytest.raises(StaleParentError):
        await ledger.commit(analysis_id, "agent_1", await _files(store, "v3"), v1)


@pytest.mark.asyncio
async def test_unknown_analysis_rejected(tmp_path) -> None:
    store = _store(tmp_path)
    ledger = VersionedBundle(store)
    v1 = await DirectoryBundle.commit(store, await _files(store, "v1"))
    with pytest.raises(UnknownAnalysisError):
        await ledger.commit("nope", "agent_1", await _files(store, "v2"), v1)


@pytest.mark.asyncio
async def test_report_image_refs_resolve_within_bundle(tmp_path) -> None:
    store = _store(tmp_path)
    report_ref = await store.put_text("![缺图](figures/missing.png)\n正文")
    fig_ref = await store.put_bytes(b"png")
    manifest_ref = await DirectoryBundle.commit(
        store, {"report.md": report_ref, "figures/missing.png": fig_ref}
    )
    await DirectoryBundle.validate_report_references(store, manifest_ref)  # 不抛


@pytest.mark.asyncio
async def test_report_image_ref_outside_bundle_rejected(tmp_path) -> None:
    store = _store(tmp_path)
    report_ref = await store.put_text("![缺图](figures/absent.png)\n正文")
    fig_ref = await store.put_bytes(b"png")
    manifest_ref = await DirectoryBundle.commit(
        store, {"report.md": report_ref, "figures/other.png": fig_ref}
    )
    with pytest.raises(InvalidBundleError):
        await DirectoryBundle.validate_report_references(store, manifest_ref)


@pytest.mark.asyncio
async def test_report_external_url_and_traversal_refs(tmp_path) -> None:
    store = _store(tmp_path)
    report_ref = await store.put_text(
        "![外链](https://example.com/a.png)![穿越](../secret.png)![绝对](/etc/passwd)"
    )
    fig_ref = await store.put_bytes(b"png")
    manifest_ref = await DirectoryBundle.commit(
        store, {"report.md": report_ref, "figures/a.png": fig_ref}
    )
    # 外部 URL 跳过；../ 与绝对路径必须拒绝
    with pytest.raises(InvalidBundleError):
        await DirectoryBundle.validate_report_references(store, manifest_ref)
