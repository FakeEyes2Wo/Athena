"""远程执行：常驻通道、远端 supervisor、工作区镜像。

对外只暴露 ``SshBackend``——它实现 ``ExecutionBackend``，因此 ``ExecutionRuntime``
之上的代码完全看不到这里的任何东西。
"""

from athena.execution.remote.channel import (
    RemoteChannel,
    RemoteError,
    RemoteTransport,
    SubprocessTransport,
)
from athena.execution.remote.mirror import FileEntry, WorkspaceMirror, local_manifest
from athena.execution.remote.ssh import SshBackend, SshHost, SshTransport

__all__ = [
    "FileEntry",
    "RemoteChannel",
    "RemoteError",
    "RemoteTransport",
    "SshBackend",
    "SshHost",
    "SshTransport",
    "SubprocessTransport",
    "WorkspaceMirror",
    "local_manifest",
]
