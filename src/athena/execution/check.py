"""算力自检：连上每一台机器，把它**实际**长什么样打出来。"""

import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from athena.execution.compute_config import ComputeConfig
from athena.execution.remote.channel import RemoteChannel
from athena.execution.remote.dataset import DatasetSpec, describe_dataset
from athena.execution.remote.ssh import SshBackend, SshHost, SshTransport


@dataclass(frozen=True, slots=True)
class ScratchUsage:
    """scratch 下某个根目录的占用情况。"""

    root: str
    total: int
    free: int
    entries: tuple[dict[str, Any], ...] = ()

    @property
    def used(self) -> int:
        """这棵子树自己占的字节数。"""
        return sum(int(entry.get("bytes", 0)) for entry in self.entries)


@dataclass(frozen=True, slots=True)
class HostCheck:
    """一台机器的自检结果。"""

    name: str
    alias: str
    error: str = ""
    facts: dict[str, Any] = field(default_factory=dict)
    runtime_block: str = ""
    leases: ScratchUsage | None = None
    datasets: ScratchUsage | None = None

    @property
    def ok(self) -> bool:
        """Whether the host passed every diagnostic."""
        return not self.error

    @property
    def gpus(self) -> tuple[dict[str, Any], ...]:
        """远端报上来的 GPU 列表。"""
        return tuple(self.facts.get("gpus") or ())


@dataclass(frozen=True, slots=True)
class ComputeCheck:
    """一次算力自检的全部结果。"""

    config: ComputeConfig
    hosts: tuple[HostCheck, ...] = ()
    dataset: DatasetSpec | None = None

    @property
    def ok(self) -> bool:
        """所有机器都可用才算通过。"""
        return bool(self.hosts) and all(host.ok for host in self.hosts)


async def _usage(channel: RemoteChannel, root: str) -> ScratchUsage:
    reply = await channel.request("space", root=root)
    return ScratchUsage(
        root=root,
        total=int(reply.get("total", 0)),
        free=int(reply.get("free", 0)),
        entries=tuple(reply.get("entries") or ()),
    )


async def _check_host(host: SshHost, transport_factory) -> HostCheck:
    """连一台机器，问清事实，并渲染出 agent 会看到的那段 Runtime。"""
    channel = RemoteChannel(transport_factory(host))
    try:
        facts = await channel.open()
    except Exception as exc:
        return HostCheck(name=host.name, alias=host.alias, error=str(exc))
    try:
        scratch = PurePosixPath(host.scratch)
        leases = await _usage(channel, str(scratch / "leases"))
        datasets = await _usage(channel, str(scratch / "data"))
        gpu_ids = tuple(
            int(gpu["index"]) for gpu in (facts.get("gpus") or [])[:1] if "index" in gpu
        )
        backend = SshBackend(
            host,
            channel=channel,
            remote_workspace=str(scratch / "leases" / "<plan>" / "workspace"),
            remote_data_root=str(scratch / "data" / "<dataset>"),
            gpu_ids=gpu_ids,
        )
        if not facts.get("python"):
            shortfall = "no python on the host"
        elif not facts.get("gpus"):
            shortfall = "nvidia-smi reported no GPUs"
        else:
            shortfall = ""
        return HostCheck(
            name=host.name,
            alias=host.alias,
            error=shortfall,
            facts=dict(facts),
            runtime_block=backend.describe(host.scratch),
            leases=leases,
            datasets=datasets,
        )
    except Exception as exc:
        return HostCheck(
            name=host.name,
            alias=host.alias,
            error=str(exc),
            facts=dict(facts),
        )
    finally:
        await channel.close()


async def check_compute(
    config: ComputeConfig,
    *,
    dataset_root: Path | None = None,
    transport_factory=SshTransport,
) -> ComputeCheck:
    """按配置逐台机器自检。"""
    dataset = None
    if dataset_root is not None:
        dataset = describe_dataset(dataset_root)
    hosts = [await _check_host(host, transport_factory) for host in config.hosts]
    return ComputeCheck(config=config, hosts=tuple(hosts), dataset=dataset)


