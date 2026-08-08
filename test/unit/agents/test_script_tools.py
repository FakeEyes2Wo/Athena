import asyncio

from athena.agents.tools.script_tools import (
    CommitResultTool,
    RunScriptTool,
    WriteScriptTool,
)
from athena.core.artifact_store import LocalArtifactStore
from athena.core.tool_types import ToolContext


def _tctx() -> ToolContext:
    return ToolContext("t", "call-1", lambda *_a: None, None)


def test_write_script_creates_file(tmp_path) -> None:
    tool = WriteScriptTool(tmp_path)
    result = asyncio.run(tool.execute({"path": "a.py", "content": "print(1)"}, _tctx()))
    assert (tmp_path / "a.py").read_text() == "print(1)"


def test_commit_result_persists_to_store(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "a")
    tool = CommitResultTool(store)
    result = asyncio.run(tool.execute({"text": "hello"}, _tctx()))
    assert result.data["ref"].startswith("sha256:")
    assert asyncio.run(store.get_text(result.data["ref"])) == "hello"


def test_run_script_runs_in_workspace(tmp_path) -> None:
    (tmp_path / "a.py").write_text("print('ok')")
    tool = RunScriptTool(tmp_path)
    result = asyncio.run(tool.execute({"path": "a.py"}, _tctx()))
    assert result.data["returncode"] == 0
    assert "ok" in result.data["stdout"]


def test_write_script_rejects_relative_escape(tmp_path) -> None:
    target = (tmp_path / ".." / "x.py").resolve()
    tool = WriteScriptTool(tmp_path)
    result = asyncio.run(tool.execute({"path": "../x.py", "content": "p"}, _tctx()))
    assert result.success is False
    assert "escape" in result.error
    assert not target.exists()


def test_write_script_rejects_absolute_path_outside_workspace(tmp_path) -> None:
    target = (tmp_path.parent / "abs.py").resolve()
    tool = WriteScriptTool(tmp_path)
    result = asyncio.run(tool.execute({"path": str(target), "content": "p"}, _tctx()))
    assert result.success is False
    assert "escape" in result.error
    assert not target.exists()


def test_write_script_happy_path_success(tmp_path) -> None:
    tool = WriteScriptTool(tmp_path)
    result = asyncio.run(tool.execute({"path": "ok.py", "content": "p"}, _tctx()))
    assert result.success is True
    assert (tmp_path / "ok.py").read_text() == "p"
