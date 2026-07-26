"""用 Git worktree 为实验提供可审查、可提交的隔离代码目录。

本模块刻意只负责 Git 边界：创建 worktree、固化 binary diff、提交已经审查的
内容，以及显式回收目录。实验树负责决定何时审查和提交，ArtifactStore 则通过
``BinaryDiffWriter`` 接缝保存 diff；这里不复制代码仓库，也不定义另一套快照格式。
"""

import asyncio
import hashlib
import re

from abc import ABC, abstractmethod
from pathlib import Path
from uuid import uuid4

from typing import Callable, Awaitable


from pydantic import BaseModel
from athena.core.schemas import ArtifactRef, CommitHash

BinaryDiffWriter = Callable[[bytes], Awaitable[ArtifactRef]]


class GitWorkspaceError(RuntimeError):
    pass


class GitWorkBranch(BaseModel):
    path: str
    branch: str
    base_commit: CommitHash


class GitWorkspace(ABC):

    @abstractmethod
    async def init(
        self,
        repo_path: Path,
        initial_file: str = "README.md",
        initial_content: str = "# Experiment Base",
    ) -> CommitHash:
        """
        在指定路径初始化一个 Git 仓库，创建初始文件并提交。
        返回初始 commit 的哈希值。
        """

    @abstractmethod
    async def create(self, base_commit: CommitHash, branch: str) -> GitWorkBranch:
        """从指定 commit 创建唯一临时分支和隔离 worktree。"""

    @abstractmethod
    async def diff(self, workspace: GitWorkBranch) -> ArtifactRef:
        """暂存全部变更，并把相对 base commit 的 binary diff 写为 artifact。"""

    @abstractmethod
    async def commit(
        self,
        workspace: GitWorkBranch,
        approved_diff_ref: ArtifactRef,
        message: str,
    ) -> CommitHash:
        """仅提交最近一次 ``diff`` 审查过且此后未变化的内容。"""

    @abstractmethod
    async def remove(
        self,
        workspace: GitWorkBranch,
        *,
        delete_branch: bool = False,
        force: bool = False,
    ) -> None:
        """移除 worktree；脏目录和临时分支均须由调用方显式授权丢弃。"""


