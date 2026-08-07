"""import 白名单与 SafeOS 代理。"""

import os as _real_os

# 允许的数据科学库和标准库模块
ALLOWED_IMPORTS: frozenset[str] = frozenset({
    "pandas", "numpy", "matplotlib", "seaborn", "scipy", "sklearn",
    "collections", "itertools", "math", "statistics", "json", "csv",
    "pathlib", "io", "typing", "datetime", "warnings",
})


class SafeOSError(PermissionError):
    """访问被禁 os 函数时抛出。"""


# os 中允许暴露的函数名
_OS_WHITELIST: frozenset[str] = frozenset({
    "listdir", "makedirs", "stat", "remove", "rename", "rmdir",
    "getcwd", "sep", "path",
})


# os 中显式禁止的敏感函数名（用于更清晰的错误信息）
_OS_BLOCKED: frozenset[str] = frozenset({
    "system", "popen", "execv", "execve", "execl", "execle", "execlp",
    "execlpe", "execvp", "execvpe", "spawnl", "spawnle", "spawnlp",
    "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
    "kill", "chmod", "chown", "setuid", "setgid", "environ",
    "startfile", "fork", "putenv", "unsetenv",
})


class _SafeOSProxy:
    """os 模块的安全代理。

    只暴露 _OS_WHITELIST 中的函数/属性；禁止 _OS_BLOCKED 中的函数；
    未在白名单也不在黑名单的函数返回 PermissionError。
    path 子模块被替换为一个具有相同路径操作函数的代理对象。
    """

    def __getattr__(self, name: str) -> object:
        if name in _OS_BLOCKED:
            raise SafeOSError(f"os.{name} 在 sandbox 中被禁止")
        if name in _OS_WHITELIST:
            return getattr(_real_os, name)
        raise SafeOSError(f"os.{name} 不在 sandbox 白名单中")


def build_safe_os() -> object:
    """返回 SafeOS 代理实例，用于替换 sys.modules['os']。"""
    return _SafeOSProxy()
