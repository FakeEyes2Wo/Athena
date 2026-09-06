"""DataScriptRunner — python-uv Bundle 冻结合同测试（supervisor_imp_docs Task 4）。

使用动态脚本名与最小 uv 项目布局，不包含任何数据集知识。
"""

import json
import subprocess

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.research.script_runner import (
    BundleMetadata,
    DataScriptRunner,
    _run_cmd,
    _run_cmd_capture,
    load_directory,
    pack_directory,
)

_ENTRYPOINT = "src/inspect_anything.py"


def _draft(tmp_path, entrypoint: str = _ENTRYPOINT) -> None:
    """写一个最小 uv 项目 draft（无外部依赖，entrypoint 声明式命名）。"""
    draft = tmp_path / "draft"
    script = draft / entrypoint
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "import json, sys\n"
        "def main():\n"
        "    req = json.load(open(sys.argv[sys.argv.index('--request') + 1]))\n"
        "    out = open(sys.argv[sys.argv.index('--output') + 1], 'w')\n"
        "    json.dump({'columns': list(req.get('data', {})), 'rows': 0}, out)\n"
        "main()\n",
        encoding="utf-8",
    )
    (draft / "pyproject.toml").write_text(
        "[project]\n"
        "name = 'draft'\n"
        "version = '0.1.0'\n"
        "requires-python = '>=3.11'\n"
        "dependencies = []\n",
        encoding="utf-8",
    )


