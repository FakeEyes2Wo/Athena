"""``_StreamDecoder`` 单元测试：UTF-8 优先、GBK/系统代码页回退、chunk 边界。"""

import os
from pathlib import Path

import pytest

import athena.execution.runtime as execution_runtime
from athena.execution.runtime import (
    CommandRequest,
    EnvironmentManager,
    ExecutionContext,
    ExecutionRuntime,
    _StreamDecoder,
)


def _feed(decoder: _StreamDecoder, chunks: list[bytes], final: bool = True) -> str:
    """按给定分块喂入字节并拼接解码结果，模拟 pipe 的任意切分。"""
    parts = [decoder.decode(chunk) for chunk in chunks]
    parts.append(decoder.decode(b"", final=final))
    return "".join(parts)


def test_decodes_utf8_chinese_across_chunks() -> None:
    """UTF-8 中文跨 chunk 撕裂也能完整还原（不产生 U+FFFD）。"""
    raw = "挑战杯".encode("utf-8")
    for split in (len(raw), 2, 1):
        decoder = _StreamDecoder("gbk")
        assert _feed(decoder, [raw[:split], raw[split:]]) == "挑战杯"


def test_decodes_gbk_fallback() -> None:
    """非法 UTF-8 的 GBK 字节回退到系统代码页解码。"""
    raw = "挑战杯".encode("gbk")
    assert _feed(_StreamDecoder("gbk"), [raw]) == "挑战杯"


def test_ascii_released_immediately_but_encoding_still_undecided() -> None:
    """纯 ASCII 立刻放行，编码判定仍推迟到第一批非 ASCII 字节。

    放行是必须的：``BoundedOutput`` 的 60000 字符上限在解码器**下游**，攒着不吐
    等于绕过它。2026-08-30 实测，shell 换到 pwsh 7 后 ``dir`` 表头从中文变英文，
    这条分支不再被非 ASCII 打断，主进程工作集冲到 20 GB。
    """
    decoder = _StreamDecoder("gbk")
    assert decoder.decode(b"hello ") == "hello "  # 立刻吐出，不是 ""
    # 判定并未被 ASCII 前缀锁死：后续 GBK 仍按 GBK 解，不会被当成 UTF-8。
    assert decoder.decode("挑战杯".encode("gbk")) == "挑战杯"
    assert decoder.decode(b"", final=True) == ""


def test_long_ascii_stream_does_not_accumulate() -> None:
    """长 ASCII 输出必须逐块吐出，解码器内部不得留存。"""
    decoder = _StreamDecoder("gbk")
    chunk = b"x" * 8192
    for _ in range(64):  # 512 KB
        assert decoder.decode(chunk) == "x" * 8192
    assert len(decoder._buf) == 0, "ASCII 放行后缓冲区必须是空的"


def test_final_flushes_ascii_only_stream() -> None:
    """纯 ASCII 流在 final 时一次性吐出（UTF-8/GBK 下 ASCII 相同）。"""
    assert _feed(_StreamDecoder("gbk"), [b"ok", b" done"]) == "ok done"


def test_invalid_byte_degrades_to_replacement() -> None:
    """已判定 UTF-8 后混入的非法字节退化为 U+FFFD，不抛异常。"""
    decoder = _StreamDecoder("gbk")
    text = decoder.decode("好".encode("utf-8")) + decoder.decode(b"\xff", final=True)
    assert "好" in text
    assert "�" in text


