"""算力池与租约：一个 Plan 槽换一份 GPU 租约。"""

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
from athena.execution.remote.mirror import MirroredBackend, WorkspaceMirror
from athena.execution.remote.ssh import SshBackend, SshHost, SshTransport

logger = logging.getLogger(__name__)

Placement = Literal["pack", "spread", "homogeneous"]


class NoComputeAvailable(RuntimeError):
    """池子里没有可用算力。"""


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
    """注册期预检的结果——一台机器"能不能用、能用来干什么"的全部依据。"""

    name: str
    os: str
    hostname: str
    python: str
    packages: dict[str, str]
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
        """写进实验证据的 ``placement`` 块。"""
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
    datasets: set[str] = field(default_factory=set)

    def free_gpus(self) -> list[int]:
        """本机当前空闲的卡号（配置声明过就以声明为准，否则用探测到的）。"""
        if self.card is None:
            return []
        declared = self.host.gpus
        indices = list(declared) if declared else [gpu.index for gpu in self.card.gpus]
        return [index for index in indices if index not in self.busy_gpus]


class GpuPool:
    """若干台裸 SSH 机器上的 GPU 分配。"""

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
        """连上每一台机器问清事实；有任何一台不过就抛错。"""
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
            packages=dict(facts.get("packages") or {}),
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
        """为一个 Plan 取一份租约；池子满时排队等待。"""
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
        # 数据亲和优先于放置策略：没有数据集的机器要先付一次完整分发。
        cold = {
            state.host.name
            for state in candidates
            if self._dataset is not None
            and self._dataset.dataset_id not in state.datasets
        }
        if self._placement == "spread":
            candidates.sort(key=lambda s: (s.host.name in cold, s.leases, s.host.name))
        else:
            candidates.sort(key=lambda s: (s.host.name in cold, -s.leases, s.host.name))
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
        """把数据集送到这台机器（已有就复用），并把 ATHENA_DATA_ROOT 指过去。"""
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
        lease.backend.inner.set_data_root(report.remote_root)
        return report

    async def release(self, plan_id: str) -> None:
        """归还租约：删掉远端工作区、关通道（远端因此清场）、把卡放回池子。"""
        lease = self._leases.pop(plan_id, None)
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
        """删掉这份租约在远端的工作区目录。"""
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
