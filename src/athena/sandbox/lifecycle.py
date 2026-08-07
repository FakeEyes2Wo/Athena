"""进程生命周期绑定。

ProcessLifecycle.bind_to_parent() 由 sandbox server 自身调用，
父进程退出时当前进程被 OS 自动清理；
ProcessLifecycle.create_parent_job() 与 assign_to_job() 由 Agent 侧调用，
通过 KILL_ON_JOB_CLOSE Job Object 清理 sandbox 进程树。
"""

import ctypes
import logging
import signal
import sys

if sys.platform == "win32":
    from ctypes import wintypes

    from athena.sandbox.limits import JOBOBJECT_EXTENDED_LIMIT_INFORMATION

logger = logging.getLogger(__name__)

# Linux prctl 选项：父进程死亡时向当前进程发送指定信号
PR_SET_PDEATHSIG = 1

# Win32 Job Object 常量
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001


class ProcessLifecycle:
    """平台感知的进程-父进程生命周期绑定。

    核心设计：Linux/macOS 用 prctl(PR_SET_PDEATHSIG) 让 sandbox 随父进程退出，
    Windows 由 Agent 侧创建 KILL_ON_JOB_CLOSE Job Object 实现等价清理。
    """

    @staticmethod
    def bind_to_parent() -> None:
        """将当前进程绑定到父进程生命周期：父进程退出时被 OS 自动清理。"""
        if sys.platform not in ("linux", "darwin"):
            # Windows 由 Agent 侧 create_parent_job() 的 Job Object 覆盖
            return
        try:
            libc = ctypes.CDLL(None)
            if libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL) != 0:
                logger.warning("prctl 设置父进程死亡信号失败")
        except Exception:
            # 容器等受限环境缺失 prctl → 无法自动清理，仅记录不阻塞启动
            logger.warning("prctl 不可用，父进程退出时 sandbox 进程可能残留")

    @staticmethod
    def create_parent_job() -> int | None:
        """创建 KILL_ON_JOB_CLOSE Job Object 并返回句柄；非 Windows 返回 None。

        Agent 启动 sandbox server 子进程后调用，将子进程赋给该 Job Object。
        Agent 退出时 OS 自动关闭句柄，sandbox 进程树被清理。
        """
        if sys.platform != "win32":
            return None

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        hjob = kernel32.CreateJobObjectW(None, None)
        if not hjob:
            return None

        # 配置 KILL_ON_JOB_CLOSE：句柄全部关闭时自动终止 Job 内进程
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ret = kernel32.SetInformationJobObject(
            hjob,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ret:
            kernel32.CloseHandle(hjob)
            return None

        return int(hjob)

    @staticmethod
    def assign_to_job(job_handle: int, pid: int) -> bool:
        """将指定 PID 的进程赋给 Job Object，成功返回 True。"""
        if sys.platform != "win32":
            return False

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        hproc = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not hproc:
            return False
        ret = kernel32.AssignProcessToJobObject(job_handle, hproc)
        kernel32.CloseHandle(hproc)
        return bool(ret)


if __name__ == "__main__":
    # 使用示例：sandbox server 启动时绑定父进程；Agent 侧可选创建 Job Object
    ProcessLifecycle.bind_to_parent()
    job = ProcessLifecycle.create_parent_job()
    if job is not None:
        print(f"平台 {sys.platform} 已创建 Job Object（句柄 {job}）")
    else:
        print(f"平台 {sys.platform} 已绑定父进程生命周期")