@pytest.mark.skipif(os.name != "nt", reason="Windows shell 探测")
def test_windows_shell_detection_finds_pwsh_on_path(tmp_path, monkeypatch) -> None:
    """PATH 里的 pwsh 7 可用时，即使固定安装路径不存在也会选中它。"""
    candidates = (
        (r"C:\Program Files\PowerShell\7\pwsh.exe", ["-NoProfile"]),
        ("pwsh", ["-NoProfile"]),
        (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", ["/c"]),
    )
    monkeypatch.setattr(execution_runtime, "_WIN_SHELLS", candidates)
    monkeypatch.setattr(Path, "is_file", lambda _path: False)
    monkeypatch.setattr(
        execution_runtime.shutil,
        "which",
        lambda name, **_kwargs: r"D:\PowerShell\pwsh.exe" if name == "pwsh" else None,
    )

    shell, args = EnvironmentManager(environment_root=tmp_path).shell_parts()

    assert shell == r"D:\PowerShell\pwsh.exe"
    assert args == ["-NoProfile"]


@pytest.mark.skipif(os.name != "nt", reason="Windows shell 探测")
def test_windows_shell_detection_falls_back_to_powershell_51(
    tmp_path, monkeypatch
) -> None:
    """PowerShell 5.1 remains the supported fallback when pwsh is unavailable."""
    fallback = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    candidates = (
        (r"C:\Program Files\PowerShell\7\pwsh.exe", ["-NoProfile"]),
        ("pwsh", ["-NoProfile"]),
        (fallback, ["/d", "/s", "/c"]),
    )
    monkeypatch.setattr(execution_runtime, "_WIN_SHELLS", candidates)
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda path: str(path).casefold() == fallback.casefold(),
    )
    monkeypatch.setattr(
        execution_runtime.shutil, "which", lambda *_args, **_kwargs: None
    )

    shell, args = EnvironmentManager(environment_root=tmp_path).shell_parts()

    assert shell == fallback
    assert args == ["/d", "/s", "/c"]


@pytest.mark.skipif(os.name != "nt", reason="依赖 Windows shell")
@pytest.mark.asyncio
async def test_runtime_reads_utf8_cjk_file_without_mojibake(tmp_path) -> None:
    """agent 用 shell 读 UTF-8 中文文件，必须原样拿回来。

    这是 2026-08-30 那次乱码的最小复现：``type README.md`` 读一个 UTF-8 无 BOM 的
    中文文件，5.1 下会得到「Athena 鐨勪换鍔℃暟鎹洰褰」这类 UTF-8-被当-GBK 的产物。
    """
    target = tmp_path / "README.md"
    target.write_text("# Athena 的任务数据目录\n挑战杯\n", encoding="utf-8")
    runtime = ExecutionRuntime(project_root=tmp_path, environment_root=tmp_path)
    context = ExecutionContext(
        project_root=tmp_path, workspace_root=tmp_path, environment_root=tmp_path
    )
    result = await runtime.run(context, CommandRequest(command=f'type "{target}"'))
    assert result.ok, result.stderr
    assert "的任务数据目录" in result.stdout
    assert "挑战杯" in result.stdout
    assert "鐨" not in result.stdout  # UTF-8 被当 GBK 读的特征字
    assert "�" not in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="依赖 Windows PowerShell")
@pytest.mark.asyncio
async def test_powershell_formats_object_output_before_process_exit(tmp_path) -> None:
    """PowerShell object output is rendered before the runtime exits the shell."""
    workspace = tmp_path / "挑战杯工作区"
    workspace.mkdir()
    (workspace / "visible-dataset.csv").write_text("feature,target\n1,0\n")
    runtime = ExecutionRuntime(project_root=workspace, environment_root=tmp_path)
    context = ExecutionContext(
        project_root=workspace,
        workspace_root=workspace,
        environment_root=tmp_path,
    )

    for command in ("pwd", "Get-Location"):
        result = await runtime.run(
            context, CommandRequest(command=command, workdir=workspace)
        )
        assert result.ok, result.stderr
        assert str(workspace) in result.stdout

    listing = await runtime.run(
        context, CommandRequest(command="Get-ChildItem", workdir=workspace)
    )
    assert listing.ok, listing.stderr
    assert "visible-dataset.csv" in listing.stdout

    combined = await runtime.run(
        context,
        CommandRequest(command="pwd; ls -la", workdir=workspace),
    )
    assert str(workspace) in combined.stdout
    assert "parameter name 'la'" in combined.stderr
    assert combined.exit_code != 0


