"""算力池与租约：一个 Plan 槽换一份 GPU 租约。

粒度是 **Plan，不是命令**（设计文档 §4.2）。一个 Plan 拿到租约后，它的全部命令
——``shell_command`` 与 manifest——都落在同一台机器、同一个目录；否则工作区状态
会在两台机器之间分叉，而这种分叉不报错，只是结果不对。

裸 SSH 机器上 Athena 自己就是调度器：没有 Slurm 替它管卡，所以要自己探测
（``nvidia-smi``）、自己分配、并用 ``CUDA_VISIBLE_DEVICES`` 强制。

两条不肯让步的规则：

1. **绝不静默降级。** 拿不到租约就排队或明确失败，绝不偷偷退回本地 CPU 跑完
   再报一个分数。那种"结果"不是你要的实验，而且全链路会是绿的——和 corpus_ref
   两臂都是 None 是同一类事故。
2. **硬件进证据。** A 臂拿 A100、B 臂拿 3090，在墙钟受限的实验里分数不可比；
   不记下来就无从发现。``Lease.placement()`` 就是那份记录。
"""

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from athena.core.contracts import ArtifactStore
from athena.execution.remote.channel import RemoteChannel
from athena.execution.remote.dataset import (
    DatasetSpec,
    DatasetStager,
    StageReport,
    describe_dataset,
)
from athena.execution.remote.mirror import WorkspaceMirror
from athena.execution.remote.mirrored import MirroredBackend
from athena.execution.remote.ssh import SshBackend, SshHost, SshTransport

logger = logging.getLogger(__name__)

Placement = Literal["pack", "spread", "homogeneous"]


class NoComputeAvailable(RuntimeError):
    """池子里没有可用算力。

    它必须是一个**错误**，不是一次降级。见模块文档第 1 条。
    """


class PreflightError(RuntimeError):
    """一台机器没通过注册期预检。"""


@dataclass(frozen=True, slots=True)
class GpuCard:
    """一张卡的事实。"""

    index: int
    name: str
    memory_total_mib: int


@dataclass(frozen=True, slots=True)
class HostCard:
    """注册期预检的结果——一台机器"能不能用、能用来干什么"的全部依据。

    这类系统最典型的浪费是：跑了两小时的 PREPARE，在第一个实验才发现远端没装 uv。
    所以预检在注册时做，一次问清，不过就当场红。
    """

    name: str
    os: str
    hostname: str
    python: str
    uv: str | None
    gpus: tuple[GpuCard, ...]

    @property
    def gpu_model(self) -> str:
        """本机 GPU 型号（异构约束按它比较）；无卡时为空串。"""
        return self.gpus[0].name if self.gpus else ""


@dataclass(slots=True)
class Lease:
    """一个 Plan 对一台机器上若干张卡的独占声明。"""

    plan_id: str
    host: SshHost
    card: HostCard
    gpu_ids: tuple[int, ...]
    backend: MirroredBackend
    remote_workspace: str
    queued_seconds: float = 0.0
    dataset: StageReport | None = None

    def placement(self) -> dict[str, Any]:
        """写进实验证据的 ``placement`` 块。

        没有这一段，异构池里的比较就是不可比而又无从发现的。
        """
        names = {gpu.index: gpu.name for gpu in self.card.gpus}
        block: dict[str, Any] = {
            "host": self.host.name,
            "hostname": self.card.hostname,
            "os": self.card.os,
            "gpu_ids": list(self.gpu_ids),
            "gpu_model": self.card.gpu_model,
            "gpus": [
                {"index": index, "name": names.get(index, "")} for index in self.gpu_ids
            ],
            "remote_workspace": self.remote_workspace,
            "queued_seconds": round(self.queued_seconds, 3),
            "remote_only_paths": list(self.backend.remote_only),
        }
        if self.dataset is not None:
            block["dataset"] = {
                "id": self.dataset.dataset_id,
                "root": self.dataset.remote_root,
                "reused": self.dataset.reused,
                "bytes_sent": self.dataset.bytes_sent,
            }
        return block


@dataclass(slots=True)
class _HostState:
    """池子对一台机器的记账。"""

    host: SshHost
    card: HostCard | None = None
    busy_gpus: set[int] = field(default_factory=set)
    leases: int = 0
    # 本机已完整分发过的数据集 id。放置策略靠它做数据亲和：落到没有数据的机器上
    # 要先付一次完整分发，大数据集下这一笔以小时计。
    datasets: set[str] = field(default_factory=set)

    def free_gpus(self) -> list[int]:
        if self.card is None:
            return []
        declared = self.host.gpus
        indices = list(declared) if declared else [gpu.index for gpu in self.card.gpus]
        return [index for index in indices if index not in self.busy_gpus]


