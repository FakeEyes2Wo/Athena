"""``Athena-cli compute --check``：开跑之前把机器实际长什么样问清楚。

它存在的理由，是真机上抓出来的四个缺陷里有三个本可以在一条命令里当场现形：
``Python: missing``、PATH 里没有解释器、依赖建议指向一个空目录。所以这一组
断言的重点不是"连上了"，而是**报出来的东西是不是真的**——尤其是那段会被原样
注入给 agent 的 Runtime 块。

"远端"仍由本地子进程跑同一份 agent 扮演，GPU 事实用假 ``nvidia-smi`` 输出注入。
"""

import sys
from pathlib import Path

import pytest

from athena.execution.check import ComputeCheck, check_compute, print_compute_check
from athena.execution.compute_config import ComputeConfig
from athena.execution.remote.channel import RemoteChannel, SubprocessTransport
from athena.execution.remote.ssh import SshHost


class _FakeTransport(SubprocessTransport):
    def __init__(self, host: SshHost) -> None:
        super().__init__(sys.executable)
        self._host = host

    @property
    def description(self) -> str:
        return f"fake:{self._host.name}"


@pytest.fixture
def fake_gpus(monkeypatch):
    real_request = RemoteChannel.request
    table: dict[str, list[dict]] = {}

    async def request(self, op: str, **fields):
        reply = await real_request(self, op, **fields)
        if op == "probe" and self.description.startswith("fake:"):
            reply = {**reply, "gpus": table.get(self.description[5:], [])}
        return reply

    monkeypatch.setattr(RemoteChannel, "request", request)
    return table


def _gpu(index: int, name: str = "NVIDIA A100") -> dict:
    return {
        "index": index,
        "name": name,
        "memory_total_mib": 81920,
        "memory_used_mib": 0,
        "utilization_pct": 0,
    }


def _config(scratch: Path, **kwargs) -> ComputeConfig:
    return ComputeConfig(
        mode="ssh",
        hosts=(SshHost(name="gpu-01", alias="gpu01.lab", scratch=scratch.as_posix()),),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_the_check_reports_what_the_machine_actually_has(tmp_path, fake_gpus):
    """事实要来自机器，不是来自配置文件里的一厢情愿。"""
    fake_gpus["gpu-01"] = [_gpu(0)]

    check = await check_compute(
        _config(tmp_path / "scratch"), transport_factory=_FakeTransport
    )

    assert check.ok
    (host,) = check.hosts
    assert host.ok and not host.error
    assert host.facts["python"].startswith("3.")
    assert host.facts["python_executable"]
    assert host.gpus[0]["name"] == "NVIDIA A100"


@pytest.mark.asyncio
async def test_the_check_shows_the_runtime_block_the_agent_will_get(
    tmp_path, fake_gpus
):
    """这一段是最容易悄悄说谎的地方，所以自检必须把**原文**打出来。

    它说谎的代价不是报错，是 agent 照着一份全是缺省值的描述写一整轮代码。
    """
    fake_gpus["gpu-01"] = [_gpu(0)]

    check = await check_compute(
        _config(tmp_path / "scratch"), transport_factory=_FakeTransport
    )
    block = check.hosts[0].runtime_block

    assert "- Python: missing" not in block, "事实没问到就等于自检什么也没验"
    assert "used as-is" in block
    assert "cannot install packages" in block
    assert "NVIDIA A100" in block
    assert "remote host gpu-01" in block


@pytest.mark.asyncio
async def test_a_host_without_gpus_is_reported_as_unusable(tmp_path, fake_gpus):
    """没有卡的机器进不了池子——自检就该说不可用，而不是"连上了"。"""
    fake_gpus["gpu-01"] = []

    check = await check_compute(
        _config(tmp_path / "scratch"), transport_factory=_FakeTransport
    )

    assert not check.ok
    assert "no GPUs" in check.hosts[0].error
    assert print_compute_check(check) == 1


@pytest.mark.asyncio
async def test_a_host_that_cannot_be_reached_says_why(tmp_path, fake_gpus):
    """连不上要带上原因；三种完全不同的故障不能塌成同一句话。"""

    class _Dead(_FakeTransport):
        def __init__(self, host: SshHost) -> None:
            super().__init__(host)
            self._python = "definitely-not-a-python-interpreter"

    check = await check_compute(_config(tmp_path / "scratch"), transport_factory=_Dead)

    assert not check.ok
    assert check.hosts[0].error
    assert check.hosts[0].facts == {}


@pytest.mark.asyncio
async def test_leftover_lease_directories_are_surfaced(tmp_path, fake_gpus):
    """正常归还会自己删租约目录；还留着的来自崩掉的那一轮，必须被看见。

    不报的话它就是一条无界的磁盘泄漏，而盘满的表现是"实验莫名其妙失败"。
    """
    fake_gpus["gpu-01"] = [_gpu(0)]
    scratch = tmp_path / "scratch"
    stale = scratch / "leases" / "h9" / "workspace"
    stale.mkdir(parents=True)
    (stale / "checkpoint.bin").write_bytes(b"z" * 4096)

    check = await check_compute(_config(scratch), transport_factory=_FakeTransport)
    leases = check.hosts[0].leases

    assert leases is not None
    assert [entry["name"] for entry in leases.entries] == ["h9"]
    assert leases.used >= 4096


@pytest.mark.asyncio
async def test_the_check_says_whether_the_dataset_fits(tmp_path, fake_gpus, capsys):
    """没有共享存储时，scratch 装不装得下是最先撞上的墙。"""
    fake_gpus["gpu-01"] = [_gpu(0)]
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "train.csv").write_bytes(b"id,y" + chr(10).encode() + b"1,0")

    check = await check_compute(
        _config(tmp_path / "scratch"),
        dataset_root=dataset,
        transport_factory=_FakeTransport,
    )
    assert print_compute_check(check) == 0

    printed = capsys.readouterr().out
    assert "数据集" in printed
    assert check.dataset is not None
    assert check.dataset.total_bytes == 8


@pytest.mark.asyncio
async def test_the_check_leaves_nothing_behind(tmp_path, fake_gpus):
    """自检不占卡、不建目录。它必须能随时跑，包括在一轮研究正在跑的时候。"""
    fake_gpus["gpu-01"] = [_gpu(0)]
    scratch = tmp_path / "scratch"

    await check_compute(_config(scratch), transport_factory=_FakeTransport)

    assert not scratch.exists(), "自检不该在远端建任何东西"


def test_local_compute_has_nothing_to_check(capsys) -> None:
    """本地算力时不该假装检查了什么。"""
    assert print_compute_check(ComputeCheck(config=ComputeConfig())) == 0
    assert "本机" in capsys.readouterr().out