def _human(size: float) -> str:
    """字节数转成人看的单位。"""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def _age(mtime: float) -> str:
    """离现在多久（远端自己的时钟对自己的文件，跨机比较的那些坑不适用）。"""
    seconds = max(0.0, time.time() - mtime)
    if seconds < 3600:
        return f"{seconds / 60:.0f} 分钟前"
    if seconds < 86400:
        return f"{seconds / 3600:.0f} 小时前"
    return f"{seconds / 86400:.0f} 天前"


def _print_host(host: HostCheck, dataset: DatasetSpec | None) -> None:
    mark = "OK  " if host.ok else "FAIL"
    print(f"\n[{mark}] {host.name}  (ssh 别名 {host.alias})")
    if not host.facts:
        print(f"       连不上: {host.error}")
        return
    if host.error:
        print(f"       不可用: {host.error}")

    facts = host.facts
    print(f"       主机      {facts.get('hostname', '?')}  {facts.get('os', '?')}")
    print(
        f"       Python    {facts.get('python', '?')}  "
        f"{facts.get('python_executable', '?')}"
    )
    for gpu in host.gpus:
        print(
            f"       GPU {gpu.get('index')}     {gpu.get('name')}  "
            f"{gpu.get('memory_total_mib')} MiB  "
            f"已用 {gpu.get('memory_used_mib')} MiB  "
            f"利用率 {gpu.get('utilization_pct')}%"
        )
    if not host.gpus:
        print("       GPU       （没有；这台机器不会进池子）")

    _print_scratch(host, dataset)
    print("       注入给 agent 的 Runtime 块：")
    for line in host.runtime_block.splitlines():
        print(f"         {line}")


def _print_scratch(host: HostCheck, dataset: DatasetSpec | None) -> None:
    datasets, leases = host.datasets, host.leases
    if datasets is not None:
        print(
            f"       磁盘      剩余 {_human(datasets.free)} / "
            f"共 {_human(datasets.total)}"
        )
        if dataset is not None:
            staged = any(
                entry.get("name") == dataset.dataset_id for entry in datasets.entries
            )
            if staged:
                verdict = "已分发过，本次复用"
            elif datasets.free > dataset.total_bytes * 2:
                verdict = "空间够"
            else:
                verdict = "**空间可能不够**"
            print(
                f"       数据集    {_human(dataset.total_bytes)}"
                f"（{len(dataset.entries)} 个文件）→ {verdict}"
            )
        elif datasets.entries:
            print(
                f"       已分发    {len(datasets.entries)} 份，"
                f"共 {_human(datasets.used)}"
            )
    if leases is not None and leases.entries:
        print(
            f"       遗留租约  {len(leases.entries)} 个，共 {_human(leases.used)}"
            "（上一轮没正常归还；可以直接删）"
        )
        for entry in leases.entries[:5]:
            print(
                f"                 {leases.root}/{entry.get('name')}  "
                f"{_human(int(entry.get('bytes', 0)))}  "
                f"{_age(float(entry.get('mtime', 0.0)))}"
            )


def print_compute_check(check: ComputeCheck) -> int:
    """把自检结果打给人看；有任何一台不可用就返回非零。"""
    config = check.config
    print("算力自检（config.toml 的 [compute]）")
    print(
        f"  模式 {config.mode}   放置 {config.placement}   降级 {config.fallback}   "
        f"每个实验 {config.gpus_per_experiment} 卡"
    )
    if not config.remote:
        print("\n算力在本机，没有远端主机要检查。")
        print(
            "  改成远程：config.toml 里写 [[compute.hosts]]，或跑时加 --compute ssh。"
        )
        return 0

    for host in check.hosts:
        _print_host(host, check.dataset)

    usable = sum(1 for host in check.hosts if host.ok)
    cards = sum(len(host.gpus) for host in check.hosts if host.ok)
    print(f"\n可用 {usable}/{len(check.hosts)} 台，共 {cards} 张卡。")
    if not check.ok:
        print("有机器不可用——这些机器不会进池子。")
    return 0 if check.ok else 1
