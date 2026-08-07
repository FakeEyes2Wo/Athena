"""平台感知的子进程资源限制。

wrapper 子进程启动后第一时间调用 SandboxLimits.apply_memory_limit()
将自身内存限制在 MEMORY_MB。
"""

import sys

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        """与 SetInformationJobObject 配套的扩展限制结构体，供模块内外复用。"""

        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
            ("IoCounters", ctypes.c_ulonglong * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

elif sys.platform in ("linux", "darwin"):
    import resource


class SandboxLimits:
    """子进程资源限制，平台感知。"""

    MEMORY_MB: int = 2048
    """硬内存上限，单位 MiB。"""

    @staticmethod
    def ensure_platform_support() -> None:
        """验证当前平台受支持；不满足时抛 RuntimeError。"""
        if sys.platform == "win32":
            if not hasattr(ctypes, "windll"):
                raise RuntimeError("Windows 平台缺少 ctypes.windll")
        elif sys.platform not in ("linux", "darwin"):
            raise RuntimeError(f"不支持的平台: {sys.platform}")

    @staticmethod
    def apply_memory_limit() -> None:
        """将当前进程内存限制为 MEMORY_MB。"""
        limit_bytes = SandboxLimits.MEMORY_MB * 1024 * 1024
        if sys.platform in ("linux", "darwin"):
            resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
        elif sys.platform == "win32":
            _win32_set_memory_limit(SandboxLimits.MEMORY_MB)


def _win32_set_memory_limit(limit_mb: int) -> None:
    """通过 Win32 Job Object 限制进程内存。"""
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
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE

    # JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x100, JOB_OBJECT_LIMIT_JOB_MEMORY = 0x200
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.LimitFlags = 0x100 | 0x200
    info.ProcessMemoryLimit = limit_mb * 1024 * 1024
    info.JobMemoryLimit = limit_mb * 1024 * 1024

    # 创建 Job Object
    hjob = kernel32.CreateJobObjectW(None, None)
    if not hjob:
        raise OSError("CreateJobObjectW 失败")

    # 配置内存限制（JobObjectExtendedLimitInformation = 9）
    ret = kernel32.SetInformationJobObject(
        hjob,
        9,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ret:
        raise OSError("SetInformationJobObject 失败")

    # 把当前进程赋给 Job Object
    hproc = kernel32.GetCurrentProcess()
    ret = kernel32.AssignProcessToJobObject(hjob, hproc)
    if not ret:
        raise OSError("AssignProcessToJobObject 失败")


if __name__ == "__main__":
    # 使用示例：校验平台支持，确认当前环境可应用内存限制
    SandboxLimits.ensure_platform_support()
    print(f"平台 {sys.platform} 受支持，内存限制 {SandboxLimits.MEMORY_MB} MiB")