@pytest.mark.skipif(os.name != "nt", reason="依赖 Windows PowerShell")
@pytest.mark.asyncio
async def test_powershell_wrapper_preserves_errors_exit_codes_and_unicode(
    tmp_path,
) -> None:
    runtime = ExecutionRuntime(project_root=tmp_path, environment_root=tmp_path)
    context = ExecutionContext(
        project_root=tmp_path,
        workspace_root=tmp_path,
        environment_root=tmp_path,
    )

    cmdlet_error = await runtime.run(
        context,
        CommandRequest(command="Get-ChildItem -ATHENA_INVALID_PARAMETER"),
    )
    assert not cmdlet_error.ok
    assert cmdlet_error.exit_code != 0
    assert "ATHENA_INVALID_PARAMETER" in cmdlet_error.stderr

    native_error = await runtime.run(
        context,
        CommandRequest(
            command=("python -c \"import sys; print('native-marker'); sys.exit(17)\"")
        ),
    )
    assert "native-marker" in native_error.stdout
    assert native_error.exit_code == 17

    script_exit = await runtime.run(
        context,
        CommandRequest(command="Write-Output 'before-挑战杯'; exit 23"),
    )
    assert "before-挑战杯" in script_exit.stdout
    assert script_exit.exit_code == 23

    cmdlet_failure = await runtime.run(
        context, CommandRequest(command="Write-Error 'last-command-error'")
    )
    assert not cmdlet_failure.ok
    assert cmdlet_failure.exit_code != 0
    assert "last-command-error" in cmdlet_failure.stderr

    terminating_failure = await runtime.run(
        context, CommandRequest(command="throw 'terminating-marker'")
    )
    assert not terminating_failure.ok
    assert terminating_failure.exit_code != 0
    assert "terminating-marker" in terminating_failure.stderr

    handled_exception = await runtime.run(
        context,
        CommandRequest(
            command=(
                "try { throw 'expected-caught'; } "
                "catch { Write-Output 'caught-marker' }"
            )
        ),
    )
    assert handled_exception.ok
    assert handled_exception.exit_code == 0
    assert "caught-marker" in handled_exception.stdout

    native_then_missing_item = await runtime.run(
        context,
        CommandRequest(
            command=('python -c "pass"; ' "Get-Item 'ATHENA_DEFINITELY_MISSING_PATH'")
        ),
    )
    assert not native_then_missing_item.ok
    assert native_then_missing_item.exit_code != 0
    assert "ATHENA_DEFINITELY_MISSING_PATH" in native_then_missing_item.stderr

    cmdlet_error_then_success = await runtime.run(
        context,
        CommandRequest(
            command=(
                "Get-Item 'ATHENA_DEFINITELY_MISSING_PATH'; "
                "Write-Output 'after-cmdlet-error'"
            )
        ),
    )
    assert not cmdlet_error_then_success.ok
    assert cmdlet_error_then_success.exit_code != 0
    assert "ATHENA_DEFINITELY_MISSING_PATH" in cmdlet_error_then_success.stderr
    assert "after-cmdlet-error" in cmdlet_error_then_success.stdout

    native_then_invalid_parameter = await runtime.run(
        context,
        CommandRequest(
            command=('python -c "pass"; ' "Get-ChildItem -ATHENA_INVALID_PARAMETER")
        ),
    )
    assert not native_then_invalid_parameter.ok
    assert native_then_invalid_parameter.exit_code != 0
    assert "ATHENA_INVALID_PARAMETER" in native_then_invalid_parameter.stderr

    native_warning = await runtime.run(
        context,
        CommandRequest(
            command=(
                "python -c \"import sys; sys.stderr.write('native-warning'); "
                'sys.exit(0)" 2>&1 | Select-Object -Last 40'
            )
        ),
    )
    assert native_warning.ok, native_warning.stderr
    assert native_warning.exit_code == 0
    assert "native-warning" in native_warning.stdout + native_warning.stderr

    native_warning_nonzero = await runtime.run(
        context,
        CommandRequest(
            command=(
                "python -c \"import sys; sys.stderr.write('native-warning'); "
                'sys.exit(17)" 2>&1 | Select-Object -Last 40'
            )
        ),
    )
    assert not native_warning_nonzero.ok
    assert native_warning_nonzero.exit_code == 17
    assert "native-warning" in (
        native_warning_nonzero.stdout + native_warning_nonzero.stderr
    )

    delayed_native_warning = await runtime.run(
        context,
        CommandRequest(
            command=(
                'python -c "import sys,time; '
                "sys.stderr.write('warning-line1\\nline2\\n'); sys.stderr.flush(); "
                "time.sleep(0.2); print('completion-marker'); sys.exit(0)"
                '" 2>&1 | Select-Object -Last 40'
            )
        ),
    )
    assert delayed_native_warning.ok, delayed_native_warning.stderr
    assert delayed_native_warning.exit_code == 0
    assert "warning-line1" in (
        delayed_native_warning.stdout + delayed_native_warning.stderr
    )
    assert "line2" in delayed_native_warning.stdout + delayed_native_warning.stderr
    assert "completion-marker" in (
        delayed_native_warning.stdout + delayed_native_warning.stderr
    )

    delayed_native_warning_nonzero = await runtime.run(
        context,
        CommandRequest(
            command=(
                'python -c "import sys,time; '
                "sys.stderr.write('warning-line1\\nline2\\n'); sys.stderr.flush(); "
                "time.sleep(0.2); print('completion-marker'); sys.exit(17)"
                '" 2>&1 | Select-Object -Last 40'
            )
        ),
    )
    assert not delayed_native_warning_nonzero.ok
    assert delayed_native_warning_nonzero.exit_code == 17
    assert "warning-line1" in (
        delayed_native_warning_nonzero.stdout + delayed_native_warning_nonzero.stderr
    )
    assert "line2" in (
        delayed_native_warning_nonzero.stdout + delayed_native_warning_nonzero.stderr
    )
    assert "completion-marker" in (
        delayed_native_warning_nonzero.stdout + delayed_native_warning_nonzero.stderr
    )