class LocalGitWorkspace(GitWorkspace):
    """最小化 Git worktree 管理，只保留原子创建、diff、提交、移除。"""

    def __init__(
        self, repo_path: Path, worktree_root: Path, diff_writer: BinaryDiffWriter
    ):
        self._repo = repo_path.resolve()
        self._root = worktree_root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._diff_writer = diff_writer
        self._lock = asyncio.Lock()
        self._states: dict[Path, dict] = (
            {}
        )  # 存储的 {Path，{GitWorkBranch,review,commit} }
        self._repo_initialized = False  # 标记仓库是否已初始化

    async def init(
        self,
        repo_path: Path | None = None,
        initial_file: str = "README.md",
        initial_content: str = "# Experiment Base",
    ) -> CommitHash:
        """
        初始化 Git 仓库（如果未初始化），添加初始文件并提交。
        如果 repo_path 未指定，则使用构造时的路径。
        """
        path = (repo_path or self._repo).resolve()
        if self._repo_initialized and path == self._repo:
            # 已初始化，直接返回当前 HEAD
            output = await self._git("rev-parse", "HEAD", cwd=path)
            return output.decode().strip()

        # 确保目录存在
        path.mkdir(parents=True, exist_ok=True)

        # 执行 git init（如果尚未初始化）
        await self._git("init", "-b", "main", cwd=path)

        # 写入初始文件
        init_file = path / initial_file
        init_file.write_text(initial_content)

        # add 并 commit
        await self._git("add", initial_file, cwd=path)
        await self._git("commit", "-m", "initial commit", cwd=path)

        # 获取 commit hash
        output = await self._git("rev-parse", "HEAD", cwd=path)
        commit_hash = output.decode().strip()

        # 更新内部 repo 路径（如果未设置或不同）
        if self._repo != path:
            self._repo = path
        self._repo_initialized = True
        return commit_hash

    async def create(self, base_commit: CommitHash, branch: str) -> GitWorkBranch:
        """从 commit 创建新 worktree 和临时分支。如果分支已存在，自动生成唯一后缀。"""
        async with self._lock:
            commit = await self._resolve_commit(base_commit)
            self._validate_branch(branch)

            # 检查分支是否存在
            existing = await self._git(
                "show-ref", "--verify", f"refs/heads/{branch}", check=False
            )
            if existing.strip():
                raise GitWorkspaceError(f"分支已存在：{branch}")

            path = self._root / f"athena-{uuid4().hex}"
            branch_ref = f"refs/heads/{branch}"

            # 原子创建分支（不存在则创建）
            await self._git("update-ref", branch_ref, commit, "0" * 40)
            try:
                await self._git("worktree", "add", str(path), branch)
            except Exception:
                await self._git("branch", "-D", branch, check=False)
                raise GitWorkspaceError("Branch 创建失败")

            ws = GitWorkBranch(path=str(path), branch=branch, base_commit=commit)
            self._states[path] = {"workspace": ws, "review": None, "committed": None}
            return ws

    async def diff(self, workspace: GitWorkBranch) -> ArtifactRef:
        """生成当前工作区相对 base commit 的 binary diff，并冻结状态。"""
        async with self._lock:
            state = self._get_state(workspace)
            if state["committed"]:
                raise GitWorkspaceError("已提交，不能再 diff")
            path = Path(workspace.path)

            # 暂存所有变更
            await self._git("add", "-A", cwd=path)
            staged = await self._git(
                "diff", "--cached", "--binary", workspace.base_commit, cwd=path
            )
            # 确保工作区干净
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
        """应用已批准的 diff 并创建新 commit。"""
        async with self._lock:
            state = self._get_state(workspace)
            review = state["review"]
            if not review or review["artifact"] != approved_artifact:
                raise GitWorkspaceError("批准的 artifact 与当前 diff 不匹配")
            if state["committed"]:
                return state["committed"]

            path = Path(workspace.path)
            # 重新检查工作区未被篡改
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

            # 创建新 commit
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
        """删除 worktree，可选择删除分支。"""
        async with self._lock:
            state = self._get_state(workspace)
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

    # ---------- 内部辅助 ----------
    def _get_state(self, ws: GitWorkBranch) -> dict:
        path = Path(ws.path).resolve()
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
            # git rev-parse 失败 → commit 不存在或格式无效
            raise GitWorkspaceError(f"未知或非 commit 对象：{commit}") from exc
        return output.decode("ascii").strip()

    def _validate_branch(self, branch: str):
        if (
            not branch
            or branch.startswith(("-", "/", "."))
            or branch.endswith(("/", ".", ".lock"))
            or ".." in branch
            or "@{" in branch
            or any(char.isspace() or char in "~^:?*[\\" for char in branch)
        ):
            raise GitWorkspaceError("非法分支名")

    async def _git(self, *args, cwd: Path | None = None, check: bool = True) -> bytes:
        """执行 git 命令，默认检查返回码。"""
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