class GpuPool:
    """若干台裸 SSH 机器上的 GPU 分配。

    只记账，**不隔离**：共享机器上别的用户随时能在"你的"卡上起进程，Athena
    拦不住。缓解手段是把观测到的争用写进证据，让不可比的比较可被发现，而不是
    假装没发生（设计文档 §九·1、§九·9）。
    """

    def __init__(
        self,
        hosts: list[SshHost],
        *,
        placement: Placement = "pack",
        store: ArtifactStore | None = None,
        dataset_root: Path | None = None,
        transport_factory=None,
    ) -> None:
        if not hosts:
            raise ValueError("compute pool needs at least one host")
        self._states = {host.name: _HostState(host) for host in hosts}
        self._placement = placement
        self._dataset_root = Path(dataset_root) if dataset_root is not None else None
        self._dataset: DatasetSpec | None = None
        self._store = store
        self._transport_factory = transport_factory or SshTransport
        self._leases: dict[str, Lease] = {}
        self._channels: dict[str, RemoteChannel] = {}
        self._lock = asyncio.Lock()
        self._freed = asyncio.Condition(self._lock)

    @property
    def hosts(self) -> tuple[str, ...]:
        """池子里的主机名。"""
        return tuple(self._states)

    def cards(self) -> dict[str, HostCard]:
        """已预检过的主机卡片。"""
        return {
            name: state.card
            for name, state in self._states.items()
            if state.card is not None
        }

    async def preflight(self) -> dict[str, HostCard]:
        """连上每一台机器问清事实；有任何一台不过就抛错。

        不过的机器一律不进池子——半可用的机器比不可用更糟：它会在第一个实验
        里失败，而那时已经烧掉了 PREPARE 的时间。
        """
        problems: list[str] = []
        for name, state in self._states.items():
            try:
                state.card = await self._probe(state.host)
            except Exception as exc:
                problems.append(f"{name}: {exc}")
        if problems:
            raise PreflightError("; ".join(problems))
        return self.cards()

    async def _probe(self, host: SshHost) -> HostCard:
        channel = RemoteChannel(self._transport_factory(host))
        try:
            # 事实随握手一起拿到（见 RemoteChannel.open）：预检看到的和实验期
            # 注入给 agent 的必须是同一份，否则预检就不是预检。
            facts = await channel.open()
        finally:
            await channel.close()
        if not facts.get("python"):
            raise PreflightError("no python3 on the host")
        gpus = tuple(
            GpuCard(
                index=int(gpu["index"]),
                name=str(gpu["name"]),
                memory_total_mib=int(gpu["memory_total_mib"]),
            )
            for gpu in facts.get("gpus") or []
        )
        if not gpus:
            raise PreflightError("nvidia-smi reported no GPUs")
        return HostCard(
            name=host.name,
            os=str(facts.get("os", "")),
            hostname=str(facts.get("hostname", "")),
            python=str(facts["python"]),
            uv=facts.get("uv"),
            gpus=gpus,
        )

    async def acquire(
        self,
        plan_id: str,
        *,
        local_workspace: Path,
        gpus: int = 1,
        same_model_as: str | None = None,
        timeout_s: float | None = None,
    ) -> Lease:
        """为一个 Plan 取一份租约；池子满时排队等待。

        ``same_model_as`` 是异构池里的同构约束：同一次比较的各臂必须落在同型号
        硬件上，否则墙钟受限实验的分数不可比。**冲突时同构优先**——分发/等待只是
        慢，异构是结果不可比。
        """
        loop = asyncio.get_running_loop()
        started = loop.time()
        async with self._lock:
            while True:
                choice = self._select(gpus, same_model_as)
                if choice is not None:
                    state, gpu_ids = choice
                    state.busy_gpus.update(gpu_ids)
                    state.leases += 1
                    break
                if timeout_s is not None and loop.time() - started >= timeout_s:
                    raise NoComputeAvailable(
                        f"no free GPU for plan {plan_id} after {timeout_s}s "
                        f"(hosts: {', '.join(self.hosts)})"
                    )
                logger.info("plan %s is queued for a GPU lease", plan_id)
                try:
                    await asyncio.wait_for(self._freed.wait(), timeout=timeout_s)
                except asyncio.TimeoutError:
                    raise NoComputeAvailable(
                        f"no free GPU for plan {plan_id} after {timeout_s}s"
                    ) from None

        try:
            lease = await self._open_lease(
                plan_id, state, tuple(gpu_ids), local_workspace
            )
            lease.dataset = await self._stage_dataset(state, lease)
        except Exception:
            async with self._lock:
                state.busy_gpus.difference_update(gpu_ids)
                state.leases -= 1
                self._freed.notify_all()
            raise
        lease.queued_seconds = loop.time() - started
        self._leases[plan_id] = lease
        return lease

    def _select(
        self, gpus: int, same_model_as: str | None
    ) -> tuple[_HostState, list[int]] | None:
        """挑一台机器与若干张卡；挑不到返回 None。"""
        candidates = [
            state
            for state in self._states.values()
            if state.card is not None
            and state.leases < state.host.max_leases
            and len(state.free_gpus()) >= gpus
            and (same_model_as is None or state.card.gpu_model == same_model_as)
        ]
        if not candidates:
            return None
        # 数据亲和优先于放置策略：落到没有数据的机器上要先付一次完整分发，大数据集
        # 下这一笔以小时计，而 pack/spread 的差别只是几个百分点的利用率。
        # 与 homogeneous 不冲突——同型号的过滤已经在上面的候选筛选里做完了，这里只在
        # 同型号的机器之间按「有没有数据」排序（分发只是慢，异构是结果不可比）。
        cold = (
            (lambda state: self._dataset.dataset_id not in state.datasets)
            if self._dataset is not None
            else (lambda state: False)
        )
        if self._placement == "spread":
            candidates.sort(key=lambda s: (cold(s), s.leases, s.host.name))
        else:
            # pack / homogeneous：先把一台机器用满，留出整台空机给需要多卡的实验。
            candidates.sort(key=lambda s: (cold(s), -s.leases, s.host.name))
        chosen = candidates[0]
        return chosen, chosen.free_gpus()[:gpus]

    async def _open_lease(
        self,
        plan_id: str,
        state: _HostState,
        gpu_ids: tuple[int, ...],
        local_workspace: Path,
    ) -> Lease:
        host = state.host
        scratch = PurePosixPath(host.scratch)
        remote_workspace = str(scratch / "leases" / plan_id / "workspace")
        remote_data_root = str(scratch / "data")

        channel = RemoteChannel(self._transport_factory(host))
        await channel.open()
        self._channels[plan_id] = channel
        inner = SshBackend(
            host,
            channel=channel,
            remote_workspace=remote_workspace,
            remote_data_root=remote_data_root,
            gpu_ids=gpu_ids,
            store=self._store,
        )
        inner.bind_local_root(local_workspace)
        await inner.prepare_remote()
        mirror = WorkspaceMirror(
            channel, local_root=local_workspace, remote_root=remote_workspace
        )
        assert state.card is not None
        return Lease(
            plan_id=plan_id,
            host=host,
            card=state.card,
            gpu_ids=gpu_ids,
            backend=MirroredBackend(inner, mirror),
            remote_workspace=remote_workspace,
        )

    async def _stage_dataset(
        self, state: _HostState, lease: Lease
    ) -> StageReport | None:
        """把数据集送到这台机器（已有就复用），并把 ATHENA_DATA_ROOT 指过去。

        一台机器一份，不是一个 Plan 一份：数据是不可变共享物，租约只 pin 它。
        """
        if self._dataset_root is None:
            return None
        if self._dataset is None:
            self._dataset = describe_dataset(self._dataset_root)
        stager = DatasetStager(
            lease.backend.inner.channel,
            data_root=str(PurePosixPath(state.host.scratch) / "data"),
        )
        report = await stager.stage(self._dataset_root, self._dataset)
        state.datasets.add(report.dataset_id)
        # 分发到的是内容寻址目录，agent 只能靠 ATHENA_DATA_ROOT 找到它——
        # 它既猜不到这个路径，也不该知道。
        lease.backend.inner.set_data_root(report.remote_root)
        return report

    async def release(self, plan_id: str) -> None:
        """归还租约：删掉远端工作区、关通道（远端因此清场）、把卡放回池子。"""
        lease = self._leases.pop(plan_id, None)
        self._channels.pop(plan_id, None)
        if lease is None:
            return
        try:
            await self._discard_workspace(lease)
            await lease.backend.aclose()
        finally:
            async with self._lock:
                state = self._states[lease.host.name]
                state.busy_gpus.difference_update(lease.gpu_ids)
                state.leases = max(0, state.leases - 1)
                self._freed.notify_all()

    async def _discard_workspace(self, lease: Lease) -> None:
        """删掉这份租约在远端的工作区目录。

        不删就是一条无界的磁盘泄漏：每个跑完的 Plan 在 scratch 上留一份工作区
        副本，而同一块盘还要装数据集。真机上确认过——``release`` 之后
        ``<scratch>/leases/h1/`` 原封不动。盘满的表现是"实验莫名其妙失败"，
        而且指不到原因。

        删得起，是因为控制节点这边已经有全部该留的东西：源码经镜像回到了本地
        worktree（可信修订据此提交），产出经 ``collect_outputs`` 回来了。**留在
        远端的大文件**（checkpoint、特征缓存）会跟着一起没——所以这里把它们逐条
        记进日志，而不是让它们悄悄消失。证据里的 ``remote_only_paths`` 说的是
        "产出过但没取回"，不是"还能去拿"。

        必须在关通道**之前**做：通道一关就没有人能执行这条删除了。
        """
        discarded = lease.backend.remote_only
        if discarded:
            logger.info(
                "lease %s discards %d remote-only path(s) with its workspace: %s",
                lease.plan_id,
                len(discarded),
                ", ".join(discarded[:10]),
            )
        lease_root = str(PurePosixPath(lease.remote_workspace).parent)
        try:
            await lease.backend.inner.channel.request("remove", paths=[lease_root])
        except Exception as exc:
            # 清不掉不该毁掉归还：卡必须回到池子里，否则一次网络抖动就少一张卡。
            logger.warning(
                "could not remove remote workspace %s on %s: %s",
                lease_root,
                lease.host.name,
                exc,
            )

    async def aclose(self) -> None:
        """归还所有租约。"""
        for plan_id in list(self._leases):
            await self.release(plan_id)
