"""显式给的搜索预算要能覆盖持久化的旧值。

既有项目的 ``state.json`` 是整份原样加载的，只有 ``experiment_timeout_s`` /
``data_root`` 会被显式覆盖。于是 ``--max-search-experiments`` 在续跑时**静默失效**：
想把预算从 4 加到 16 的人看不出它没生效，只会看到搜索照旧在 4 停下。
"""

from pathlib import Path

from athena.research.config import ResearchOptions, SearchLimits
from athena.research.runtime import ResearchRuntime
from athena.research.supervisor.state import ResearchState


def _project(tmp_path: Path, limit: int) -> Path:
    project = tmp_path / "proj"
    (project / ".athena").mkdir(parents=True)
    ResearchState(
        status="IDLE", phase="SEARCH", search_limit=limit, concurrency=1
    ).save(project / ".athena" / "state.json")
    return project


def test_an_explicit_budget_replaces_the_persisted_one(tmp_path: Path) -> None:
    project = _project(tmp_path, 4)

    runtime = ResearchRuntime(
        project_root=project,
        research=ResearchOptions(search=SearchLimits(search_limit=16)),
    )

    assert runtime.state.search_limit == 16
    assert ResearchState.load(project / ".athena" / "state.json").search_limit == 16


def test_omitting_the_flag_keeps_the_persisted_budget(tmp_path: Path) -> None:
    """没给标志位时不该把用户先前设的 16 悄悄打回默认的 10。"""
    project = _project(tmp_path, 16)

    runtime = ResearchRuntime(project_root=project)

    assert runtime.state.search_limit == 16


def test_a_new_project_falls_back_to_the_default(tmp_path: Path) -> None:
    project = tmp_path / "fresh"
    project.mkdir()

    assert ResearchRuntime(project_root=project).state.search_limit == 10
