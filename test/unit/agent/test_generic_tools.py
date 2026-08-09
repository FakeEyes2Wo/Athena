# test/unit/agent/test_generic_tools.py
import asyncio
from pathlib import Path

from athena.agents.tools.generic_tools import (
    BashTool,
    PwshTool,
    ReadFileTool,
    WriteFileTool,
    generic_tool_registry,
)
from athena.core.tool_types import ToolContext


def _ctx() -> ToolContext:
    return ToolContext("t", "id", lambda *a: asyncio.sleep(0), asyncio.Event())


async def test_read_file_line_range(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text(
        "\n".join(f"line{i}" for i in range(1, 6)), encoding="utf-8"
    )
    tool = ReadFileTool(tmp_path)
    result = await tool.ainvoke(_ctx(), path="a.txt", start_line=2, end_line=3)
    assert result.data["content"] == "line2\nline3"


async def test_write_file_creates_file(tmp_path: Path) -> None:
    tool = WriteFileTool(tmp_path)
    result = await tool.ainvoke(_ctx(), path="sub/b.txt", content="hello")
    assert result.success
    assert (tmp_path / "sub" / "b.txt").read_text(encoding="utf-8") == "hello"


async def test_path_escape_is_rejected(tmp_path: Path) -> None:
    tool = WriteFileTool(tmp_path)
    result = await tool.ainvoke(_ctx(), path="../evil.txt", content="x")
    assert not result.success
    assert not (tmp_path.parent / "evil.txt").exists()


async def test_bash_runs_and_returns_output(tmp_path: Path) -> None:
    tool = BashTool(tmp_path)
    result = await tool.ainvoke(_ctx(), command="echo hello-from-bash")
    assert result.success
    assert "hello-from-bash" in result.data["stdout"]


async def test_registry_has_four_tools(tmp_path: Path) -> None:
    reg = generic_tool_registry(tmp_path)
    assert {t.name for t in reg.specs} == {"read_file", "write_file", "bash", "pwsh"}
