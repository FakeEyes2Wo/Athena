"""shared execution runtime 单元测试：shell 探测、命令执行、事件、超时、截断。"""

import asyncio
import os
from pathlib import Path

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.core.tool_types import ToolContext
from athena.execution.runtime import (
    CommandExecutor,
    CommandRequest,
    EnvironmentManager,
    ExecutionContext,
    ExecutionRuntime,
)

PY = "python"


def _runtime(tmp_path: Path) -> ExecutionRuntime:
    return ExecutionRuntime(project_root=tmp_path, environment_root=tmp_path)


def _context(tmp_path: Path) -> ExecutionContext:
    return ExecutionContext(
        project_root=tmp_path, workspace_root=tmp_path, environment_root=tmp_path
    )


def test_shell_parts_detects_native_shell(tmp_path: Path) -> None:
    """探测返回当前平台可用的 shell 绝对路径与启动参数。"""
    shell, args = EnvironmentManager(
        project_root=tmp_path, environment_root=tmp_path
    ).shell_parts()
    assert Path(shell).is_file()
    assert args


def test_build_env_places_venv_first(tmp_path: Path) -> None:
    """环境根 .venv 的 Scripts/bin 前置 PATH，且注入 UTF-8 与 ATHENA_ENV_ROOT。"""
    bindir = tmp_path / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    bindir.mkdir(parents=True)
    env = EnvironmentManager(
        project_root=tmp_path, environment_root=tmp_path
    ).build_env()
    assert env["PATH"].split(os.pathsep)[0] == str(bindir)
    assert env["PYTHONUTF8"] == "1"
    assert env["ATHENA_ENV_ROOT"] == str(tmp_path)


def test_build_env_injects_the_command_dataset_path(tmp_path: Path) -> None:
    dataset = tmp_path / "model_input.csv"

    env = EnvironmentManager(
        project_root=tmp_path, environment_root=tmp_path
    ).build_env(data_csv=dataset)

    assert env["ATHENA_DATA_CSV"] == str(dataset)


def test_ensure_environment_creates_minimal_pyproject_when_missing(
    tmp_path: Path,
) -> None:
    """环境根无 pyproject.toml 时，ensure_environment 补一份可被 uv 使用的项目文件。"""
    assert not (tmp_path / "pyproject.toml").exists()

    _runtime(tmp_path).ensure_environment()

    pyproject = tmp_path / "pyproject.toml"
    assert pyproject.is_file()
    content = pyproject.read_text(encoding="utf-8")
    assert "[project]" in content
    assert "dependencies = []" in content


def test_ensure_environment_is_idempotent_and_preserves_existing(
    tmp_path: Path,
) -> None:
    """已有 pyproject.toml 时不动它（幂等，避免覆盖 agent 写入的依赖声明）。"""
    existing = tmp_path / "pyproject.toml"
    existing.write_text("custom-project", encoding="utf-8")

    _runtime(tmp_path).ensure_environment()

    assert existing.read_text(encoding="utf-8") == "custom-project"


def test_runtime_summary_reports_platform_and_python(tmp_path: Path) -> None:
    """运行时摘要包含 OS / Shell / Python 与加依赖提示。"""
    summary = _runtime(tmp_path).runtime_summary(tmp_path)
    assert "OS:" in summary
    assert "Python:" in summary
    assert "uv add --project" in summary


def test_runtime_summary_reports_missing_python(tmp_path: Path, monkeypatch) -> None:
    """python 不可探测时摘要标记需要修复（design §missing-tool diagnostics）。"""
    monkeypatch.setattr(
        "athena.execution.runtime.shutil.which",
        lambda name: "/usr/bin/bash" if name in ("bash", "sh") else None,
    )
    summary = _runtime(tmp_path).runtime_summary(tmp_path)
    assert "needs repair" in summary


