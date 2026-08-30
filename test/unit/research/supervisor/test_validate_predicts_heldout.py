"""VALIDATE must score the held-out split, not the one SEARCH already used.

真机（2026-08-30）：平台自己切分的项目里，VALIDATE 从来没能打出过分。候选按数据
契约预测 ``search_features.csv``，路径写死在它自己的源码里，而 ``experiment.json``
的 argv 是冻结的；VALIDATE 重跑同一条命令，产出的还是 search 行的预测。final
evaluator 要 final split 的 14539 行，交集为零，于是报：

    {"primary": 0.0}
    ERROR: 14539 ground truth rows have no prediction

看起来像评估器坏了或者模型坏了，两者都不是。``final_features.csv`` 在被 splitter
写出之后，全代码库再无任何地方引用它。
"""

import json
from pathlib import Path

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.core.workspace import GitWorkBranch
from athena.execution.runtime import EnvironmentManager, ExecutionRuntime
from athena.research.supervisor.validation import _execute_predictions

ROW_ID = "__athena_row_id"


def _features(path: Path, ids: range) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = "\n".join(f"{i},{i * 0.5}" for i in ids)
    path.write_text(f"{ROW_ID},feature\n{rows}\n", encoding="utf-8")
    return path


class _Git:
    def __init__(self) -> None:
        self.restored: list[tuple[str, ...]] = []

    async def restore_paths(self, workspace, paths) -> None:
        del workspace
        self.restored.append(tuple(paths))


class _Execution:
    """Records what ATHENA_PREDICT_FEATURES was during each command."""

    def __init__(self, environment_root: Path) -> None:
        self.environment_root = environment_root
        self.env_during_run: list[str | None] = []
        self._predict: Path | None = None

    def set_predict_features(self, path) -> None:
        self._predict = Path(path) if path is not None else None

    def predicting(self, path):
        from contextlib import contextmanager

        @contextmanager
        def scope():
            previous = self._predict
            self.set_predict_features(path)
            try:
                yield
            finally:
                self.set_predict_features(previous)

        return scope()

    async def run(self, context, command=None, *, argv=None, **kwargs):
        del context, command, argv, kwargs
        self.env_during_run.append(
            str(self._predict) if self._predict is not None else None
        )

        class _Ok:
            ok = True
            stdout = ""
            stderr = ""
            exit_code = 0

        return _Ok()


def _workspace(tmp_path: Path, predicted_ids: range) -> tuple[Path, GitWorkBranch]:
    workdir = tmp_path / "validate"
    (workdir / "predictions").mkdir(parents=True)
    rows = "\n".join(f"{i},0.4" for i in predicted_ids)
    (workdir / "predictions" / "pred.csv").write_text(
        f"{ROW_ID},score\n{rows}\n", encoding="utf-8"
    )
    (workdir / "experiment.json").write_text(
        json.dumps(
            {
                "version": 1,
                "commands": [["python", "solution/train_model.py"]],
                "outputs": {"predictions": "predictions", "report": "REPORT.md"},
            }
        ),
        encoding="utf-8",
    )
    return workdir, GitWorkBranch(
        path=str(workdir), branch="validate", base_commit="sota-a"
    )


def test_the_variable_is_only_set_when_a_target_is_given(tmp_path: Path) -> None:
    manager = EnvironmentManager(
        project_root=tmp_path, environment_root=tmp_path, host={"PATH": "/usr/bin"}
    )
    assert "ATHENA_PREDICT_FEATURES" not in manager.build_env(tmp_path)

    target = tmp_path / "data_split" / "search_features.csv"
    manager.set_predict_features(target)
    assert manager.build_env(tmp_path)["ATHENA_PREDICT_FEATURES"] == str(target)

    manager.set_predict_features(None)
    assert "ATHENA_PREDICT_FEATURES" not in manager.build_env(tmp_path)


def test_the_flip_is_scoped_to_one_block(tmp_path: Path) -> None:
    """VALIDATE 只在重跑期间换靶。留着不还原，之后每条 SEARCH 命令都会去预测
    留出集——正是划分存在的意义所要防的那种泄漏。"""
    search = tmp_path / "data_split" / "search_features.csv"
    final = tmp_path / "data_split" / "final_features.csv"
    runtime = ExecutionRuntime(project_root=tmp_path, environment_root=tmp_path)
    runtime.set_predict_features(search)
    environment = runtime.backend.environment

    assert environment.predict_features == search
    with runtime.predicting(final):
        assert environment.predict_features == final
    assert environment.predict_features == search


@pytest.mark.asyncio
async def test_the_rerun_is_pointed_at_the_held_out_split(tmp_path: Path) -> None:
    final = _features(tmp_path / "data_split" / "final_features.csv", range(100, 110))
    workdir, workspace = _workspace(tmp_path, range(100, 110))
    execution = _Execution(workdir)

    await _execute_predictions(
        execution=execution,
        git=_Git(),
        workspace=workspace,
        store=LocalArtifactStore(tmp_path / "artifacts"),
        publish=None,
        predict_features=final,
    )

    assert execution.env_during_run == [str(final)]


@pytest.mark.asyncio
async def test_a_hardcoded_path_fails_with_the_real_reason(tmp_path: Path) -> None:
    """零交集只有一个成因，报错就该直说，而不是留一个 {"primary": 0.0} 让人猜。"""
    final = _features(tmp_path / "data_split" / "final_features.csv", range(100, 110))
    # 候选把 search 路径写死了，于是重跑产出的是 search 行的预测。
    workdir, workspace = _workspace(tmp_path, range(0, 10))
    del workdir

    with pytest.raises(ValueError) as caught:
        await _execute_predictions(
            execution=_Execution(tmp_path),
            git=_Git(),
            workspace=workspace,
            store=LocalArtifactStore(tmp_path / "artifacts"),
            publish=None,
            predict_features=final,
        )

    message = str(caught.value)
    assert "10 of 10" in message
    assert "overlap 0" in message
    assert "ATHENA_PREDICT_FEATURES" in message


@pytest.mark.asyncio
async def test_full_coverage_passes(tmp_path: Path) -> None:
    final = _features(tmp_path / "data_split" / "final_features.csv", range(100, 110))
    _, workspace = _workspace(tmp_path, range(100, 110))

    ref, rel_path = await _execute_predictions(
        execution=_Execution(tmp_path),
        git=_Git(),
        workspace=workspace,
        store=LocalArtifactStore(tmp_path / "artifacts"),
        publish=None,
        predict_features=final,
    )

    assert rel_path == "predictions"
    assert ref
