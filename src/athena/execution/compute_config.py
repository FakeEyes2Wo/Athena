"""``[compute]`` 配置：算力从哪来。

**配置里只写 ``~/.ssh/config`` 的 Host 别名**，不写用户名、IP、端口、密钥路径。
连接的复杂度（跳板、端口、密钥、known_hosts）留在唯一有资格管它的地方，Athena
的任何配置文件里因此永远不会出现凭据。

``fallback`` 只有 ``never`` 与 ``ask`` 两个取值，**没有 ``silent``**。拿不到 GPU
就排队或明确失败，绝不偷偷退回本地 CPU 跑完再报一个分数——那种结果不是你要的
实验，而且全链路会是绿的。
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from athena.execution.pool import Placement
from athena.execution.remote.ssh import SshHost

Fallback = Literal["never", "ask"]

DEFAULT_CONFIG_PATH = Path("config.toml")


class ComputeConfigError(ValueError):
    """``[compute]`` 配置本身有问题。

    宁可开跑之前就红，也不要跑到第一个实验才发现主机名写错。
    """


@dataclass(frozen=True, slots=True)
class ComputeConfig:
    """算力配置。``mode == "local"`` 时其余字段无意义。"""

    mode: Literal["local", "ssh"] = "local"
    placement: Placement = "pack"
    fallback: Fallback = "never"
    gpus_per_experiment: int = 1
    queue_timeout_s: float | None = None
    hosts: tuple[SshHost, ...] = field(default_factory=tuple)

    @property
    def remote(self) -> bool:
        """是否走远程算力。"""
        return self.mode == "ssh"


def _require(section: dict, key: str, where: str) -> object:
    if key not in section:
        raise ComputeConfigError(f"{where} is missing required key '{key}'")
    return section[key]


def parse_compute_config(payload: dict | None) -> ComputeConfig:
    """把 ``config.toml`` 的 ``[compute]`` 节解析成配置对象。"""
    if not payload:
        return ComputeConfig()
    mode = str(payload.get("mode", "local"))
    if mode not in ("local", "ssh"):
        raise ComputeConfigError(f"compute.mode must be 'local' or 'ssh', got {mode!r}")
    placement = str(payload.get("placement", "pack"))
    if placement not in ("pack", "spread", "homogeneous"):
        raise ComputeConfigError(
            f"compute.placement must be pack/spread/homogeneous, got {placement!r}"
        )
    fallback = str(payload.get("fallback", "never"))
    if fallback not in ("never", "ask"):
        # 有意不提供 silent：静默降级会产出一个看起来正常、实际不是你要的实验的分数。
        raise ComputeConfigError(
            f"compute.fallback must be 'never' or 'ask', got {fallback!r}; "
            "silent local fallback is not an option by design"
        )

    hosts: list[SshHost] = []
    for index, entry in enumerate(payload.get("hosts") or []):
        where = f"compute.hosts[{index}]"
        if not isinstance(entry, dict):
            raise ComputeConfigError(f"{where} must be a table")
        name = str(_require(entry, "name", where))
        alias = str(_require(entry, "ssh", where))
        if "@" in alias:
            raise ComputeConfigError(
                f"{where}.ssh must be a Host alias from ~/.ssh/config, not "
                f"'user@host' ({alias!r}); connection details belong in ssh_config"
            )
        declared = entry.get("gpus", "auto")
        if declared == "auto":
            gpus: tuple[int, ...] | None = None
        elif isinstance(declared, list) and all(
            isinstance(item, int) for item in declared
        ):
            gpus = tuple(declared)
        else:
            raise ComputeConfigError(
                f'{where}.gpus must be "auto" or a list of GPU indices'
            )
        hosts.append(
            SshHost(
                name=name,
                alias=alias,
                scratch=str(entry.get("scratch", "/scratch/athena")),
                python=str(entry.get("python", "python3")),
                gpus=gpus,
                max_leases=int(entry.get("max_leases", 1)),
                options=tuple(str(item) for item in entry.get("ssh_options") or ()),
            )
        )
    if mode == "ssh" and not hosts:
        raise ComputeConfigError(
            "compute.mode is 'ssh' but no [[compute.hosts]] are configured"
        )
    names = [host.name for host in hosts]
    if len(set(names)) != len(names):
        raise ComputeConfigError(f"duplicate host names in [[compute.hosts]]: {names}")

    timeout = payload.get("queue_timeout_s")
    return ComputeConfig(
        mode=mode,  # type: ignore[arg-type]
        placement=placement,  # type: ignore[arg-type]
        fallback=fallback,  # type: ignore[arg-type]
        gpus_per_experiment=int(payload.get("gpus_per_experiment", 1)),
        queue_timeout_s=None if timeout is None else float(timeout),
        hosts=tuple(hosts),
    )


def load_compute_config(path: Path | None = None) -> ComputeConfig:
    """从 ``config.toml`` 读 ``[compute]``；文件缺失时按本地算力。

    解析错误**不**吞掉：配置写错了要立刻知道，而不是静默按本地跑完一整轮。
    """
    target = path or DEFAULT_CONFIG_PATH
    if not target.is_file():
        return ComputeConfig()
    try:
        payload = tomllib.loads(target.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ComputeConfigError(f"cannot read {target}: {exc}") from exc
    return parse_compute_config(payload.get("compute"))
