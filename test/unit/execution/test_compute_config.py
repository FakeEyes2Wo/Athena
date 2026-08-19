"""``[compute]`` 配置：凭据不进配置，降级不给默认。

两条断言最重要，它们守的不是解析正确性，是产品判断：

- ``ssh`` 只收 ``~/.ssh/config`` 的 Host 别名。写成 ``user@ip`` 直接报错——
  一旦允许，端口、密钥路径、跳板设置就会跟着搬进来，凭据管理从此有两个地方。
- ``fallback`` 没有 ``silent``。静默退回本地 CPU 会跑完、会出分、日志也齐，
  但那不是你要的实验，而且没有任何一层会报错。
"""

import pytest

from athena.execution.compute_config import (
    ComputeConfigError,
    load_compute_config,
    parse_compute_config,
)


def test_no_compute_section_means_local(tmp_path) -> None:
    assert parse_compute_config(None).mode == "local"
    assert not parse_compute_config(None).remote
    assert load_compute_config(tmp_path / "missing.toml").mode == "local"


def test_hosts_are_ssh_config_aliases_not_user_at_host() -> None:
    """凭据只允许有一个家：``~/.ssh``。"""
    with pytest.raises(ComputeConfigError, match="Host alias"):
        parse_compute_config(
            {
                "mode": "ssh",
                "hosts": [{"name": "gpu-01", "ssh": "root@10.0.0.5"}],
            }
        )


def test_silent_fallback_is_not_an_option() -> None:
    with pytest.raises(ComputeConfigError, match="silent local fallback"):
        parse_compute_config({"mode": "ssh", "fallback": "silent", "hosts": []})


def test_the_default_fallback_is_never() -> None:
    config = parse_compute_config(
        {"mode": "ssh", "hosts": [{"name": "gpu-01", "ssh": "gpu01.lab"}]}
    )
    assert config.fallback == "never"


def test_ssh_mode_without_hosts_is_an_error() -> None:
    """声明了要用远程却一台机器都没配 —— 这只可能是配错了。"""
    with pytest.raises(ComputeConfigError, match="no \\[\\[compute.hosts\\]\\]"):
        parse_compute_config({"mode": "ssh"})


def test_duplicate_host_names_are_rejected() -> None:
    with pytest.raises(ComputeConfigError, match="duplicate host names"):
        parse_compute_config(
            {
                "mode": "ssh",
                "hosts": [
                    {"name": "gpu-01", "ssh": "a.lab"},
                    {"name": "gpu-01", "ssh": "b.lab"},
                ],
            }
        )


def test_gpu_lists_and_auto_detection_both_parse() -> None:
    config = parse_compute_config(
        {
            "mode": "ssh",
            "placement": "homogeneous",
            "gpus_per_experiment": 2,
            "hosts": [
                {"name": "gpu-01", "ssh": "a.lab", "gpus": "auto"},
                {
                    "name": "gpu-02",
                    "ssh": "b.lab",
                    "gpus": [0, 1, 2, 3],
                    "scratch": "/data/athena",
                    "max_leases": 4,
                },
            ],
        }
    )
    assert config.remote
    assert config.placement == "homogeneous"
    assert config.gpus_per_experiment == 2
    assert config.hosts[0].gpus is None  # auto = 由 nvidia-smi 探测
    assert config.hosts[1].gpus == (0, 1, 2, 3)
    assert config.hosts[1].scratch == "/data/athena"
    assert config.hosts[1].max_leases == 4


def test_a_broken_config_file_is_not_swallowed(tmp_path) -> None:
    """配置写错要在开跑前红，而不是静默按本地跑完一整轮。"""
    path = tmp_path / "config.toml"
    path.write_text("[compute\nmode = 'ssh'\n", encoding="utf-8")
    with pytest.raises(ComputeConfigError):
        load_compute_config(path)


def test_a_real_config_file_round_trips(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            [
                "[compute]",
                'mode = "ssh"',
                'placement = "spread"',
                "",
                "[[compute.hosts]]",
                'name = "gpu-01"',
                'ssh = "gpu01.lab"',
                'scratch = "/scratch/athena"',
                "max_leases = 2",
            ]
        ),
        encoding="utf-8",
    )
    config = load_compute_config(path)
    assert config.mode == "ssh"
    assert config.placement == "spread"
    assert config.hosts[0].alias == "gpu01.lab"
    assert config.hosts[0].ssh_argv()[0] == "ssh"
