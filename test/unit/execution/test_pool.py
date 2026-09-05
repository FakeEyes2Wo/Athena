"""算力池与租约。

池子对每台机器起的是真通道（本地子进程跑同一份 agent），只把 GPU 事实换成可控的
假 ``nvidia-smi`` 输出——否则这些用例只能在有卡的机器上跑。除此之外全是真的：
真握手、真预检、真建远端目录、真关通道。

守的两条不变式（设计文档 §五·5、§五·6）：
- 拿不到算力是**错误**，不是一次退回本地 CPU 的降级；
- 硬件必须进证据，否则异构池里的比较不可比而又无从发现。
"""

import asyncio
import sys
from pathlib import Path

import pytest

from athena.execution.pool import (
    GpuPool,
    NoComputeAvailable,
    PreflightError,
)
from athena.execution.remote.channel import (
    RemoteChannel,
    RemoteError,
    SubprocessTransport,
)
from athena.execution.remote.ssh import SshHost
from athena.execution.runtime import CommandRequest


class _FakeGpuTransport(SubprocessTransport):
    """本地 agent + 一份编造的 GPU 清单。"""

    gpus: list[dict] = []

    def __init__(self, host: SshHost) -> None:
        super().__init__(sys.executable)
        self._host = host

    @property
    def description(self) -> str:
        return f"fake:{self._host.name}"


def _gpu(index: int, name: str = "NVIDIA A100") -> dict:
    return {
        "index": index,
        "name": name,
        "memory_total_mib": 81920,
        "memory_used_mib": 0,
        "utilization_pct": 0,
    }


@pytest.fixture
def patched_probe(monkeypatch):
    """把 probe 的 gpus 字段换成用例给的清单。"""
    real_request = RemoteChannel.request
    table: dict[str, list[dict]] = {}

    async def request(self, op: str, **fields):
        reply = await real_request(self, op, **fields)
        if op == "probe" and self.description.startswith("fake:"):
            reply = {**reply, "gpus": table.get(self.description[5:], [])}
        return reply

    monkeypatch.setattr(RemoteChannel, "request", request)
    return table


def _host(name: str, tmp_path: Path, **kwargs) -> SshHost:
    return SshHost(
        name=name, alias=f"{name}.lab", scratch=(tmp_path / name).as_posix(), **kwargs
    )


def _make_pool(hosts: list[SshHost], **kwargs) -> GpuPool:
    return GpuPool(hosts, transport_factory=_FakeGpuTransport, **kwargs)


@pytest.mark.asyncio
async def test_first_acquire_records_the_facts_a_lease_needs(tmp_path, patched_probe):
    host = _host("gpu-01", tmp_path)
    patched_probe["gpu-01"] = [_gpu(0), _gpu(1)]
    pool = _make_pool([host])

    workspace = tmp_path / "ws"
    workspace.mkdir()

    lease = await pool.acquire("h1", local_workspace=workspace)
    try:
        assert lease.card.gpu_model == "NVIDIA A100"
        assert [gpu.index for gpu in lease.card.gpus] == [0, 1]
    finally:
        await pool.release("h1")


@pytest.mark.asyncio
async def test_a_host_without_gpus_never_enters_the_pool(tmp_path, patched_probe):
    """半可用的机器比不可用更糟：它会在第一个实验才失败，那时已经烧掉了 PREPARE。"""
    patched_probe["gpu-01"] = []
    pool = _make_pool([_host("gpu-01", tmp_path)])
    workspace = tmp_path / "ws"
    workspace.mkdir()

    with pytest.raises(PreflightError, match="no GPUs"):
        await pool.acquire("h1", local_workspace=workspace)


@pytest.mark.asyncio
async def test_a_lease_pins_gpus_and_records_where_it_ran(tmp_path, patched_probe):
    patched_probe["gpu-01"] = [_gpu(0), _gpu(1)]
    pool = _make_pool([_host("gpu-01", tmp_path, max_leases=2)])
    workspace = tmp_path / "ws"
    workspace.mkdir()

    lease = await pool.acquire("h1", local_workspace=workspace, gpus=1)
    try:
        assert lease.gpu_ids == (0,)
        assert lease.backend.inner.build_env()["CUDA_VISIBLE_DEVICES"] == "0"

        placement = lease.placement()
        assert placement["host"] == "gpu-01"
        assert placement["gpu_ids"] == [0]
        assert placement["gpu_model"] == "NVIDIA A100"
        assert "queued_seconds" in placement
    finally:
        await pool.release("h1")


@pytest.mark.asyncio
async def test_two_leases_never_share_a_card(tmp_path, patched_probe):
    patched_probe["gpu-01"] = [_gpu(0), _gpu(1)]
    pool = _make_pool([_host("gpu-01", tmp_path, max_leases=2)])
    workspace = tmp_path / "ws"
    workspace.mkdir()

    first = await pool.acquire("h1", local_workspace=workspace)
    second = await pool.acquire("h2", local_workspace=workspace)
    try:
        assert set(first.gpu_ids).isdisjoint(second.gpu_ids)
    finally:
        await pool.release("h1")
        await pool.release("h2")


