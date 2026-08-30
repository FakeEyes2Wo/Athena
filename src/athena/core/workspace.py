"""Contracts for isolated Git workspaces."""

from abc import ABC, abstractmethod
from pathlib import Path
from collections.abc import Awaitable, Callable

from pydantic import BaseModel

from athena.core.contracts import ArtifactRef, CommitHash

BinaryDiffWriter = Callable[[bytes], Awaitable[ArtifactRef]]


FRAMEWORK_OWNED_DIR = ".athena"
"""框架私有目录名：state / tree / logs / artifacts 都落在这里，agent 不得写。"""


def resolve_workspace_path(root: Path, rel: str) -> Path:
    """Resolve a workspace-relative path and reject escape outside ``root``."""
    candidate = (root / rel).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError(f"path escapes workspace: {rel}")
    return candidate


def is_framework_owned(path: Path, workspace_root: Path) -> bool:
    """``path`` 是否落在框架私有目录里——以工作区为界判定。

    只看「工作区之内」那一段有没有 ``.athena``。命名会话的工作区本身就在
    ``.athena/conversations/<sid>/workspaces/`` 下（``ResearchRuntime`` 收到
    ``state_root`` 时的布局），拿绝对路径一刀切会把 agent 自己的 worktree 也判成
    框架私有，于是它在自己的工作区里连一个文件都写不了、一条命令都跑不了。
    工作区之外的路径没有这层歧义，仍按绝对路径判定。
    """
    root = workspace_root.resolve()
    resolved = path.resolve()
    if resolved.is_relative_to(root):
        return FRAMEWORK_OWNED_DIR in resolved.relative_to(root).parts
    return FRAMEWORK_OWNED_DIR in resolved.parts


class GitWorkspaceError(RuntimeError):
    """Raised when a Git workspace operation cannot be completed."""


class GitWorkBranch(BaseModel):
    """The path, branch, and current checkpoint commit for one Git worktree."""

    path: str
    branch: str
    base_commit: CommitHash


class GitDiff(BaseModel):
    """A content-addressed binary diff with the paths changed from the base commit."""

    ref: ArtifactRef
    paths: tuple[str, ...]


class GitWorkspace(ABC):
    """Abstract operations for an isolated Git worktree."""

    @abstractmethod
    async def init(
        self,
        repo_path: Path,
        initial_file: str = "README.md",
        initial_content: str = "# Experiment Base",
    ) -> CommitHash:
        """Initialize a Git repository and return its initial commit."""

    @abstractmethod
    async def create(self, base_commit: CommitHash, branch: str) -> GitWorkBranch:
        """Create a temporary branch and isolated worktree from a commit."""

    @abstractmethod
    async def diff(self, workspace: GitWorkBranch) -> GitDiff:
        """Store the binary diff from the workspace's current HEAD."""

    @abstractmethod
    async def commit(
        self,
        workspace: GitWorkBranch,
        approved_diff: GitDiff,
        message: str,
    ) -> CommitHash:
        """Commit the last approved, unchanged diff and begin a new review cycle."""

    @abstractmethod
    async def restore_paths(
        self,
        workspace: GitWorkBranch,
        paths: tuple[str, ...],
    ) -> None:
        """Restore only these paths to the last reviewed tree."""

    @abstractmethod
    async def remove(
        self,
        workspace: GitWorkBranch,
        *,
        delete_branch: bool = False,
        force: bool = False,
    ) -> None:
        """Remove a workspace, optionally deleting its branch."""