@pytest.mark.asyncio
async def test_run_echo_success(tmp_path: Path) -> None:
    """echo hello → ok=True, stdout 含 hello, exit_code=0, 事件含 started/completed。"""
    events: list[tuple[str, str, object]] = []

    async def emit(kind: str, ref: str, data: object) -> None:
        events.append((kind, ref, data))

    result = await _runtime(tmp_path).run(
        _context(tmp_path),
        CommandRequest(command=f"{PY} -c \"print('hello-athena')\"", emit=emit),
    )
    assert result.ok
    assert result.exit_code == 0
    assert "hello-athena" in result.stdout
    kinds = [kind for kind, _, _ in events]
    assert "command/started" in kinds
    assert "command/completed" in kinds


@pytest.mark.asyncio
async def test_run_returns_when_completed_event_consumer_hangs(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """completed 消费者卡住时，命令结果仍在投递边界后返回。"""
    events: list[str] = []

    async def emit(kind: str, _ref: str, _data: object) -> None:
        events.append(kind)
        if kind == "command/completed":
            await asyncio.Event().wait()

    monkeypatch.setattr(
        "athena.execution.runtime.COMPLETION_EMIT_TIMEOUT_S", 0.01, raising=False
    )

    result = await asyncio.wait_for(
        _runtime(tmp_path).run(
            _context(tmp_path),
            CommandRequest(command=f"{PY} -c \"print('completed-result')\"", emit=emit),
        ),
        timeout=2.0,
    )

    assert result.ok
    assert "completed-result" in result.stdout
    assert "command/completed" in events
    assert "command/completed delivery timed out" in caplog.text


@pytest.mark.asyncio
async def test_run_returns_when_exited_process_pipe_never_reaches_eof(
    tmp_path: Path, monkeypatch
) -> None:
    """子进程已退出但 pipe 不送 EOF 时，保留已读输出并在 drain 边界内返回。"""

    class HangingStream:
        def __init__(self) -> None:
            self._sent = False

        def __aiter__(self):
            return self

        async def __anext__(self) -> bytes:
            if not self._sent:
                self._sent = True
                return b"captured-before-eof\n"
            await asyncio.Event().wait()
            raise StopAsyncIteration

    class ClosedStream:
        def __aiter__(self):
            return self

        async def __anext__(self) -> bytes:
            raise StopAsyncIteration

    class ExitedProcess:
        returncode = 0
        stdout = HangingStream()
        stderr = ClosedStream()

        async def wait(self) -> int:
            return 0

    async def create_process(*_args, **_kwargs):
        return ExitedProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(
        "athena.execution.runtime.POST_EXIT_DRAIN_TIMEOUT_S", 0.01, raising=False
    )

    result = await asyncio.wait_for(
        CommandExecutor(env={}).run(
            argv=["finished-command"],
            workdir=tmp_path,
            timeout_s=10,
        ),
        timeout=0.2,
    )

    assert result.ok
    assert result.stdout == "captured-before-eof\n"


@pytest.mark.asyncio
async def test_run_uses_returncode_when_process_wait_never_returns(
    tmp_path: Path, monkeypatch
) -> None:
    """OS 已报告退出时，不再等待仍被 pipe transport 阻塞的 Process.wait。"""

    class ClosedStream:
        def __aiter__(self):
            return self

        async def __anext__(self) -> bytes:
            raise StopAsyncIteration

    class ExitedProcess:
        returncode = 0
        stdout = ClosedStream()
        stderr = ClosedStream()

        async def wait(self) -> int:
            await asyncio.Event().wait()
            return 0

    async def create_process(*_args, **_kwargs):
        return ExitedProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(CommandExecutor, "_terminate", lambda _self, _proc: None)

    result = await asyncio.wait_for(
        CommandExecutor(env={}).run(
            argv=["finished-command"],
            workdir=tmp_path,
            timeout_s=10,
        ),
        timeout=0.2,
    )

    assert result.ok
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_run_rejects_command_and_argv_together(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        await _runtime(tmp_path).run(
            _context(tmp_path),
            CommandRequest(command="echo shell", argv=[PY, "-c", "print('argv')"]),
        )


@pytest.mark.asyncio
async def test_run_requires_command_or_argv(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        await _runtime(tmp_path).run(_context(tmp_path), CommandRequest())


@pytest.mark.asyncio
async def test_run_rejects_empty_argv(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        await _runtime(tmp_path).run(_context(tmp_path), CommandRequest(argv=[]))


@pytest.mark.asyncio
async def test_run_nonzero_exit_is_tool_result(tmp_path: Path) -> None:
    """exit(3) → ok=False, exit_code=3, 无 error（非零退出是工具结果，不是运行时失败）。"""
    result = await _runtime(tmp_path).run(
        _context(tmp_path), CommandRequest(command=f'{PY} -c "import sys; sys.exit(3)"')
    )
    assert not result.ok
    assert result.exit_code == 3
    assert result.error is None


@pytest.mark.asyncio
async def test_run_timeout_terminates_process(tmp_path: Path) -> None:
    """sleep 超时 → error=timeout 且进程树被终止，整体快速返回。"""
    result = await asyncio.wait_for(
        _runtime(tmp_path).run(
            _context(tmp_path),
            CommandRequest(
                command=f'{PY} -c "import time; time.sleep(30)"', timeout_s=1
            ),
        ),
        timeout=10,
    )
    assert not result.ok
    assert result.error == "timeout"


@pytest.mark.asyncio
async def test_run_cancel_terminates_process_tree(tmp_path: Path) -> None:
    """取消 turn → 终止完整子进程树并传播 CancelledError（design §Streaming）。"""
    task = asyncio.create_task(
        _runtime(tmp_path).run(
            _context(tmp_path),
            CommandRequest(
                command=f'{PY} -c "import time; time.sleep(30)"', timeout_s=30
            ),
        )
    )
    await asyncio.sleep(0.5)  # 让子进程起来
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_run_utf8_chinese_output(tmp_path: Path) -> None:
    """中文输出不 mojibake（UTF-8 强制）。"""
    result = await _runtime(tmp_path).run(
        _context(tmp_path),
        CommandRequest(command=f"{PY} -c \"print('中文路径测试')\""),
    )
    assert "中文路径测试" in result.stdout


@pytest.mark.asyncio
async def test_truncation_caps_output_and_hashes(tmp_path: Path, monkeypatch) -> None:
    """超长输出截断为有界头部，并给出 sha256 output_ref。"""
    monkeypatch.setattr("athena.execution.runtime.MAX_OUTPUT_CHARS", 200)
    result = await _runtime(tmp_path).run(
        _context(tmp_path), CommandRequest(command=f"{PY} -c \"print('x' * 10000)\"")
    )
    assert result.truncated
    assert result.output_ref is not None
    assert result.output_ref.startswith("sha256:")
    assert len(result.stdout) < 10000


@pytest.mark.asyncio
async def test_truncation_keeps_the_tail_where_the_failure_is(
    tmp_path: Path, monkeypatch
) -> None:
    """截断必须留尾。

    训练日志前面是配置回显，最后才是 traceback / 最终指标 / OOM。只留头等于
    精确地删掉唯一有用的那一段，而且流读过就没了。省略要在正文里说清楚断了多少，
    否则 agent 会把拼接处当成真实内容读。
    """
    monkeypatch.setattr("athena.execution.runtime.MAX_OUTPUT_CHARS", 300)
    script = (
        "print('CONFIG-ECHO'); print('n' * 5000); "
        "print('Traceback (most recent call last)'); print('RuntimeError: CUDA OOM')"
    )
    result = await _runtime(tmp_path).run(
        _context(tmp_path), CommandRequest(command=f'{PY} -c "{script}"')
    )

    assert result.truncated
    assert "CONFIG-ECHO" in result.stdout, "头部要留：它说明跑的是什么"
    assert "RuntimeError: CUDA OOM" in result.stdout, "尾部要留：失败现场在最后"
    assert "chars elided" in result.stdout, "省略必须写在正文里"


@pytest.mark.asyncio
async def test_the_artifact_behind_output_ref_is_the_complete_log(
    tmp_path: Path, monkeypatch
) -> None:
    """``output_ref`` 承诺"完整输出在这里"，就必须真的完整。

    存一份被砍过的，等于给了 agent 一个骗人的引用：它拿着 ref 去查 traceback，
    查到的还是被砍掉 traceback 的那份。
    """
    monkeypatch.setattr("athena.execution.runtime.MAX_OUTPUT_CHARS", 300)
    store = LocalArtifactStore(tmp_path / "artifacts")
    runtime = ExecutionRuntime(
        project_root=tmp_path, environment_root=tmp_path, store=store
    )
    script = "print('HEAD-MARK'); print('n' * 5000); print('TAIL-MARK')"

    result = await runtime.run(
        _context(tmp_path), CommandRequest(command=f'{PY} -c "{script}"')
    )

    assert result.truncated and result.output_ref is not None
    full = await store.get_text(result.output_ref)
    assert "HEAD-MARK" in full and "TAIL-MARK" in full
    assert full.count("n") >= 5000, "中间那 5000 行也必须在"
    assert result.output_ref in result.stdout, "正文要告诉 agent 去哪儿取全的"


@pytest.mark.asyncio
async def test_missing_shell_reports_error(tmp_path: Path) -> None:
    """shell 路径不存在 → error=shell_not_found（FileNotFoundError 不穿透）。"""
    executor = CommandExecutor(env={})
    result = await executor.run(
        command="echo hi",
        workdir=tmp_path,
        shell=str(tmp_path / "no-such-shell.exe"),
        shell_args=["-c"],
        timeout_s=10,
    )
    assert not result.ok
    assert result.error == "shell_not_found"


def test_resolve_executable_resolves_bare_name(monkeypatch) -> None:
    """裸可执行名按 env PATH 解析为绝对路径；已含路径/未找到则原样返回。"""
    monkeypatch.setattr(
        "athena.execution.runtime.shutil.which",
        lambda name, path: f"{path}/python.exe" if name == "python" else None,
    )
    executor = CommandExecutor(env={"PATH": "/fake/venv"})
    assert executor._resolve_executable(["python", "a", "b"]) == [
        "/fake/venv/python.exe",
        "a",
        "b",
    ]
    assert executor._resolve_executable(["/abs/python", "a"]) == ["/abs/python", "a"]
    assert executor._resolve_executable(["missing", "a"]) == ["missing", "a"]


@pytest.mark.asyncio
async def test_shell_command_tool_contract(tmp_path: Path) -> None:
    """shell_command 工具返回契约 JSON：ok/stdout/stderr/exit_code。"""
    tool = _runtime(tmp_path).shell_command_tool(tmp_path)
    ctx = ToolContext(
        "shell_command", "c1", lambda *a: asyncio.sleep(0), asyncio.Event()
    )
    result = await tool.ainvoke(ctx, command=f"{PY} -c \"print('tool-ok')\"")
    assert result.success
    data = result.data
    assert data["ok"] is True
    assert data["exit_code"] == 0
    assert "tool-ok" in data["stdout"]


@pytest.mark.asyncio
async def test_shell_tool_rejects_framework_owned_athena_writes(tmp_path: Path) -> None:
    """agent 的 shell_command 不得写框架私有目录 .athena/**。"""
    (tmp_path / ".athena").mkdir()
    tool = _runtime(tmp_path).shell_command_tool(tmp_path)
    ctx = ToolContext(
        "shell_command", "c1", lambda *a: asyncio.sleep(0), asyncio.Event()
    )
    result = await tool.ainvoke(ctx, command="Set-Content .athena/state.json '{}'")
    assert not result.success
    assert ".athena" in result.error


@pytest.mark.asyncio
async def test_shell_tool_allows_framework_owned_athena_reads(tmp_path: Path) -> None:
    """只读访问 .athena/** 仍然允许，方便 agent 检查状态。"""
    (tmp_path / ".athena").mkdir()
    (tmp_path / ".athena" / "state.json").write_text("{}", encoding="utf-8")
    tool = _runtime(tmp_path).shell_command_tool(tmp_path)
    ctx = ToolContext(
        "shell_command", "c1", lambda *a: asyncio.sleep(0), asyncio.Event()
    )
    result = await tool.ainvoke(ctx, command="Get-Content .athena/state.json")
    assert result.success
    assert "{}" in result.data["stdout"]


def test_environment_hash_changes_with_declaration(tmp_path: Path) -> None:
    """环境哈希随 pyproject.toml 内容变化；缺声明文件也可计算。"""
    env = EnvironmentManager(project_root=tmp_path, environment_root=tmp_path)
    before = env.environment_hash()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    assert env.environment_hash() != before
    assert env.environment_hash() == env.environment_hash()  # 确定性


def test_sync_requires_pyproject(tmp_path: Path) -> None:
    """环境根缺 pyproject.toml → 明确不 ready，不尝试 uv。"""
    result = EnvironmentManager(project_root=tmp_path, environment_root=tmp_path).sync()
    assert result["ready"] is False
    assert "pyproject" in str(result["error"])


def test_sync_uv_missing(tmp_path: Path, monkeypatch) -> None:
    """uv 不在 PATH → 明确报错，不 ready。"""
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.setattr("athena.execution.runtime.shutil.which", lambda _name: None)
    result = EnvironmentManager(project_root=tmp_path, environment_root=tmp_path).sync()
    assert result["ready"] is False
    assert "uv" in str(result["error"])


def test_sync_runs_uv_with_frozen_flag(tmp_path: Path, monkeypatch) -> None:
    """uv sync 成功 → ready；frozen=True 时参数含 --frozen。"""
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return type("Proc", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("athena.execution.runtime.shutil.which", lambda _name: "uv")
    monkeypatch.setattr("athena.execution.runtime.subprocess.run", fake_run)
    env = EnvironmentManager(project_root=tmp_path, environment_root=tmp_path)
    assert env.sync(frozen=True)["ready"] is True
    assert calls and calls[0][:2] == ["uv", "sync"]
    assert "--frozen" in calls[0]


def test_sync_failure_marks_needs_repair_in_summary(
    tmp_path: Path, monkeypatch
) -> None:
    """sync 失败 → needs_repair 记录，运行时摘要报告修复（design §needs_repair）。"""
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.setattr("athena.execution.runtime.shutil.which", lambda _name: None)
    env = EnvironmentManager(project_root=tmp_path, environment_root=tmp_path)
    result = env.sync()
    assert result["ready"] is False
    assert env.needs_repair is not None
    assert "needs repair" in env.runtime_summary(tmp_path)


def test_sync_success_clears_needs_repair(tmp_path: Path, monkeypatch) -> None:
    """sync 成功 → needs_repair 清除，摘要恢复 ready。"""
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    def fake_run(args, **kwargs):
        return type("Proc", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("athena.execution.runtime.shutil.which", lambda _name: "uv")
    monkeypatch.setattr("athena.execution.runtime.subprocess.run", fake_run)
    env = EnvironmentManager(project_root=tmp_path, environment_root=tmp_path)
    assert env.sync()["ready"] is True
    assert env.needs_repair is None


@pytest.mark.asyncio
async def test_runtime_persists_failed_output_to_store(tmp_path: Path) -> None:
    """配置 store 时失败命令的完整输出持久化为 artifact（design §Persistence Policy）。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    runtime = ExecutionRuntime(
        project_root=tmp_path, environment_root=tmp_path, store=store
    )
    result = await runtime.run(
        _context(tmp_path),
        CommandRequest(command=f"{PY} -c \"raise RuntimeError('boom')\""),
    )
    assert not result.ok
    assert result.output_ref is not None
    text = await store.get_text(result.output_ref)
    assert "RuntimeError" in text


@pytest.mark.asyncio
async def test_runtime_does_not_persist_success(tmp_path: Path) -> None:
    """成功命令不持久化（output_ref 为 None）。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    runtime = ExecutionRuntime(
        project_root=tmp_path, environment_root=tmp_path, store=store
    )
    result = await runtime.run(
        _context(tmp_path), CommandRequest(command=f"{PY} -c \"print('ok')\"")
    )
    assert result.ok
    assert result.output_ref is None
