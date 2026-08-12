"""Local Git implementation of the Git workspace contract."""

import asyncio
import hashlib
import inspect
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from athena.core.contracts import ArtifactRef, CommitHash
from athena.core.workspace import (
    BinaryDiffWriter,
    GitDiff,
    GitWorkBranch,
    GitWorkspace,
    GitWorkspaceError,
)


@dataclass(slots=True)
class _Review:
    artifact: ArtifactRef
    sha256: str
    tree: str
    head: CommitHash
    empty: bool
    pending_commit: CommitHash | None = None


@dataclass(slots=True)
class _Committed:
    artifact: ArtifactRef
    commit: CommitHash


@dataclass(slots=True)
class _WorkspaceState:
    workspace: GitWorkBranch
    review: _Review | None = None
    committed: _Committed | None = None


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
        self._states: dict[Path, _WorkspaceState] = {}
        self._repo_initialized = False

    async def init(
        self,
        repo_path: Path | None = None,
        initial_file: str = "README.md",
        initial_content: str = "# Experiment Base",
    ) -> CommitHash:
        """初始化 git 仓库并创建 base commit；已初始化则返回当前 HEAD。"""
        path = (repo_path or self._repo).resolve()
        if self._repo_initialized and path == self._repo:
            output = await self._git("rev-parse", "HEAD", cwd=path)
            return output.decode().strip()

        path.mkdir(parents=True, exist_ok=True)
        await self._git("init", "-b", "main", cwd=path)

        init_file = path / initial_file
        init_file.write_text(initial_content)
        await self._git("add", "-A", cwd=path)
        await self._git("commit", "-m", "initial commit", cwd=path)

        output = await self._git("rev-parse", "HEAD", cwd=path)
        commit_hash = output.decode().strip()
        if self._repo != path:
            self._repo = path
        self._repo_initialized = True
        return commit_hash

    async def create(self, base_commit: CommitHash, branch: str) -> GitWorkBranch:
        """为分支创建 worktree；分支已存在时幂等返回现有 worktree。"""
        async with self._lock:
            commit = await self._resolve_commit(base_commit)
            self._validate_branch(branch)

            existing = await self._git(
                "show-ref", "--verify", f"refs/heads/{branch}", check=False
            )
            if existing.strip():
                # 幂等恢复：分支已存在 → 返回匹配的现有 worktree（design §idempotent）
                workspace = await self._find_worktree(branch)
                if workspace is not None:
                    return workspace
                raise GitWorkspaceError(f"分支已存在但无 worktree：{branch}")

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
            self._states[path] = _WorkspaceState(workspace)
            return workspace

    async def _find_worktree(self, branch: str) -> GitWorkBranch | None:
        """按分支返回已存在的 worktree（幂等恢复）。"""
        listing = await self._git("worktree", "list", "--porcelain", cwd=self._repo)
        for block in listing.decode("utf-8").split("\n\n"):
            path = br = None
            for line in block.splitlines():
                if line.startswith("worktree "):
                    path = line[len("worktree ") :]
                elif line.startswith("branch "):
                    br = line[len("branch ") :].removeprefix("refs/heads/")
            if br == branch and path:
                worktree_path = Path(path).resolve()
                registered = self._states.get(worktree_path)
                if registered is not None:
                    workspace = registered.workspace
                    if workspace.branch != branch:
                        raise GitWorkspaceError("worktree 分支与已注册状态不匹配")
                    return workspace
                workspace = GitWorkBranch(
                    path=str(worktree_path),
                    branch=branch,
                    base_commit=await self._resolve_commit(branch),
                )
                self._states[worktree_path] = _WorkspaceState(workspace)
                return workspace
        return None

    async def diff(self, workspace: GitWorkBranch) -> GitDiff:
        """计算 worktree 相对当前 HEAD 的内容寻址 diff。"""
        async with self._lock:
            state = self._get_state(workspace)
            path = Path(workspace.path)
            current_head = (
                (await self._git("rev-parse", "HEAD", cwd=path)).decode().strip()
            )

            await self._git("add", "-A", cwd=path)
            staged = await self._git(
                "diff", "--cached", "--binary", current_head, cwd=path
            )
            if await self._git("diff", cwd=path) or await self._git(
                "ls-files", "--others", "--exclude-standard", cwd=path
            ):
                raise GitWorkspaceError("工作区仍有未暂存变更")

            names = await self._git(
                "diff",
                "--cached",
                "--name-only",
                "-z",
                current_head,
                cwd=path,
            )
            changed = {name for name in names.decode("utf-8").split("\0") if name}
            # 相对 base 的净 diff 会吞掉「先暂存、后删除且从未提交」的文件；
            # 用上一轮暂存树再 diff 一次，还原这类删除路径，保证 paths 不漏掉删除。
            previous_tree = state.review.tree if state.review else current_head
            previous_names = await self._git(
                "diff", "--cached", "--name-only", "-z", previous_tree, cwd=path
            )
            changed.update(
                name for name in previous_names.decode("utf-8").split("\0") if name
            )
            paths = tuple(sorted(changed))

            result = self._diff_writer(staged)
            artifact = await result if inspect.isawaitable(result) else result
            state.review = _Review(
                artifact=artifact,
                sha256=hashlib.sha256(staged).hexdigest(),
                tree=(await self._git("write-tree", cwd=path)).decode().strip(),
                head=current_head,
                empty=not staged,
            )
            return GitDiff(ref=artifact, paths=paths)

    async def commit(
        self, workspace: GitWorkBranch, approved_diff: GitDiff, message: str
    ) -> CommitHash:
        """提交批准的 diff 到 worktree 分支，返回 commit hash。"""
        async with self._lock:
            state = self._get_state(workspace)
            review = state.review
            if not review:
                committed = state.committed
                if committed and committed.artifact == approved_diff.ref:
                    self._record_commit(
                        state, workspace, approved_diff.ref, committed.commit
                    )
                    return committed.commit
                reconciled = await self._reconcile_installed_review(
                    workspace, approved_diff
                )
                if reconciled is not None:
                    self._record_commit(state, workspace, approved_diff.ref, reconciled)
                    return reconciled
                raise GitWorkspaceError("批准的 artifact 与当前 diff 不匹配")
            if review.artifact != approved_diff.ref:
                raise GitWorkspaceError("批准的 artifact 与当前 diff 不匹配")

            path = Path(workspace.path)
            current_head = (
                (await self._git("rev-parse", "HEAD", cwd=path)).decode().strip()
            )
            pending_commit = review.pending_commit
            if pending_commit and current_head == pending_commit:
                marker = await self._resolve_review_marker(workspace.branch)
                if marker != pending_commit:
                    raise GitWorkspaceError("已安装的提交缺少匹配的审查标记")
                self._record_commit(state, workspace, approved_diff.ref, pending_commit)
                return pending_commit
            current_tree = (await self._git("write-tree", cwd=path)).decode().strip()
            staged = await self._git(
                "diff", "--cached", "--binary", review.head, cwd=path
            )
            unstaged = await self._git("diff", cwd=path)
            untracked = await self._git(
                "ls-files", "--others", "--exclude-standard", cwd=path
            )
            if (
                hashlib.sha256(staged).hexdigest() != review.sha256
                or current_head != review.head
                or current_tree != review.tree
                or unstaged
                or untracked
            ):
                raise GitWorkspaceError("工作区在审查后被修改")

            if review.empty:
                self._record_commit(state, workspace, approved_diff.ref, current_head)
                return current_head

            new_commit = review.pending_commit
            if new_commit is None:
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
                review.pending_commit = new_commit
            await self._git(
                "update-ref",
                self._review_ref(workspace.branch),
                new_commit,
                cwd=path,
            )
            await self._git(
                "update-ref",
                f"refs/heads/{workspace.branch}",
                new_commit,
                current_head,
                cwd=path,
            )
            self._record_commit(state, workspace, approved_diff.ref, new_commit)
            return new_commit

    async def restore_paths(
        self,
        workspace: GitWorkBranch,
        paths: tuple[str, ...],
    ) -> None:
        """Restore declared outputs without hiding other post-review edits."""

        async with self._lock:
            state = self._get_state(workspace)
            if state.review is None:
                raise GitWorkspaceError("restore_paths requires a reviewed diff")
            root = Path(workspace.path).resolve()
            resolved: list[tuple[str, Path]] = []
            for rel in paths:
                relative = Path(rel)
                candidate = (root / relative).resolve()
                if (
                    not rel
                    or relative.is_absolute()
                    or ".." in relative.parts
                    or candidate == root
                    or not candidate.is_relative_to(root)
                ):
                    raise GitWorkspaceError("restore path escapes workspace")
                resolved.append((rel, candidate))

            for rel, candidate in resolved:
                reviewed_path = await self._git(
                    "ls-tree",
                    "--name-only",
                    "-z",
                    state.review.tree,
                    "--",
                    rel,
                    cwd=root,
                )
                if reviewed_path:
                    await self._git(
                        "restore",
                        f"--source={state.review.tree}",
                        "--staged",
                        "--worktree",
                        "--",
                        rel,
                        cwd=root,
                    )
                    continue
                await self._git(
                    "rm", "--cached", "--ignore-unmatch", "--", rel, cwd=root
                )
                candidate.unlink(missing_ok=True)

    @staticmethod
    def _record_commit(
        state: _WorkspaceState,
        workspace: GitWorkBranch,
        artifact: ArtifactRef,
        commit: CommitHash,
    ) -> None:
        state.review = None
        state.committed = _Committed(artifact, commit)
        state.workspace.base_commit = commit
        workspace.base_commit = commit

    async def _reconcile_installed_review(
        self, workspace: GitWorkBranch, approved_diff: GitDiff
    ) -> CommitHash | None:
        marker = await self._resolve_review_marker(workspace.branch)
        if marker is None:
            return None

        path = Path(workspace.path)
        current_head = (await self._git("rev-parse", "HEAD", cwd=path)).decode().strip()
        if current_head != marker:
            return None

        parent = await self._git(
            "rev-parse", "--verify", f"{marker}^{{commit}}^", cwd=path, check=False
        )
        if not parent.strip():
            return None
        reviewed_diff = await self._git(
            "diff",
            "--binary",
            parent.decode("ascii").strip(),
            marker,
            cwd=path,
        )
        result = self._diff_writer(reviewed_diff)
        artifact = await result if inspect.isawaitable(result) else result
        if artifact != approved_diff.ref:
            return None
        return marker

    async def _resolve_review_marker(self, branch: str) -> CommitHash | None:
        output = await self._git(
            "rev-parse",
            "--verify",
            f"{self._review_ref(branch)}^{{commit}}",
            cwd=self._repo,
            check=False,
        )
        return output.decode("ascii").strip() or None

    @staticmethod
    def _review_ref(branch: str) -> str:
        return f"refs/athena/reviews/{branch}"

    async def remove(
        self,
        workspace: GitWorkBranch,
        *,
        delete_branch: bool = False,
        force: bool = False,
    ) -> None:
        """移除 worktree（可选删分支）。"""
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

    def _get_state(self, workspace: GitWorkBranch) -> _WorkspaceState:
        path = Path(workspace.path).resolve()
        if path not in self._states:
            raise GitWorkspaceError("未知的 worktree")
        state = self._states[path]
        if state.workspace.branch != workspace.branch:
            raise GitWorkspaceError("worktree 分支与已注册状态不匹配")
        return state

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
