"""Athena 自管的项目仓不得做行尾归一化。

这个仓库是 ``LocalGitWorkspace.init`` 用一句 ``git init`` 现建的：没有
``.gitattributes``，于是完整继承用户的全局 ``core.autocrlf``。本机实测该值为
``true``——在 Windows 上签出的脚本因此带 CRLF。同机跑无害，一旦把工作区送到
Linux 执行，shebang 行末尾多出的 CR 直接变成 ``bad interpreter``，而报错内容
完全指不到真正的原因。

用例用 ``GIT_CONFIG_GLOBAL`` 造一份 ``autocrlf=true`` 的全局配置，让判定与跑
用例的这台机器的真实全局设置无关。
"""

import subprocess
from pathlib import Path

import pytest

from athena.core.git_workspace import LocalGitWorkspace


@pytest.fixture
def hostile_global_git(tmp_path, monkeypatch) -> Path:
    """一份开启了行尾归一化的全局 git 配置。"""
    config = tmp_path / "gitconfig"
    # 身份也要给：GIT_CONFIG_NOSYSTEM 把真实全局配置一起挡掉了，
    # 缺 user.name 时 init 里的首个 commit 会直接失败。
    config.write_text(
        "[core]\n\tautocrlf = true\n"
        "[user]\n\tname = athena-test\n"
        "\temail = athena@test.invalid\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return config


async def _init(tmp_path: Path) -> LocalGitWorkspace:
    workspace = LocalGitWorkspace(
        tmp_path / "repo", tmp_path / "worktrees", lambda _data: "sha256:" + "0" * 64
    )
    await workspace.init()
    return workspace


def _config(repo: Path, key: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "config", "--get", key],
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


@pytest.mark.asyncio
async def test_the_project_repo_disables_line_ending_translation(
    tmp_path, hostile_global_git
) -> None:
    await _init(tmp_path)
    assert _config(tmp_path / "repo", "core.autocrlf") == "false"


@pytest.mark.asyncio
async def test_checked_out_scripts_keep_lf_under_a_hostile_global_config(
    tmp_path, hostile_global_git
) -> None:
    """真正要守的是字节：提交 LF、签出回来仍然是 LF，一个 CR 都不能多。"""
    await _init(tmp_path)
    repo = tmp_path / "repo"
    script = repo / "train.sh"
    body = b"#!/bin/bash\npython train.py\n"
    script.write_bytes(body)

    subprocess.run(["git", "-C", str(repo), "add", "train.sh"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-m",
            "script",
        ],
        check=True,
        capture_output=True,
    )
    script.unlink()
    subprocess.run(["git", "-C", str(repo), "checkout", "--", "train.sh"], check=True)

    assert script.read_bytes() == body
    assert b"\r" not in script.read_bytes()
