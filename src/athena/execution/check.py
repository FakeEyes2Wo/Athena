"""算力自检：连上每一台机器，把它**实际**长什么样打出来。

存在的理由很实际：这类系统最典型的浪费是「跑了两小时 PREPARE，在第一个实验才
发现远端跑不了」。在真机上验证这套东西时，四个缺陷里有三个本可以在一条命令里
当场现形——`Python: missing`、PATH 里没有解释器、`uv` 建议指向一个空目录。所以
这里刻意不只报「连上了」，而是把**注入给 agent 的那段 Runtime 原文**一起打出来：
它是最容易悄悄说谎的一处，而它说谎的代价是 agent 按错的前提写一整轮代码。

顺带回答两个只有站在机器上才知道的问题：scratch 还剩不剩得下一份数据集，以及
有没有上一轮崩溃留下、再没人会清的租约目录（正常归还会自己删掉，见
``GpuPool._discard_workspace``）。
"""

import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from athena.execution.compute_config import ComputeConfig
from athena.execution.remote.channel import RemoteChannel
from athena.execution.remote.dataset import DatasetSpec
from athena.execution.remote.ssh import SshBackend, SshHost, SshTransport


@dataclass(frozen=True, slots=True)
class ScratchUsage:
    """scratch 下某个根目录的占用情况。"""

    root: str
    exists: bool
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
    ok: bool
    error: str = ""
    facts: dict[str, Any] = field(default_factory=dict)
    runtime_block: str = ""
    leases: ScratchUsage | None = None
    datasets: ScratchUsage | None = None

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
        """所有机器都可用才算通过。

        半可用的池子比不可用更糟：它会在第一个实验里失败，而那时 PREPARE 的时间
        已经烧掉了。
        """
        return bool(self.hosts) and all(host.ok for host in self.hosts)


async def _usage(channel: RemoteChannel, root: str) -> ScratchUsage:
    reply = await channel.request("space", root=root)
    return ScratchUsage(
        root=root,
        exists=bool(reply.get("exists")),
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
        return HostCheck(name=host.name, alias=host.alias, ok=False, error=str(exc))
    try:
        scratch = PurePosixPath(host.scratch)
        leases = await _usage(channel, str(scratch / "leases"))
        datasets = await _usage(channel, str(scratch / "data"))
        # 渲染 Runtime 块时按「租到第一张卡」来展示——没有卡的话这台机器进不了池子，
        # 而有卡时 agent 看到的就是这一段。
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
        return HostCheck(
            name=host.name,
            alias=host.alias,
            ok=bool(facts.get("python")) and bool(facts.get("gpus")),
            error=_shortfall(facts),
            facts=dict(facts),
            runtime_block=backend.describe(host.scratch),
            leases=leases,
            datasets=datasets,
        )
    except Exception as exc:
        # 一台机器上的意外不该让整轮自检没有输出——池子里其余机器的结论同样有用，
        # 而这条命令的全部意义就是"一次问清所有机器"。
        return HostCheck(
            name=host.name,
            alias=host.alias,
            ok=False,
            error=str(exc),
            facts=dict(facts),
        )
    finally:
        await channel.close()


def _shortfall(facts: dict[str, Any]) -> str:
    """机器连上了但用不了时，说清楚缺什么。"""
    if not facts.get("python"):
        return "no python on the host"
    if not facts.get("gpus"):
        return "nvidia-smi reported no GPUs"
    return ""


async def check_compute(
    config: ComputeConfig,
    *,
    dataset_root: Path | None = None,
    transport_factory=SshTransport,
) -> ComputeCheck:
    """按配置逐台机器自检。**不占用任何卡，也不留下任何东西。**"""
    dataset = None
    if dataset_root is not None:
        from athena.execution.remote.dataset import describe_dataset

        dataset = describe_dataset(dataset_root)
    hosts = [await _check_host(host, transport_factory) for host in config.hosts]
    return ComputeCheck(config=config, hosts=tuple(hosts), dataset=dataset)


# ------------------------------------------------------------------ 打印


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
        # 正常归还会自己删掉租约目录。还留着的，来自崩掉/被 kill 的那一轮。
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
        # 半可用的池子不算通过：不过的机器会在第一个实验里失败，而那时 PREPARE
        # 的时间已经烧掉了。
        print("有机器不可用——这些机器不会进池子。")
    return 0 if check.ok else 1
