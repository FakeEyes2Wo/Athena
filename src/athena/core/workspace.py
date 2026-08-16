"""Contracts for isolated Git workspaces."""

from abc import ABC, abstractmethod
from pathlib import Path
from collections.abc import Awaitable, Callable

from pydantic import BaseModel

from athena.core.contracts import ArtifactRef, CommitHash

BinaryDiffWriter = Callable[[bytes], Awaitable[ArtifactRef]]


def resolve_workspace_path(root: Path, rel: str) -> Path:
    """Resolve a workspace-relative path and reject escape outside ``root``."""
    candidate = (root / rel).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError(f"path escapes workspace: {rel}")
    return candidate


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
