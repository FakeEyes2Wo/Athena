"""DataScriptRunner — python-uv Bundle 冻结合同测试（supervisor_imp_docs Task 4）。

使用动态脚本名与最小 uv 项目布局，不包含任何数据集知识。
"""

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.research.script_runner import BundleMetadata, DataScriptRunner

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
    assert runner.command(bundle)[0:3] == ["uv", "run", "--frozen"]
    # 动态脚本名：frozen 命令只认声明 entrypoint，不按文件约定
    assert _ENTRYPOINT in runner.command(bundle)


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
async def test_freeze_rejects_missing_declared_entrypoint(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    runner = DataScriptRunner(store=store, workdir=tmp_path / "work")
    _draft(tmp_path)
    with pytest.raises(FileNotFoundError, match="entrypoint missing"):
        await runner.freeze(
            tmp_path / "draft", BundleMetadata(entrypoint="src/nope.py")
        )
