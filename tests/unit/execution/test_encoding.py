"""``_StreamDecoder`` 单元测试：UTF-8 优先、GBK/系统代码页回退、chunk 边界。"""

import os

import pytest

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
def test_windows_shell_detection_finds_pwsh_on_path(tmp_path) -> None:
    """探测到的必须是 PowerShell 7，哪怕它不在 Program Files 下。

    回归自 2026-08-30：``_WIN_SHELLS`` 只列了两个写死的 ``Program Files`` 路径，
    Windows 分支又只判断 ``Path(shell).is_file()``、从不查 PATH。本机的 pwsh 7 是
    Microsoft Store 装的（``…\\AppData\\Local\\Microsoft\\WindowsApps\\pwsh.exe``），
    于是探测直接掉到 Windows PowerShell 5.1 —— 而 5.1 的 ``Get-Content`` 按系统
    ANSI 代码页读文件，中文 Windows 上把 UTF-8 文本读成乱码喂给 agent。
    """
    manager = EnvironmentManager(environment_root=tmp_path)
    shell, _args = manager.shell_parts()
    assert "pwsh" in shell.lower(), f"应选 PowerShell 7，实际是 {shell}"


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


@pytest.mark.skipif(os.name != "nt", reason="依赖 Windows shell")
@pytest.mark.asyncio
async def test_chain_operator_works_under_powershell7(tmp_path) -> None:
    """``&&`` 必须可用。

    5.1 把它当解析错误，agent 每次写 ``cd X && python y.py`` 都要浪费一轮重试改写
    ——2026-08-30 的运行日志里每个 EDA worker 都撞了一次。7 原生支持。
    """
    runtime = ExecutionRuntime(project_root=tmp_path, environment_root=tmp_path)
    context = ExecutionContext(
        project_root=tmp_path, workspace_root=tmp_path, environment_root=tmp_path
    )
    result = await runtime.run(context, CommandRequest(command="echo a && echo b"))
    assert result.ok, result.stderr
    assert "a" in result.stdout and "b" in result.stdout
    assert "�" not in result.stderr  # 无替换字符 = 无 mojibake
