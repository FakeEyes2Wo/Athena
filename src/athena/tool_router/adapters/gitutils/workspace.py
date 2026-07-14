"""使用 Git worktree 隔离并发实验的代码快照边界。

代码版本仅由 Git 保存，不设计自定义 snapshot 格式。每个实验 worktree 拥有独立
目录、HEAD 与 index，并共享 Git 对象库；并发创建、提交和删除仍需要细致锁机制，
但锁的具体协议尚未在设计中确定。
"""

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field

from athena.core.schemas import ArtifactRef, CommitHash


class GitWorktree(BaseModel):
    """一个实验专用的隔离 worktree 及其临时分支来源。"""

    path: str = Field(description="Absolute path of the experiment worktree.")
    branch: str = Field(description="Unique temporary branch used by the experiment.")
    base_commit: CommitHash = Field(description="Commit from which the experiment was created.")


class GitWorkspace(ABC):
    """Git 原生代码快照生命周期接口；所有变更须先经 Supervisor 批准。"""

    @abstractmethod
    async def create(self, base_commit: CommitHash, branch: str) -> GitWorktree:
        """从指定 commit 创建唯一临时分支和隔离 worktree。"""

    @abstractmethod
    async def diff(self, workspace: GitWorktree) -> ArtifactRef:
        """将 worktree 相对 base commit 的 diff 保存为不可变 artifact。"""

    @abstractmethod
    async def commit(self, workspace: GitWorktree, message: str) -> CommitHash:
        """提交已批准的变更并返回 commit hash；不提交未经审查的修改。"""

    @abstractmethod
    async def remove(self, workspace: GitWorktree, *, delete_branch: bool = False) -> None:
        """移除 worktree，并按调用方选择清理临时分支。"""