@pytest.mark.asyncio
async def test_an_exhausted_pool_raises_instead_of_falling_back(
    tmp_path, patched_probe
):
    """这条是第五节第 5 条不变式。

    偷偷退回本地 CPU 会跑完、会出分、日志也齐——但那不是你要的实验，而且没有
    任何一层会报错。和 corpus_ref 两臂都是 None 是同一类事故。
    """
    patched_probe["gpu-01"] = [_gpu(0)]
    pool = _make_pool([_host("gpu-01", tmp_path, max_leases=1)])
    workspace = tmp_path / "ws"
    workspace.mkdir()

    held = await pool.acquire("h1", local_workspace=workspace)
    try:
        with pytest.raises(NoComputeAvailable):
            await pool.acquire("h2", local_workspace=workspace, timeout_s=0.5)
    finally:
        await pool.release("h1")
    assert held.gpu_ids == (0,)


@pytest.mark.asyncio
async def test_a_queued_plan_gets_the_card_when_it_is_returned(tmp_path, patched_probe):
    """排队是可见的等待，不是失败——归还之后必须自动醒来。"""
    patched_probe["gpu-01"] = [_gpu(0)]
    pool = _make_pool([_host("gpu-01", tmp_path, max_leases=1)])
    workspace = tmp_path / "ws"
    workspace.mkdir()

    await pool.acquire("h1", local_workspace=workspace)
    waiting = asyncio.create_task(
        pool.acquire("h2", local_workspace=workspace, timeout_s=30)
    )
    await asyncio.sleep(0.2)
    assert not waiting.done()

    await pool.release("h1")
    lease = await asyncio.wait_for(waiting, timeout=30)
    try:
        assert lease.gpu_ids == (0,)
        assert lease.placement()["queued_seconds"] > 0
    finally:
        await pool.release("h2")


@pytest.mark.asyncio
async def test_homogeneous_placement_refuses_a_different_gpu_model(
    tmp_path, patched_probe
):
    """同一次比较的各臂必须同型号，否则墙钟受限实验的分数不可比。

    冲突时同构优先：等待只是慢，异构是结果不可比。
    """
    patched_probe["a100"] = [_gpu(0, "NVIDIA A100")]
    patched_probe["rtx"] = [_gpu(0, "NVIDIA RTX 3090")]
    pool = _make_pool(
        [_host("a100", tmp_path), _host("rtx", tmp_path)], placement="homogeneous"
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()

    first = await pool.acquire("arm-a", local_workspace=workspace)
    try:
        assert first.card.gpu_model == "NVIDIA A100"
        with pytest.raises(NoComputeAvailable):
            await pool.acquire(
                "arm-b",
                local_workspace=workspace,
                same_model_as="NVIDIA A100",
                timeout_s=0.5,
            )
    finally:
        await pool.release("arm-a")


@pytest.mark.asyncio
async def test_releasing_a_lease_closes_its_channel(tmp_path, patched_probe):
    """归还 = 关通道；远端 stdin 因此 EOF，它自己把进程组清掉。"""
    patched_probe["gpu-01"] = [_gpu(0)]
    pool = _make_pool([_host("gpu-01", tmp_path)])
    workspace = tmp_path / "ws"
    workspace.mkdir()

    lease = await pool.acquire("h1", local_workspace=workspace)
    channel = lease.backend.inner._channel
    await channel.request("probe")

    await pool.release("h1")
    with pytest.raises(RemoteError):
        await channel.request("probe")


@pytest.mark.asyncio
async def test_releasing_a_lease_takes_its_remote_workspace_with_it(
    tmp_path, patched_probe
):
    """归还也要把远端工作区删掉，否则 scratch 是一条无界的磁盘泄漏。

    每个跑完的 Plan 在远端留一份工作区副本，而同一块盘还得装数据集。真机上确认
    过这个泄漏是真的。盘满的表现是"实验莫名其妙失败"，指不到原因。

    删得起是因为该留的都已经在控制节点：源码经镜像回到本地 worktree，产出经
    ``collect_outputs`` 回来了。
    """
    patched_probe["gpu-01"] = [_gpu(0)]
    pool = _make_pool([_host("gpu-01", tmp_path)])
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "train.py").write_text("print(1)\n", encoding="utf-8")

    lease = await pool.acquire("h1", local_workspace=workspace)
    result = await lease.backend.run(
        workspace_root=workspace,
        request=CommandRequest(
            argv=[sys.executable, "-c", "pass"],
            workdir=workspace,
            timeout_s=60,
        ),
    )
    assert result.ok
    remote_workspace = Path(lease.remote_workspace)
    assert (remote_workspace / "train.py").is_file(), "先确认真有东西可删"

    await pool.release("h1")

    assert not remote_workspace.exists()
    assert not remote_workspace.parent.exists(), "整个租约目录都该走，不止 workspace"


