"""一个项目同时只允许一个 run。

``state.json`` 与 ``research_tree.json`` 是一次运行的持久记忆，而此前没有任何东西
阻止两个进程同时写它们——最后写入者获胜，静默生效。

真机（2026-08-30）：一次续跑还活着的时候又起了第二个进程。第二个从更早的检查点
重建了研究树并覆盖回去，三个已结算的实验——包括一个 PR-AUC 0.8716 的 SOTA——
从树里消失了。两个进程都还在正常跑，唯一的迹象是树变短了。
"""

import os
import subprocess
import sys
import time
import textwrap
from pathlib import Path

import pytest

from athena.core.project_lock import LOCK_NAME, ProjectBusyError, project_lock

_SRC = Path(__file__).resolve().parents[3] / "src"


def _spawn_holder(athena: Path) -> subprocess.Popen:
    """Start a process that holds the lock until killed."""
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(f"""
                import sys, time
                sys.path.insert(0, r"{_SRC}")
                from athena.core.project_lock import project_lock
                with project_lock(r"{athena}"):
                    print("held", flush=True)
                    time.sleep(30)
                """),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "held"
    return holder


def test_the_lock_is_taken_and_released(tmp_path: Path) -> None:
    athena = tmp_path / ".athena"

    with project_lock(athena) as path:
        assert path.name == LOCK_NAME
        # pid 记在同目录的 run.pid 里：Windows 上往被锁住的那段字节里写会弄坏句柄。
        assert f"pid {os.getpid()}" in (athena / "run.pid").read_text(encoding="utf-8")

    with project_lock(athena):
        pass  # released, so it can be taken again


def test_a_second_holder_is_refused(tmp_path: Path) -> None:
    """必须跨进程测：同进程内的文件锁在多数平台上是可重入的。"""
    athena = tmp_path / ".athena"
    athena.mkdir(parents=True)
    holder = _spawn_holder(athena)
    try:
        with pytest.raises(ProjectBusyError, match="already being run"):
            with project_lock(athena):
                pass
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_the_lock_dies_with_its_holder(tmp_path: Path) -> None:
    """崩掉的进程不该留下一把谁也拿不到的锁。"""
    athena = tmp_path / ".athena"
    athena.mkdir(parents=True)
    holder = _spawn_holder(athena)
    holder.kill()
    holder.wait(timeout=10)

    # 内核在进程句柄关闭时释放锁，但那不是"kill 返回即完成"——给它一小段时间。
    deadline = time.monotonic() + 10
    while True:
        try:
            with project_lock(athena):
                return
        except ProjectBusyError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)
