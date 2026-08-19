"""数据集分发：能不能**证明**送过去的那份是完整的。

这一组守的是本设计里最阴的一个失败模式：

> 半个数据集。脚本照跑、分数照出、只是少了一半样本，没有任何一层会报错。

所以断言的重点不是"传成功了"，而是几种残缺状态各自会不会被抓住：
中断续传、少一个文件、内容被改、完成标记缺失。
"""

import json
import sys
from pathlib import Path

import pytest

from athena.execution.remote.channel import RemoteChannel, SubprocessTransport
from athena.execution.remote.dataset import (
    COMPLETE_MARKER,
    DatasetError,
    DatasetStager,
    describe_dataset,
)


@pytest.fixture
async def channel():
    channel = RemoteChannel(SubprocessTransport(sys.executable))
    await channel.open()
    try:
        yield channel
    finally:
        await channel.close()


def _dataset(root: Path, *, rows: int = 3) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "train.csv").write_bytes(
        ("id,y" + chr(10) + chr(10).join(f"{i},{i % 2}" for i in range(rows))).encode()
    )
    (root / "test.csv").write_bytes(b"id" + chr(10).encode() + b"1" + chr(10).encode())
    (root / "meta").mkdir(exist_ok=True)
    (root / "meta" / "schema.json").write_bytes(b'{"target": "y"}')
    return root


def test_the_id_is_content_addressed(tmp_path) -> None:
    """同一份数据在任何机器上都指向同一个远端目录——换控制节点不会重传。"""
    a = describe_dataset(_dataset(tmp_path / "a"))
    b = describe_dataset(_dataset(tmp_path / "b"))
    assert a.dataset_id == b.dataset_id

    (tmp_path / "b" / "extra.csv").write_bytes(b"x")
    assert describe_dataset(tmp_path / "b").dataset_id != a.dataset_id


def test_an_empty_dataset_is_an_error(tmp_path) -> None:
    """空目录几乎总是路径写错。让它在开跑前红，而不是在第一次 read_csv 时。"""
    (tmp_path / "empty").mkdir()
    with pytest.raises(DatasetError, match="no files"):
        describe_dataset(tmp_path / "empty")


@pytest.mark.asyncio
async def test_staging_copies_everything_and_marks_it_complete(channel, tmp_path):
    local = _dataset(tmp_path / "data")
    spec = describe_dataset(local)
    stager = DatasetStager(channel, data_root=(tmp_path / "remote").as_posix())

    report = await stager.stage(local, spec)

    assert not report.reused
    assert set(report.uploaded) == {"train.csv", "test.csv", "meta/schema.json"}
    remote = Path(report.remote_root)
    assert (remote / "meta" / "schema.json").read_bytes() == b'{"target": "y"}'
    marker = json.loads((remote / COMPLETE_MARKER).read_text(encoding="utf-8"))
    assert marker["dataset_id"] == spec.dataset_id
    assert marker["files"] == 3


@pytest.mark.asyncio
async def test_a_second_lease_reuses_what_is_already_there(channel, tmp_path):
    """一台机器一份，不是一个 Plan 一份。大数据集下这一笔以小时计。"""
    local = _dataset(tmp_path / "data")
    spec = describe_dataset(local)
    stager = DatasetStager(channel, data_root=(tmp_path / "remote").as_posix())

    await stager.stage(local, spec)
    again = await stager.stage(local, spec)

    assert again.reused
    assert again.uploaded == ()
    assert again.bytes_sent == 0


@pytest.mark.asyncio
async def test_an_interrupted_transfer_resumes_instead_of_restarting(channel, tmp_path):
    """中断后重来只补缺的那些——大数据集下"从头再来"是不可接受的。"""
    local = _dataset(tmp_path / "data")
    spec = describe_dataset(local)
    remote_root = tmp_path / "remote"
    stager = DatasetStager(channel, data_root=remote_root.as_posix())

    # 模拟传到一半就断了：一个文件在，完成标记没有。
    partial = remote_root / spec.dataset_id
    partial.mkdir(parents=True)
    (partial / "train.csv").write_bytes((local / "train.csv").read_bytes())

    report = await stager.stage(local, spec)

    assert not report.reused
    assert "train.csv" not in report.uploaded, "已经对上的文件不该重传"
    assert set(report.uploaded) == {"test.csv", "meta/schema.json"}