@pytest.mark.asyncio
async def test_a_lease_can_actually_run_a_command_in_its_workspace(
    tmp_path, patched_probe
):
    """端到端：租约拿到手就该能跑东西，工作区在远端已经建好。"""
    patched_probe["gpu-01"] = [_gpu(0)]
    pool = _make_pool([_host("gpu-01", tmp_path)])
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "hello.py").write_text("print('from the lease')\n", encoding="utf-8")

    lease = await pool.acquire("h1", local_workspace=workspace)
    try:
        result = await lease.backend.run(
            workspace_root=workspace,
            request=CommandRequest(
                argv=[sys.executable, "hello.py"],
                workdir=workspace,
                timeout_s=60,
            ),
        )
        assert result.ok, result.stderr
        assert "from the lease" in result.stdout
    finally:
        await pool.release("h1")


# 数据分发：一台机器一份，且租约拿到的是分发完成的那个目录


def _dataset(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "train.csv").write_bytes(b"id,y" + chr(10).encode() + b"1,0")
    return root


@pytest.mark.asyncio
async def test_a_lease_points_ATHENA_DATA_ROOT_at_the_staged_copy(
    tmp_path, patched_probe
):
    """agent 猜不到内容寻址目录，也不该知道——它只认 ATHENA_DATA_ROOT。"""
    patched_probe["gpu-01"] = [_gpu(0)]
    data = _dataset(tmp_path / "dataset")
    pool = _make_pool([_host("gpu-01", tmp_path)], dataset_root=data)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    lease = await pool.acquire("h1", local_workspace=workspace)
    try:
        assert lease.dataset is not None
        assert not lease.dataset.reused
        staged = lease.backend.inner.build_env()["ATHENA_DATA_ROOT"]
        assert staged == lease.dataset.remote_root
        assert (Path(staged) / "train.csv").is_file()
        assert lease.placement()["dataset"]["id"] == lease.dataset.dataset_id
    finally:
        await pool.release("h1")

    # 归还会删掉租约目录——但数据集是机器级共享物，只被 pin 住，绝不能跟着走。
    # 跟着走的话，下一个 Plan 要重付一次完整分发，大数据集下这一笔以小时计。
    assert (Path(lease.dataset.remote_root) / "train.csv").is_file()


@pytest.mark.asyncio
async def test_the_second_lease_on_a_host_reuses_the_staged_dataset(
    tmp_path, patched_probe
):
    """分发是按机器摊销的，不是按 Plan 重复付的。"""
    patched_probe["gpu-01"] = [_gpu(0), _gpu(1)]
    data = _dataset(tmp_path / "dataset")
    pool = _make_pool([_host("gpu-01", tmp_path, max_leases=2)], dataset_root=data)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    first = await pool.acquire("h1", local_workspace=workspace)
    second = await pool.acquire("h2", local_workspace=workspace)
    try:
        assert first.dataset is not None and not first.dataset.reused
        assert second.dataset is not None and second.dataset.reused
        assert second.dataset.bytes_sent == 0
    finally:
        await pool.release("h1")
        await pool.release("h2")


@pytest.mark.asyncio
async def test_placement_prefers_a_host_that_already_has_the_data(
    tmp_path, patched_probe
):
    """数据亲和：落到冷机上要先付一次完整分发，大数据集下这一笔以小时计。

    这里用 spread（本会挑负载更低的那台）验证亲和确实压过了放置策略本身。
    """
    patched_probe["gpu-01"] = [_gpu(0), _gpu(1)]
    patched_probe["gpu-02"] = [_gpu(0), _gpu(1)]
    data = _dataset(tmp_path / "dataset")
    pool = _make_pool(
        [
            _host("gpu-01", tmp_path, max_leases=2),
            _host("gpu-02", tmp_path, max_leases=2),
        ],
        placement="spread",
        dataset_root=data,
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()

    # 先让 gpu-01 上有数据，并且**占着一份租约**——这样 spread 会明确倾向 gpu-02
    # （负载 0 < 1）。亲和压不住策略的话，第二份租约就会落到 gpu-02 上。
    first = await pool.acquire("h1", local_workspace=workspace)
    assert first.card.name == "gpu-01"
    second = await pool.acquire("h2", local_workspace=workspace)
    try:
        assert second.card.name == "gpu-01", "数据亲和必须压过 spread 的负载均衡"
        assert second.dataset is not None and second.dataset.reused
    finally:
        await pool.release("h1")
        await pool.release("h2")


@pytest.mark.asyncio
async def test_a_released_lease_frees_the_card_for_the_next_plan(
    tmp_path, patched_probe
):
    """结算即归还。等到 runtime 关闭才还的话，每个跑完的 Plan 都还占着一张卡，
    池子会在第 N 个实验上无谓地耗尽——而表现是"排队排不到"，指不到真正的原因。
    """
    patched_probe["gpu-01"] = [_gpu(0)]
    pool = _make_pool([_host("gpu-01", tmp_path, max_leases=1)])
    workspace = tmp_path / "ws"
    workspace.mkdir()

    for plan_id in ("h1", "h2", "h3"):
        lease = await pool.acquire(plan_id, local_workspace=workspace, timeout_s=5)
        assert lease.gpu_ids == (0,)
        await pool.release(plan_id)