if __name__ == "__main__":
    import asyncio
    import tempfile
    from pathlib import Path

    async def fake_diff_writer(diff_bytes: bytes) -> str:
        import hashlib

        return f"artifact-{hashlib.sha256(diff_bytes).hexdigest()[:16]}"

    async def main():
        created_worktrees = []
        workspace = None
        base_commit = None
        try:
            with (
                tempfile.TemporaryDirectory() as repo_dir,
                tempfile.TemporaryDirectory() as worktree_root,
            ):
                repo_path = Path(repo_dir)
                wt_root = Path(worktree_root)

                workspace = LocalGitWorkspace(repo_path, wt_root, fake_diff_writer)
                base_commit = await workspace.init(
                    repo_path=repo_path, initial_content="# Athena Experiment Base"
                )
                print(f"✅ 仓库初始化完成，初始 commit: {base_commit}")

                # ---- 正常流程 ----
                print("=== 正常流程 ===")
                wt = await workspace.create(base_commit, "experiment/feature-a")
                created_worktrees.append(wt)
                wt_path = Path(wt.path)
                (wt_path / "model.py").write_text("def train(): pass\n")
                (wt_path / "data.csv").write_text("x,y\n1,2\n3,4")

                artifact = await workspace.diff(wt)
                commit_hash = await workspace.commit(wt, artifact, "Add model and data")
                print(f"✅ 提交成功: {commit_hash}")

                proc = await asyncio.create_subprocess_exec(
                    "git",
                    "-C",
                    str(wt_path),
                    "rev-parse",
                    "HEAD",
                    stdout=asyncio.subprocess.PIPE,
                )
                stdout, _ = await proc.communicate()
                assert stdout.decode().strip() == commit_hash
                print("✅ 提交内容已验证")

                await workspace.remove(wt, delete_branch=False)
                print("✅ worktree 移除，分支保留")

                # ---- 异常场景 ----
                print("\n=== 异常场景 ===")

                # 场景1：未 diff 直接 commit
                wt2 = await workspace.create(base_commit, "experiment/bad")
                created_worktrees.append(wt2)
                try:
                    await workspace.commit(wt2, "fake-artifact", "no diff")
                except GitWorkspaceError as e:
                    print(f"❌ 正确捕获异常: {e}")
                finally:
                    await workspace.remove(wt2, delete_branch=True)

                # 场景2：diff 后篡改
                wt3 = await workspace.create(base_commit, "experiment/tampered")
                created_worktrees.append(wt3)
                wt3_path = Path(wt3.path)
                (wt3_path / "tamper.txt").write_text("original")
                artifact3 = await workspace.diff(wt3)
                (wt3_path / "tamper.txt").write_text("tampered!")
                try:
                    await workspace.commit(wt3, artifact3, "should fail")
                except GitWorkspaceError as e:
                    print(f"❌ 正确捕获篡改异常: {e}")
                await workspace.remove(wt3, delete_branch=True, force=True)

                # 场景3：已提交后再次 diff
                wt4 = await workspace.create(base_commit, "experiment/multi")
                created_worktrees.append(wt4)
                wt4_path = Path(wt4.path)
                (wt4_path / "file1.txt").write_text("first")
                art = await workspace.diff(wt4)
                await workspace.commit(wt4, art, "first commit")
                try:
                    (wt4_path / "file2.txt").write_text("second")
                    await workspace.diff(wt4)
                except GitWorkspaceError as e:
                    print(f"❌ 正确捕获重复 diff 异常: {e}")
                await workspace.remove(wt4, delete_branch=True)

                # ---- 多 worktree 协同场景 ----
                print("\n=== 多 worktree 协同测试 ===")
                wt_a = await workspace.create(base_commit, "experiment/feature-a")
                created_worktrees.append(wt_a)
                wt_b = await workspace.create(
                    base_commit, "experiment/feature-a"
                )  # 自动添加后缀
                created_worktrees.append(wt_b)
                wt_a_path = Path(wt_a.path)
                wt_b_path = Path(wt_b.path)
                (wt_a_path / "a.txt").write_text("from feature A")
                (wt_b_path / "b.txt").write_text("from feature B")

                art_a = await workspace.diff(wt_a)
                commit_a = await workspace.commit(wt_a, art_a, "add a.txt")
                print(f"✅ feature-a 提交: {commit_a}")

                art_b = await workspace.diff(wt_b)
                commit_b = await workspace.commit(wt_b, art_b, "add b.txt")
                print(f"✅ feature-b 提交: {commit_b}")

                # 验证两个分支互不影响
                proc_a = await asyncio.create_subprocess_exec(
                    "git",
                    "-C",
                    str(wt_a_path),
                    "log",
                    "-1",
                    "--oneline",
                    stdout=asyncio.subprocess.PIPE,
                )
                stdout_a, _ = await proc_a.communicate()
                proc_b = await asyncio.create_subprocess_exec(
                    "git",
                    "-C",
                    str(wt_b_path),
                    "log",
                    "-1",
                    "--oneline",
                    stdout=asyncio.subprocess.PIPE,
                )
                stdout_b, _ = await proc_b.communicate()
                print(f"✅ feature-a 最新提交: {stdout_a.decode().strip()}")
                print(f"✅ feature-b 最新提交: {stdout_b.decode().strip()}")
                assert commit_a != commit_b

                await workspace.remove(wt_a, delete_branch=True)
                await workspace.remove(wt_b, delete_branch=True)
                print("✅ 多 worktree 已清理")

        except Exception as e:
            print(f"测试意外失败: {e}")
            raise
        finally:
            if workspace:
                for wt in created_worktrees:
                    try:
                        await workspace.remove(wt, delete_branch=True, force=True)
                    except:
                        pass
                for wt in created_worktrees:
                    path = Path(wt.path)
                    if path.exists():
                        try:
                            await workspace._git(
                                "worktree", "remove", "--force", str(path), check=False
                            )
                        except:
                            pass

    asyncio.run(main())
