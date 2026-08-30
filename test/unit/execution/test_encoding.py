"""``_StreamDecoder`` 单元测试：UTF-8 优先、GBK/系统代码页回退、chunk 边界。"""

import os

import pytest

from athena.execution.runtime import (
    CommandRequest,
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


def test_ascii_only_buffered_until_final() -> None:
    """纯 ASCII 保持未判定直到 final，避免把后续 GBK 误判为 UTF-8。"""
    decoder = _StreamDecoder("gbk")
    assert decoder.decode(b"hello ") == ""
    assert decoder.decode("挑战杯".encode("gbk")) == "hello 挑战杯"
    assert decoder.decode(b"", final=True) == ""


def test_final_flushes_ascii_only_stream() -> None:
    """纯 ASCII 流在 final 时一次性吐出（UTF-8/GBK 下 ASCII 相同）。"""
    assert _feed(_StreamDecoder("gbk"), [b"ok", b" done"]) == "ok done"


def test_invalid_byte_degrades_to_replacement() -> None:
    """已判定 UTF-8 后混入的非法字节退化为 U+FFFD，不抛异常。"""
    decoder = _StreamDecoder("gbk")
    text = decoder.decode("好".encode("utf-8")) + decoder.decode(b"\xff", final=True)
    assert "好" in text
    assert "�" in text


@pytest.mark.skipif(os.name != "nt", reason="依赖中文 Windows 的 GBK 控制台输出")
@pytest.mark.asyncio
async def test_runtime_decodes_powershell_parse_error_gbk(tmp_path) -> None:
    """PowerShell 解析错误 stderr（GBK）不再 mojibake。

    ``&&`` 在 PowerShell 5.1 是解析错误，且发生在 UTF-8 包装生效之前，故按
    控制台代码页（GBK）输出——正是 titanic 运行中观察到的问题。
    """
    runtime = ExecutionRuntime(project_root=tmp_path, environment_root=tmp_path)
    context = ExecutionContext(
        project_root=tmp_path, workspace_root=tmp_path, environment_root=tmp_path
    )
    result = await runtime.run(context, CommandRequest(command="echo a && echo b"))
    assert not result.ok
    assert "&&" in result.stderr  # 源命令的 token 会回显在错误里
    assert "�" not in result.stderr  # 无替换字符 = 无 mojibake