@pytest.mark.skipif(os.name != "nt", reason="依赖 Windows PowerShell")
@pytest.mark.asyncio
async def test_powershell_success_output_streams_before_timeout(tmp_path) -> None:
    runtime = ExecutionRuntime(project_root=tmp_path, environment_root=tmp_path)
    context = ExecutionContext(
        project_root=tmp_path,
        workspace_root=tmp_path,
        environment_root=tmp_path,
    )

    result = await runtime.run(
        context,
        CommandRequest(
            command="Write-Output 'before-timeout-marker'; Start-Sleep -Seconds 10",
            timeout_s=1,
        ),
    )

    assert result.error == "timeout"
    assert "before-timeout-marker" in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="依赖 Windows shell")
@pytest.mark.asyncio
async def test_chain_operator_works_under_powershell7(tmp_path) -> None:
    """``&&`` 必须可用。

    5.1 把它当解析错误，agent 每次写 ``cd X && python y.py`` 都要浪费一轮重试改写
    ——2026-08-30 的运行日志里每个 EDA worker 都撞了一次。7 原生支持。
    """
    runtime = ExecutionRuntime(project_root=tmp_path, environment_root=tmp_path)
    shell, _args = EnvironmentManager(environment_root=tmp_path).shell_parts()
    if Path(shell).name.casefold() == "powershell.exe":
        pytest.skip("PowerShell 5.1 does not support the PowerShell 7 && operator")
    context = ExecutionContext(
        project_root=tmp_path, workspace_root=tmp_path, environment_root=tmp_path
    )
    result = await runtime.run(context, CommandRequest(command="echo a && echo b"))
    assert result.ok, result.stderr
    assert "a" in result.stdout and "b" in result.stdout
    assert "�" not in result.stderr  # 无替换字符 = 无 mojibake
