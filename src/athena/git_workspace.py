"""Local Git implementation of the Git workspace contract."""

import asyncio
import hashlib
import re
from pathlib import Path
from uuid import uuid4

from athena.core.contracts import ArtifactRef, CommitHash
from athena.core.workspace import (
    BinaryDiffWriter,
    GitWorkBranch,
    GitWorkspace,
    GitWorkspaceError,
)


class LocalGitWorkspace(GitWorkspace):
    """Manage Git worktrees on the local filesystem."""

    def __init__(
        self, repo_path: Path, worktree_root: Path, diff_writer: BinaryDiffWriter
    ):
        self._repo = repo_path.resolve()
        self._root = worktree_root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._diff_writer = diff_writer
        self._lock = asyncio.Lock()
        self._states: dict[Path, dict] = {}
        self._repo_initialized = False

    async def init(
        self,
        repo_path: Path | None = None,
        initial_file: str = "README.md",
        initial_content: str = "# Experiment Base",
    ) -> CommitHash:
        path = (repo_path or self._repo).resolve()
        if self._repo_initialized and path == self._repo:
            output = await self._git("rev-parse", "HEAD", cwd=path)
            return output.decode().strip()

        path.mkdir(parents=True, exist_ok=True)
        await self._git("init", "-b", "main", cwd=path)

        init_file = path / initial_file
        init_file.write_text(initial_content)
        await self._git("add", initial_file, cwd=path)
        await self._git("commit", "-m", "initial commit", cwd=path)

        output = await self._git("rev-parse", "HEAD", cwd=path)
        commit_hash = output.decode().strip()
        if self._repo != path:
            self._repo = path
        self._repo_initialized = True
        return commit_hash

    async def create(self, base_commit: CommitHash, branch: str) -> GitWorkBranch:
        async with self._lock:
            commit = await self._resolve_commit(base_commit)
            self._validate_branch(branch)

            existing = await self._git(
                "show-ref", "--verify", f"refs/heads/{branch}", check=False
            )
            if existing.strip():
                raise GitWorkspaceError(f"分支已存在：{branch}")

            path = self._root / f"athena-{uuid4().hex}"
            branch_ref = f"refs/heads/{branch}"
            await self._git("update-ref", branch_ref, commit, "0" * 40)
            try:
                await self._git("worktree", "add", str(path), branch)
            except Exception:
                # worktree add 失败 → 回滚已创建的分支引用后抛出领域错误
                await self._git("branch", "-D", branch, check=False)
                raise GitWorkspaceError("Branch 创建失败")

            workspace = GitWorkBranch(path=str(path), branch=branch, base_commit=commit)
            self._states[path] = {
                "workspace": workspace,
                "review": None,
                "committed": None,
            }
            return workspace

    async def diff(self, workspace: GitWorkBranch) -> ArtifactRef:
        async with self._lock:
            state = self._get_state(workspace)
            if state["committed"]:
                raise GitWorkspaceError("已提交，不能再 diff")
            path = Path(workspace.path)

            await self._git("add", "-A", cwd=path)
            staged = await self._git(
                "diff", "--cached", "--binary", workspace.base_commit, cwd=path
            )
            if await self._git("diff", cwd=path) or await self._git(
                "ls-files", "--others", cwd=path
            ):
                raise GitWorkspaceError("工作区仍有未暂存变更")

            artifact = await self._diff_writer(staged)
            state["review"] = {
                "artifact": artifact,
                "sha256": hashlib.sha256(staged).hexdigest(),
                "tree": (await self._git("write-tree", cwd=path)).decode().strip(),
                "head": (await self._git("rev-parse", "HEAD", cwd=path))
                .decode()
                .strip(),
                "empty": not staged,
            }
            return artifact

    async def commit(
        self, workspace: GitWorkBranch, approved_artifact: ArtifactRef, message: str
    ) -> CommitHash:
        async with self._lock:
            state = self._get_state(workspace)
            review = state["review"]
            if not review or review["artifact"] != approved_artifact:
                raise GitWorkspaceError("批准的 artifact 与当前 diff 不匹配")
            if state["committed"]:
                return state["committed"]

            path = Path(workspace.path)
            current_head = (
                (await self._git("rev-parse", "HEAD", cwd=path)).decode().strip()
            )
            current_tree = (await self._git("write-tree", cwd=path)).decode().strip()
            staged = await self._git(
                "diff", "--cached", "--binary", workspace.base_commit, cwd=path
            )
            unstaged = await self._git("diff", cwd=path)
            untracked = await self._git(
                "ls-files", "--others", "--exclude-standard", cwd=path
            )
            if (
                hashlib.sha256(staged).hexdigest() != review["sha256"]
                or current_head != review["head"]
                or current_tree != review["tree"]
                or unstaged
                or untracked
            ):
                raise GitWorkspaceError("工作区在审查后被修改")

            if review["empty"]:
                state["committed"] = current_head
                return current_head

            new_commit = (
                (
                    await self._git(
                        "commit-tree",
                        current_tree,
                        "-p",
                        current_head,
                        "-m",
                        message,
                        cwd=path,
                    )
                )
                .decode()
                .strip()
            )
            await self._git(
                "update-ref",
                f"refs/heads/{workspace.branch}",
                new_commit,
                current_head,
                cwd=path,
            )
            state["committed"] = new_commit
            return new_commit

    async def remove(
        self,
        workspace: GitWorkBranch,
        *,
        delete_branch: bool = False,
        force: bool = False,
    ) -> None:
        async with self._lock:
            self._get_state(workspace)
            path = Path(workspace.path)
            if path.exists():
                if not force and await self._git("status", "--porcelain", cwd=path):
                    raise GitWorkspaceError("worktree 包含未提交变更")
                args = ["worktree", "remove"]
                if force:
                    args.append("--force")
                args.append(str(path))
                await self._git(*args)
            if delete_branch:
                await self._git("branch", "-D", workspace.branch)
            self._states.pop(path, None)

    def _get_state(self, workspace: GitWorkBranch) -> dict:
        path = Path(workspace.path).resolve()
        if path not in self._states:
            raise GitWorkspaceError("未知的 worktree")
        return self._states[path]

    async def _resolve_commit(self, commit: str) -> str:
        if not commit or not isinstance(commit, str):
            raise GitWorkspaceError("commit 不能为空")
        commit = commit.strip()
        if not commit:
            raise GitWorkspaceError("commit 不能为空")
        try:
            output = await self._git("rev-parse", "--verify", f"{commit}^{{commit}}")
        except GitWorkspaceError as exc:
            raise GitWorkspaceError(f"未知或非 commit 对象：{commit}") from exc
        return output.decode("ascii").strip()

    def _validate_branch(self, branch: str) -> None:
        if (
            not branch
            or branch.startswith(("-", "/", "."))
            or branch.endswith(("/", ".", ".lock"))
            or ".." in branch
            or "@{" in branch
            or any(char.isspace() or char in "~^:?*[\\" for char in branch)
        ):
            raise GitWorkspaceError("非法分支名")

    async def _git(
        self, *args: str, cwd: Path | None = None, check: bool = True
    ) -> bytes:
        cwd = cwd or self._repo
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(cwd),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if check and proc.returncode != 0:
            raise GitWorkspaceError(
                f"git {' '.join(args)} 失败: {stderr.decode()[:200]}"
            )
        return stdout
