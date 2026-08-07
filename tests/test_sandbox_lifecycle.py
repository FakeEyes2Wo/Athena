"""ProcessLifecycle 进程生命周期绑定模块的单元测试。"""

import ctypes
import subprocess
import sys
import time

import pytest

from athena.sandbox.lifecycle import ProcessLifecycle

if sys.platform == "win32":
    from ctypes import wintypes


def _close_job_handle(handle: int) -> None:
    """关闭 Job Object 句柄，触发 KILL_ON_JOB_CLOSE。"""
    kernel32 = ctypes.windll.kernel32
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle(handle)


def _child_in_job(pid: int) -> bool:
    """判断子进程是否已被宿主环境放入某个 Job Object。"""
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.IsProcessInJob.restype = wintypes.BOOL
    kernel32.IsProcessInJob.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.LPBOOL,
    ]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    hproc = kernel32.OpenProcess(
        0x0400, False, pid
    )  # PROCESS_QUERY_LIMITED_INFORMATION
    if not hproc:
        return False
    in_job = wintypes.BOOL()
    kernel32.IsProcessInJob(hproc, None, ctypes.byref(in_job))
    kernel32.CloseHandle(hproc)
    return bool(in_job.value)


def test_bind_to_parent_no_raise():
    # 任意平台调用不应抛异常（prctl 失败时仅记录日志）
    ProcessLifecycle.bind_to_parent()


def test_create_parent_job_non_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert ProcessLifecycle.create_parent_job() is None


def test_assign_to_job_non_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert ProcessLifecycle.assign_to_job(0, 1) is False


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 支持 Job Object")
def test_win32_create_parent_job_smoke():
    job = ProcessLifecycle.create_parent_job()
    assert isinstance(job, int)
    assert job > 0
    _close_job_handle(job)


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 支持 Job Object")
def test_win32_assign_to_job_invalid_pid():
    job = ProcessLifecycle.create_parent_job()
    assert job is not None
    # PID 0 为系统 Idle 进程，用户态无法打开 → OpenProcess 失败返回 False
    assert ProcessLifecycle.assign_to_job(job, 0) is False
    _close_job_handle(job)


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 支持 Job Object")
def test_win32_assign_to_job_success():
    """子进程可赋入 Job，句柄关闭后由 KILL_ON_JOB_CLOSE 终止（回归 QUOTA 权限缺陷）。"""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
    time.sleep(0.5)
    job = ProcessLifecycle.create_parent_job()
    try:
        assert job is not None
        ok = ProcessLifecycle.assign_to_job(job, child.pid)
        if not ok and _child_in_job(child.pid):
            # 宿主环境已把子进程放入 Job → 无法二次赋值，属环境限制而非代码缺陷
            pytest.skip("子进程已处于宿主 Job，无法再次赋值")
        assert ok
        _close_job_handle(job)
        job = None
        child.wait(timeout=5)
    finally:
        if job is not None:
            _close_job_handle(job)
        if child.poll() is None:
            child.kill()
            child.wait()
