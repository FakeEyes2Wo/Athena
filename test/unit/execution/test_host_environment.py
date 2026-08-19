"""执行环境暴露给 agent 的那几行，必须对得上真正跑命令的那台机器。

这里守的是两个**实测过的**缺陷，不是假想：

1. ``runtime_summary`` 告诉 agent 用 ``uv add --project "$ATHENA_ENV_ROOT"``。
   注入子进程的是**环境**变量，而 PowerShell 里 ``$FOO`` 取的是 PowerShell 变量，
   未定义就静默展开成空串——那条命令在 Windows 上等价于 ``--project ""``。
   实测：``echo "[$ATHENA_ENV_ROOT]"`` 得到 ``[]``，``$env:`` 写法才拿到真路径。

2. 数据集路径原本以绝对路径写进 prompt，agent 生成的脚本因此写死宿主机路径。
   现在只经 ``ATHENA_DATA_ROOT`` 注入，summary 里也只出现变量名。
"""

import os
from pathlib import Path

import pytest

from athena.execution.runtime import EnvironmentManager, ExecutionRuntime


def _manager(tmp_path: Path, *, data_root: Path | None = None) -> EnvironmentManager:
    return EnvironmentManager(
        project_root=tmp_path, environment_root=tmp_path, data_root=data_root
    )


def test_env_refs_use_the_host_shell_syntax(tmp_path) -> None:
    """PowerShell 必须是 ``$env:NAME``；写成 ``$NAME`` 会静默变成空串。"""
    ref = _manager(tmp_path).env_ref("ATHENA_ENV_ROOT")
    if os.name == "nt":
        assert ref == "$env:ATHENA_ENV_ROOT"
    else:
        assert ref == "$ATHENA_ENV_ROOT"


def test_runtime_summary_never_tells_the_agent_a_broken_variable_form(
    tmp_path,
) -> None:
    """summary 里的变量写法必须和 shell 一致，否则 agent 照抄就拿到空串。"""
    summary = _manager(tmp_path).runtime_summary(tmp_path)
    assert "ATHENA_ENV_ROOT" in summary
    if os.name == "nt":
        assert "$env:ATHENA_ENV_ROOT" in summary
        assert '"$ATHENA_ENV_ROOT"' not in summary


def test_the_dataset_root_is_injected_but_never_spelled_out(tmp_path) -> None:
    """summary 只给变量名，不给绝对路径——给了 agent 就会把它写死进脚本。"""
    data = tmp_path / "data"
    data.mkdir()
    manager = _manager(tmp_path, data_root=data)

    assert manager.build_env(tmp_path)["ATHENA_DATA_ROOT"] == str(data)
    summary = manager.runtime_summary(tmp_path)
    assert "ATHENA_DATA_ROOT" in summary
    assert str(data) not in summary


def test_no_dataset_root_means_no_dataset_line_and_no_variable(tmp_path) -> None:
    """没有数据集时不要造一个指向空气的变量：宁可没有这一行。"""
    manager = _manager(tmp_path)
    assert "ATHENA_DATA_ROOT" not in manager.build_env(tmp_path)
    assert "ATHENA_DATA_ROOT" not in manager.runtime_summary(tmp_path)


@pytest.mark.asyncio
async def test_the_shell_form_in_the_summary_actually_resolves(tmp_path) -> None:
    """端到端：按 summary 教的写法在真 shell 里取变量，必须拿到真路径。

    这是上面那几条断言的兜底——只比对字符串的话，换个 shell 又会悄悄错回去。
    """
    from athena.execution.runtime import ExecutionContext

    data = tmp_path / "data"
    data.mkdir()
    runtime = ExecutionRuntime(
        project_root=tmp_path, environment_root=tmp_path, data_root=data
    )
    ref = runtime._env.env_ref("ATHENA_DATA_ROOT")
    context = ExecutionContext(
        project_root=tmp_path, workspace_root=tmp_path, environment_root=tmp_path
    )
    result = await runtime.run(context, f'echo "{ref}"', timeout_s=60)

    assert result.ok
    assert result.stdout.strip() == str(data)
