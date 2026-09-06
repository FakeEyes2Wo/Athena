"""崩过一次的运行必须还能续跑。

``subscribe`` 会把当前状态立刻回放给订阅者，而 CLI 把 ``FAILED`` 当作本次运行的
终态并退出。状态在 ``start()`` 之前就被回放，于是崩过一次的项目再也起不来——
每次 `Athena-cli run` 只打印一行 ``phase=SEARCH status=FAILED`` 就退出，什么都没做。

真机（2026-08-29）：SEARCH 崩在一个解包错误上之后，续跑完全无效。
"""

from pathlib import Path

from athena.research.runtime import ResearchRuntime
from athena.research.supervisor.state import ResearchState


def _failed_state(project: Path) -> Path:
    athena = project / ".athena"
    athena.mkdir(parents=True, exist_ok=True)
    state = ResearchState(
        status="FAILED", phase="SEARCH", search_limit=4, concurrency=1
    )
    return state.save(athena / "state.json")


def test_a_failed_run_starts_again_as_idle(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    _failed_state(project)

    runtime = ResearchRuntime(project_root=project)

    assert runtime.state.status == "IDLE"
    assert runtime.state.phase == "SEARCH", "阶段要保留，续跑才有意义"


def test_a_healthy_status_is_left_alone(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    athena = project / ".athena"
    athena.mkdir()
    ResearchState(status="WAITING", phase="SEARCH", search_limit=4, concurrency=1).save(
        athena / "state.json"
    )

    runtime = ResearchRuntime(project_root=project)

    assert runtime.state.status == "WAITING"