@pytest.mark.asyncio
async def test_a_mismatch_at_upload_time_never_gets_a_complete_marker(
    channel, tmp_path
):
    """传上去的东西和清单对不上时，绝不能盖完成章。

    真实触发方式：分发在飞的时候有人动了本地数据。校验必须发生在写标记**之前**，
    顺序反过来就等于给"内容不对的那份"发了通行证，而且之后每次都会被当成完好复用。
    """
    local = _dataset(tmp_path / "data")
    spec = describe_dataset(local)
    # 清单已经算好，此刻本地文件被改：上传的字节将与 spec 对不上。
    (local / "test.csv").write_bytes(b"tampered" + chr(10).encode())
    stager = DatasetStager(channel, data_root=(tmp_path / "remote").as_posix())

    with pytest.raises(DatasetError, match="content differs"):
        await stager.stage(local, spec)

    # 没有完成标记 = 不算数；下次会重新分发，而不是把坏数据当好数据复用。
    assert not (Path(stager.remote_root(spec)) / COMPLETE_MARKER).exists()
    assert await stager.staged_ids() == set()


@pytest.mark.asyncio
async def test_a_missing_file_under_a_complete_marker_is_an_error(channel, tmp_path):
    """标记在、文件少了（有人手删、磁盘满过）——必须报错，不能照常跑。

    这是"半个数据集"最真实的发生方式，也是这一整套内容寻址存在的理由。
    """
    local = _dataset(tmp_path / "data")
    spec = describe_dataset(local)
    stager = DatasetStager(channel, data_root=(tmp_path / "remote").as_posix())
    report = await stager.stage(local, spec)

    (Path(report.remote_root) / "test.csv").unlink()

    with pytest.raises(DatasetError, match="missing test.csv"):
        await stager.stage(local, spec)


@pytest.mark.asyncio
async def test_altered_content_under_a_complete_marker_is_an_error(channel, tmp_path):
    """字节被改过也要抓住：大小可能一样，哈希不会。"""
    local = _dataset(tmp_path / "data")
    spec = describe_dataset(local)
    stager = DatasetStager(channel, data_root=(tmp_path / "remote").as_posix())
    report = await stager.stage(local, spec)

    target = Path(report.remote_root) / "meta" / "schema.json"
    target.write_bytes(b'{"target": "Y"}')  # 同样长度，内容不同

    with pytest.raises(DatasetError, match="content differs"):
        await stager.stage(local, spec)


@pytest.mark.asyncio
async def test_without_the_marker_the_data_does_not_count_as_staged(channel, tmp_path):
    """没有完成标记就当作不存在——宁可重传，不可少读。"""
    local = _dataset(tmp_path / "data")
    spec = describe_dataset(local)
    stager = DatasetStager(channel, data_root=(tmp_path / "remote").as_posix())
    report = await stager.stage(local, spec)

    (Path(report.remote_root) / COMPLETE_MARKER).unlink()
    assert await stager.staged_ids() == set()

    again = await stager.stage(local, spec)
    assert not again.reused
    assert again.uploaded == (), "内容都在，只是要重新盖章"


@pytest.mark.asyncio
async def test_eviction_never_touches_a_pinned_dataset(channel, tmp_path):
    """被活跃租约用着的数据集不得回收，否则实验会在半路失去输入。"""
    remote_root = tmp_path / "remote"
    stager = DatasetStager(channel, data_root=remote_root.as_posix())
    kept = describe_dataset(_dataset(tmp_path / "kept"))
    stale = describe_dataset(_dataset(tmp_path / "stale", rows=9))
    await stager.stage(tmp_path / "kept", kept)
    await stager.stage(tmp_path / "stale", stale)
    assert await stager.staged_ids() == {kept.dataset_id, stale.dataset_id}

    removed = await stager.evict(keep={kept.dataset_id})

    assert removed == (stale.dataset_id,)
    assert await stager.staged_ids() == {kept.dataset_id}
    assert (remote_root / kept.dataset_id / "train.csv").is_file()
