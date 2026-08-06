"""Contracts for isolated Git workspaces."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Awaitable, Callable

from pydantic import BaseModel

from athena.core.contracts import ArtifactRef, CommitHash

BinaryDiffWriter = Callable[[bytes], Awaitable[ArtifactRef]]


class GitWorkspaceError(RuntimeError):
    """Raised when a Git workspace operation cannot be completed."""


class GitWorkBranch(BaseModel):
    """The path, branch, and base commit for one Git worktree."""

    path: str
    branch: str
    base_commit: CommitHash


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
    async def diff(self, workspace: GitWorkBranch) -> ArtifactRef:
        """Store the binary diff from the workspace base commit."""

    @abstractmethod
    async def commit(
        self,
        workspace: GitWorkBranch,
        approved_diff_ref: ArtifactRef,
        message: str,
    ) -> CommitHash:
        """Commit the last approved, unchanged workspace diff."""

    @abstractmethod
    async def remove(
        self,
        workspace: GitWorkBranch,
        *,
        delete_branch: bool = False,
        force: bool = False,
    ) -> None:
        """Remove a workspace, optionally deleting its branch."""