def _stdout_draft(tmp_path) -> None:
    """写一个打印 primary 到 stdout 的 draft（metric.json 合同，不写 result.json）。"""
    draft = tmp_path / "draft"
    script = draft / _ENTRYPOINT
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "import json\n"
        "def main():\n"
        "    print(json.dumps({'primary': 0.956938}))\n"
        "main()\n",
        encoding="utf-8",
    )
    (draft / "pyproject.toml").write_text(
        "[project]\n"
        "name = 'draft'\n"
        "version = '0.1.0'\n"
        "requires-python = '>=3.11'\n"
        "dependencies = []\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_pack_load_directory_roundtrip(tmp_path) -> None:
    """pack_directory → load_directory 往返 {相对路径: bytes} 一致。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    preds = tmp_path / "preds"
    (preds / "sub").mkdir(parents=True)
    (preds / "a.csv").write_bytes(b"id,pred\n1,0\n")
    (preds / "sub" / "b.bin").write_bytes(b"\x00\x01\xff")

    ref = await pack_directory(store, preds)
    loaded = await load_directory(store, ref)

    assert loaded == {
        "a.csv": b"id,pred\n1,0\n",
        "sub/b.bin": b"\x00\x01\xff",
    }


@pytest.mark.asyncio
async def test_run_injects_bytes_and_nested(tmp_path) -> None:
    """extra_files 以字节注入并支持嵌套目录。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    workdir = tmp_path / "work"
    runner = DataScriptRunner(store=store, workdir=workdir)
    _stdout_draft(tmp_path)
    bundle = await runner.freeze(
        tmp_path / "draft", BundleMetadata(entrypoint=_ENTRYPOINT)
    )

    await runner.run(bundle, {}, extra_files={"sub/b.bin": b"\x00\x01\xff"})

    run_dirs = list(workdir.iterdir())
    assert len(run_dirs) == 1
    injected = run_dirs[0] / "sub" / "b.bin"
    assert injected.is_file()
    assert injected.read_bytes() == b"\x00\x01\xff"


@pytest.mark.asyncio
async def test_run_frozen_bundle_reads_stdout_primary(tmp_path) -> None:
    """metric.json 合同：eval 脚本打印 primary 到 stdout，不写 result.json。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    runner = DataScriptRunner(store=store, workdir=tmp_path / "work")
    _stdout_draft(tmp_path)
    bundle = await runner.freeze(
        tmp_path / "draft", BundleMetadata(entrypoint=_ENTRYPOINT)
    )
    result = await runner.run(bundle, {}, output_schema={"primary": None})
    assert result.outputs["primary"] == 0.956938


@pytest.mark.asyncio
async def test_frozen_bundle_uses_declared_entrypoint_and_uv(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    runner = DataScriptRunner(store=store, workdir=tmp_path / "work")
    _draft(tmp_path)
    bundle = await runner.freeze(
        tmp_path / "draft", BundleMetadata(entrypoint=_ENTRYPOINT)
    )
    assert bundle.entrypoint == _ENTRYPOINT
    assert bundle.runtime == "python-uv"
    assert bundle.lock_ref is not None
    assert bundle.tree_ref is not None  # 完整源码树已固化
    assert bundle.python_version is not None and bundle.python_version.startswith("3.")
    assert bundle.environment_hash is not None
    assert runner.command(bundle)[0:2] == ["uv", "run"]  # 不再 --frozen
    # 动态脚本名：frozen 命令只认声明 entrypoint，不按文件约定
    assert _ENTRYPOINT in runner.command(bundle)
    # 冻结的 entrypoint 头部写了「不能动」标记
    tree = json.loads(await store.get_text(bundle.tree_ref))
    entrypoint_bytes = await store.get_bytes(tree[_ENTRYPOINT])
    assert entrypoint_bytes.startswith(b"# ATHENA-FROZEN")


@pytest.mark.asyncio
async def test_run_frozen_bundle_executes_and_returns_output(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    runner = DataScriptRunner(store=store, workdir=tmp_path / "work")
    _draft(tmp_path)
    bundle = await runner.freeze(
        tmp_path / "draft", BundleMetadata(entrypoint=_ENTRYPOINT)
    )
    result = await runner.run(
        bundle, {"data": {"age": [1, 2]}}, output_schema={"columns": list}
    )
    assert result.strong_isolation is False
    assert result.outputs["columns"] == ["age"]
    assert result.outputs["rows"] == 0


@pytest.mark.asyncio
async def test_run_frozen_bundle_uses_configured_workdir(tmp_path) -> None:
    """Frozen script executions stay below the project-owned run directory."""
    store = LocalArtifactStore(tmp_path / "artifacts")
    workdir = tmp_path / ".athena" / "runs"
    runner = DataScriptRunner(store=store, workdir=workdir)
    _draft(tmp_path)
    bundle = await runner.freeze(
        tmp_path / "draft", BundleMetadata(entrypoint=_ENTRYPOINT)
    )

    await runner.run(bundle, {"data": {}})

    run_dirs = list(workdir.iterdir())
    assert len(run_dirs) == 1
    assert run_dirs[0].is_dir()


@pytest.mark.asyncio
async def test_run_dir_persists_declared_metrics_file(tmp_path, monkeypatch) -> None:
    """A live evaluator's declared public metrics table survives its temporary run."""
    evaluator = tmp_path / "evaluate"
    evaluator.mkdir()
    (evaluator / "evaluate.py").write_text("", encoding="utf-8")
    (evaluator / "metric.json").write_text(
        json.dumps(
            {
                "eval_script": "evaluate.py",
                "metrics_file": "metrics_public_test.csv",
            }
        ),
        encoding="utf-8",
    )
    metrics = b"team_name,task_id,F1\nAthena,example,0.8\n"

    def run_evaluator(_cmd, *, cwd):
        (cwd / "metrics_public_test.csv").write_bytes(metrics)
        return json.dumps({"primary": 0.8})

    monkeypatch.setattr("athena.research.script_runner._run_cmd_capture", run_evaluator)
    store = LocalArtifactStore(tmp_path / "artifacts")
    runner = DataScriptRunner(store=store, workdir=tmp_path / "runs")

    result = await runner.run_dir(evaluator, {}, output_schema={"primary": None})

    assert await store.get_bytes(result.outputs["metrics_ref"]) == metrics


@pytest.mark.asyncio
async def test_freeze_rejects_missing_declared_entrypoint(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    runner = DataScriptRunner(store=store, workdir=tmp_path / "work")
    _draft(tmp_path)
    with pytest.raises(FileNotFoundError, match="entrypoint missing"):
        await runner.freeze(
            tmp_path / "draft", BundleMetadata(entrypoint="src/nope.py")
        )


def test_run_cmd_timeout_raises_clear_error(tmp_path, monkeypatch) -> None:
    """固定命令超时 → TimeoutExpired（SubprocessError 子类，按可重试处理）。"""
    monkeypatch.setattr(
        "athena.research.script_runner.subprocess.run",
        lambda *_a, **_kw: (_ for _ in ()).throw(subprocess.TimeoutExpired("uv", 900)),
    )
    with pytest.raises(subprocess.TimeoutExpired, match="timed out"):
        _run_cmd(["uv", "lock"], cwd=tmp_path)
    with pytest.raises(subprocess.TimeoutExpired, match="timed out"):
        _run_cmd_capture(["uv", "run", "python", "--version"], cwd=tmp_path)


def test_a_failed_command_carries_its_stderr_into_the_message() -> None:
    """退出码说不出该改什么；变成 Agent 反馈的那条消息必须带上 stderr。

    真机（2026-08-16 A/B 的 survey-on 臂）：评估器在 stderr 写了
    ``4800 prediction ids not in labels``，传到 PREPARE 的却只有 "returned non-zero
    exit status 1"，于是它重试 11 次、每次交出同样的 6000 行预测，直到轮次预算耗尽。
    """
    import pathlib
    import subprocess
    import sys

    from athena.research.script_runner import ScriptCommandError, _run

    script = (
        "import sys; sys.stderr.write('4800 prediction ids not in labels'); sys.exit(1)"
    )
    with pytest.raises(ScriptCommandError) as caught:
        _run([sys.executable, "-c", script], cwd=pathlib.Path.cwd())

    assert "4800 prediction ids not in labels" in str(caught.value)
    # 类型不变，既有的 SubprocessError 捕获点照常生效
    assert isinstance(caught.value, subprocess.CalledProcessError)


def test_a_successful_command_still_returns_its_output() -> None:
    import pathlib
    import sys

    from athena.research.script_runner import _run

    completed = _run([sys.executable, "-c", "print('ok')"], cwd=pathlib.Path.cwd())

    assert completed.stdout.strip() == "ok"
    assert completed.returncode == 0
