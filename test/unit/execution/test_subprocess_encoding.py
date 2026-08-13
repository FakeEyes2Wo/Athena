"""子进程输出解码测试：中文 Windows 的 GBK locale 不得撕裂 uv/python 的 UTF-8 输出。

``subprocess.run(text=True)`` 不带 ``encoding`` 时按 locale 解码（中文 Windows 上是
GBK）。uv 的输出含非 GBK 字节，解码在读取线程里抛 ``UnicodeDecodeError``——异常留在
子线程不向上传播，调用方只拿到空 stdout，于是表现为"命令莫名其妙没输出"。
"""

import subprocess
import sys
from pathlib import Path

from athena.execution.runtime import EnvironmentManager
from athena.research.script_runner import _run_cmd, _run_cmd_capture

# ------------------------------------------------------------------------------

# 非 GBK 可编码字符：GBK 无法表示，用来触发 locale 解码失败。
NON_GBK_TEXT = "ַ✓"

# ------------------------------------------------------------------------------


def _print_non_gbk(text: str) -> list[str]:
    """构造一条把非 GBK 文本以 UTF-8 字节写到 stdout 的 python 命令。

    直接 ``print`` 会让子进程按自身 locale 编码（中文 Windows 上是 GBK）而失败，
    那是子进程的编码问题；这里要复现的是父进程按 GBK 解码 UTF-8 输出的问题。
    """
    source = f"import sys; sys.stdout.buffer.write({text!r}.encode('utf-8'))"
    return [sys.executable, "-c", source]


def test_run_cmd_capture_decodes_non_gbk_output(tmp_path: Path) -> None:
    """输出含非 GBK 字符时照常返回内容，而不是空串。"""
    stdout = _run_cmd_capture(_print_non_gbk(NON_GBK_TEXT), cwd=tmp_path)

    assert stdout


def test_run_cmd_tolerates_non_gbk_output(tmp_path: Path) -> None:
    """非 GBK 输出不得让命令执行本身失败。"""
    _run_cmd(_print_non_gbk(NON_GBK_TEXT), cwd=tmp_path)


def test_sync_decodes_non_gbk_uv_output(tmp_path: Path, monkeypatch) -> None:
    """uv sync 的输出按 UTF-8 解码，失败原因不被 locale 解码吞掉。"""
    captured: dict[str, object] = {}
    real_run = subprocess.run

    def spy(args, **kwargs):
        captured.update(kwargs)
        return real_run(_print_non_gbk(NON_GBK_TEXT), **kwargs)

    monkeypatch.setattr("athena.execution.runtime.shutil.which", lambda name: "uv")
    monkeypatch.setattr("athena.execution.runtime.subprocess.run", spy)
    (tmp_path / "pyproject.toml").write_text("[project]", encoding="utf-8")

    result = EnvironmentManager(
        project_root=tmp_path, environment_root=tmp_path
    ).sync()

    assert result["ready"] is True
    assert captured["encoding"] == "utf-8"
