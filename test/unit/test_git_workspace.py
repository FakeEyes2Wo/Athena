"""真实临时仓库上的 LocalGitWorkspace 单元测试。"""

import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import pytest

from athena.core.workspace import GitDiff, GitWorkspaceError, GitWorkBranch
from athena.core.git_workspace import LocalGitWorkspace


class LocalGitWorkspaceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        root = Path(self._temp.name)
        self.repo = root / "repo"
        self.worktree_root = root / "worktrees"
        self.repo.mkdir()

        self._git("init")
        self._git("config", "user.name", "Athena Test")
        self._git("config", "user.email", "athena@example.invalid")
        (self.repo / "seed.txt").write_text("baseline\n", encoding="utf-8")
        self._git("add", "seed.txt")
        self._git("commit", "--no-gpg-sign", "-m", "baseline")
        self.base_commit = self._git("rev-parse", "HEAD").stdout.strip()
        self._git("branch", "already-exists", self.base_commit)

        self.artifacts: dict[str, bytes] = {}

        async def write_diff(content: bytes) -> str:
            digest = hashlib.sha256(content).hexdigest()
            ref = f"artifact://git-diff/{digest}"
            self.artifacts[ref] = content
            return ref

        self.manager = LocalGitWorkspace(self.repo, self.worktree_root, write_diff)
        self.created: list[GitWorkBranch] = []

    async def asyncTearDown(self) -> None:
        # 失败断言也不应把注册中的 worktree 留给 TemporaryDirectory 清理。
        for workspace in reversed(self.created):
            try:
                await self.manager.remove(workspace, delete_branch=True, force=True)
            except GitWorkspaceError:
                pass
        self._temp.cleanup()

    def _git(
        self, *args: str, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(cwd or self.repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    async def _create(self, branch: str) -> GitWorkBranch:
        workspace = await self.manager.create(self.base_commit, branch)
        self.created.append(workspace)
        return workspace

    async def test_diff_rejects_post_review_change_then_commits_new_review(
        self,
    ) -> None:
        workspace = await self._create("experiment/reviewed")
        path = Path(workspace.path)
        target = path / "new.txt"
        target.write_text("first version\n", encoding="utf-8")
        (path / "weights.bin").write_bytes(bytes(range(256)) * 2)

        first_diff = await self.manager.diff(workspace)
        self.assertIn(b"diff --git a/new.txt b/new.txt", self.artifacts[first_diff.ref])
        self.assertIn(b"first version", self.artifacts[first_diff.ref])
        self.assertIn(b"GIT binary patch", self.artifacts[first_diff.ref])

        # diff 之后的任何 tracked 变化都必须重新进入审查流程。
        target.write_text("approved version\n", encoding="utf-8")
        with self.assertRaises(GitWorkspaceError):
            await self.manager.commit(
                workspace, first_diff, "must not commit stale review"
            )

        second_diff = await self.manager.diff(workspace)
        self.assertNotEqual(first_diff.ref, second_diff.ref)
        with self.assertRaises(GitWorkspaceError):
            await self.manager.commit(
                workspace, first_diff, "must bind the approved reference"
            )
        surprise = path / "not-reviewed.txt"
        surprise.write_text("not reviewed\n", encoding="utf-8")
        with self.assertRaises(GitWorkspaceError):
            await self.manager.commit(
                workspace, second_diff, "must not commit untracked file"
            )
        surprise.unlink()
        commit = await self.manager.commit(
            workspace, second_diff, "checkpoint approved diff"
        )
        saved = self._git("show", f"{commit}:new.txt", cwd=path).stdout
        self.assertEqual("approved version\n", saved)
        self.assertEqual("", self._git("status", "--porcelain", cwd=path).stdout)
        self.assertEqual(
            commit,
            await self.manager.commit(
                workspace, second_diff, "idempotent retry after uncertain response"
            ),
        )

        await self.manager.remove(workspace, delete_branch=True)
        self.assertFalse(path.exists())
        branch = subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "show-ref",
                "--verify",
                "--quiet",
                "refs/heads/experiment/reviewed",
            ],
            check=False,
        )
        self.assertEqual(1, branch.returncode)

    async def test_empty_diff_reuses_head_without_creating_commit(self) -> None:
        workspace = await self._create("experiment/config-only")

        diff = await self.manager.diff(workspace)
        self.assertEqual(b"", self.artifacts[diff.ref])
        self.assertEqual(
            self.base_commit,
            await self.manager.commit(workspace, diff, "configuration-only experiment"),
        )
        self.assertEqual(
            self.base_commit,
            self._git("rev-parse", "HEAD", cwd=Path(workspace.path)).stdout.strip(),
        )
        await self.manager.remove(workspace, delete_branch=True)

    async def test_workspace_supports_multiple_reviewed_commits(self) -> None:
        workspace = await self._create("athena/plan/h1")
        path = Path(workspace.path)
        model = path / "model.py"

        model.write_text("v1\n", encoding="utf-8")
        first_diff = await self.manager.diff(workspace)
        first_commit = await self.manager.commit(workspace, first_diff, "score 0.80")

        model.write_text("v2\n", encoding="utf-8")
        second_diff = await self.manager.diff(workspace)
        second_commit = await self.manager.commit(workspace, second_diff, "score 0.82")

        self.assertNotEqual(first_commit, second_commit)
        self.assertEqual(
            second_commit,
            self._git("rev-parse", "HEAD", cwd=path).stdout.strip(),
        )
        self.assertEqual("v2\n", self._git("show", "HEAD:model.py", cwd=path).stdout)

    async def test_create_recovers_stable_branch_without_losing_commits(self) -> None:
        workspace = await self._create("athena/plan/recover")
        path = Path(workspace.path)
        model = path / "model.py"
        model.write_text("v1\n", encoding="utf-8")
        reviewed = await self.manager.diff(workspace)
        first_commit = await self.manager.commit(workspace, reviewed, "score 0.80")

        async def write_recovered_diff(content: bytes) -> str:
            digest = hashlib.sha256(content).hexdigest()
            ref = f"artifact://git-diff/{digest}"
            self.artifacts[ref] = content
            return ref

        recovered_manager = LocalGitWorkspace(
            self.repo, self.worktree_root, write_recovered_diff
        )
        recovered = await recovered_manager.create(
            self.base_commit, "athena/plan/recover"
        )

        self.assertEqual(workspace.path, recovered.path)
        self.assertEqual(first_commit, recovered.base_commit)
        self.assertEqual("v1\n", self._git("show", "HEAD:model.py", cwd=path).stdout)

        model.write_text("v2\n", encoding="utf-8")
        second_diff = await recovered_manager.diff(recovered)
        second_commit = await recovered_manager.commit(
            recovered, second_diff, "score 0.82"
        )
        self.assertNotEqual(first_commit, second_commit)
        self.assertEqual(
            second_commit,
            self._git("rev-parse", "HEAD", cwd=path).stdout.strip(),
        )

    async def test_diff_ignores_runtime_outputs_excluded_by_gitignore(self) -> None:
        workspace = await self._create("experiment/ignored-runtime")
        path = Path(workspace.path)
        (path / ".gitignore").write_text("runtime.log\n", encoding="utf-8")
        (path / "run_experiment.py").write_text("print('ok')\n", encoding="utf-8")
        (path / "runtime.log").write_text("observed output\n", encoding="utf-8")

        diff = await self.manager.diff(workspace)

        self.assertIn(b"run_experiment.py", self.artifacts[diff.ref])
        self.assertNotIn(b"diff --git a/runtime.log", self.artifacts[diff.ref])

    async def test_restore_paths_restores_reviewed_output_and_preserves_source_change(
        self,
    ) -> None:
        workspace = await self._create("experiment/restore-reviewed-output")
        root = Path(workspace.path)
        source = root / "solution.py"
        output = root / "predictions.csv"
        source.write_text("VERSION = 1\n", encoding="utf-8")
        output.write_text("id,prediction\n1,0\n", encoding="utf-8")
        reviewed = await self.manager.diff(workspace)

        source.write_text("VERSION = 2\n", encoding="utf-8")
        output.write_text("id,prediction\n1,1\n", encoding="utf-8")
        await self.manager.restore_paths(workspace, ("predictions.csv",))

        self.assertEqual("id,prediction\n1,0\n", output.read_text(encoding="utf-8"))
        self.assertEqual("VERSION = 2\n", source.read_text(encoding="utf-8"))
        self.assertNotEqual(reviewed, await self.manager.diff(workspace))

    async def test_restore_paths_removes_execution_only_output(self) -> None:
        workspace = await self._create("experiment/remove-generated-output")
        root = Path(workspace.path)
        source = root / "solution.py"
        output = root / "predictions.csv"
        source.write_text("VERSION = 1\n", encoding="utf-8")
        reviewed = await self.manager.diff(workspace)

        source.write_text("VERSION = 2\n", encoding="utf-8")
        output.write_text("id,prediction\n1,1\n", encoding="utf-8")
        await self.manager.restore_paths(workspace, ("predictions.csv",))

        self.assertFalse(output.exists())
        self.assertEqual("VERSION = 2\n", source.read_text(encoding="utf-8"))
        self.assertNotEqual(reviewed, await self.manager.diff(workspace))

    async def test_restore_paths_rejects_paths_outside_workspace(self) -> None:
        workspace = await self._create("experiment/reject-output-escape")
        outside = Path(self._temp.name) / "outside.csv"
        outside.write_text("keep\n", encoding="utf-8")
        await self.manager.diff(workspace)

        for invalid in (str(outside), "../outside.csv", "nested/../outside.csv"):
            with self.subTest(path=invalid):
                with self.assertRaises(GitWorkspaceError):
                    await self.manager.restore_paths(workspace, (invalid,))

        self.assertEqual("keep\n", outside.read_text(encoding="utf-8"))

    async def test_restore_paths_requires_reviewed_tree(self) -> None:
        workspace = await self._create("experiment/restore-before-review")

        with self.assertRaises(GitWorkspaceError):
            await self.manager.restore_paths(workspace, ("predictions.csv",))

    async def test_dirty_remove_requires_explicit_force(self) -> None:
        workspace = await self._create("experiment/discard")
        path = Path(workspace.path)
        (path / "dirty.txt").write_text("discard me\n", encoding="utf-8")

        with self.assertRaises(GitWorkspaceError):
            await self.manager.remove(workspace)
        self.assertTrue(path.exists())

        await self.manager.remove(workspace, delete_branch=True, force=True)
        self.assertFalse(path.exists())

    async def test_rejects_invalid_or_unowned_inputs(self) -> None:
        with self.assertRaises(GitWorkspaceError):
            await self.manager.create("not-a-hash", "experiment/invalid-commit")
        with self.assertRaises(GitWorkspaceError):
            await self.manager.create("f" * 40, "experiment/unknown-commit")
        with self.assertRaises(GitWorkspaceError):
            await self.manager.create(self.base_commit, "../invalid")
        with self.assertRaises(GitWorkspaceError):
            await self.manager.create(self.base_commit, "already-exists")

    async def test_create_is_idempotent(self) -> None:
        """分支已存在且有 worktree → 幂等恢复返回同一 worktree，不重复创建。"""
        first = await self.manager.create(self.base_commit, "experiment/e1")
        self.created.append(first)
        second = await self.manager.create(self.base_commit, "experiment/e1")
        self.assertEqual(first.path, second.path)
        self.assertEqual(first.branch, second.branch)

        forged = GitWorkBranch(
            path=str(Path(self._temp.name) / "outside"),
            branch="experiment/forged",
            base_commit=self.base_commit,
        )
        with self.assertRaises(GitWorkspaceError):
            await self.manager.diff(forged)

        forged_inside = forged.model_copy(
            update={"path": str(self.worktree_root / "never-created")}
        )
        with self.assertRaises(GitWorkspaceError):
            await self.manager.diff(forged_inside)

        forged_registered = first.model_copy(
            update={"branch": "experiment/different-identity"}
        )
        with self.assertRaises(GitWorkspaceError):
            await self.manager.diff(forged_registered)

        workspace = await self._create("experiment/no-review")
        with self.assertRaises(GitWorkspaceError):
            await self.manager.commit(
                workspace,
                GitDiff(ref="artifact://diff/missing", paths=()),
                "not reviewed",
            )

    async def test_create_rebuilds_stale_worktree_after_project_copy(self) -> None:
        """复制/迁移项目后 worktree 注册指向旧路径 → create 重建 fresh worktree。"""
        await self._create("athena/prepare")

        copy_root = Path(self._temp.name) / "copy"
        copied_repo = copy_root / "repo"
        copied_worktrees = copy_root / "worktrees"
        shutil.copytree(self.repo, copied_repo)

        async def write_diff(content: bytes) -> str:
            digest = hashlib.sha256(content).hexdigest()
            ref = f"artifact://git-diff-copy/{digest}"
            self.artifacts[ref] = content
            return ref

        copied = LocalGitWorkspace(copied_repo, copied_worktrees, write_diff)
        fresh = await copied.create(self.base_commit, "athena/prepare")
        self.assertTrue(Path(fresh.path).is_relative_to(copied_worktrees.resolve()))
        self.assertTrue(Path(fresh.path).is_dir())

    async def test_init_creates_nested_repo_instead_of_walking_up(self) -> None:
        """空 repo 目录位于父 git 仓库内时，init 必须就地建 .git，不能向上走到父仓。"""
        nested = self.repo / "nested-project" / "repo"
        nested.mkdir(parents=True)

        async def write_diff(content: bytes) -> str:
            digest = hashlib.sha256(content).hexdigest()
            ref = f"artifact://git-diff-nested/{digest}"
            self.artifacts[ref] = content
            return ref

        manager = LocalGitWorkspace(
            nested, self.repo / "nested-project" / "worktrees", write_diff
        )
        commit = await manager.init()
        # 就地建 .git，而不是沿用父仓 self.repo 的 HEAD
        self.assertTrue((nested / ".git").is_dir())
        self.assertEqual(
            commit, self._git("rev-parse", "HEAD", cwd=nested).stdout.strip()
        )
        workspace = await manager.create(commit, "athena/prepare")
        self.assertTrue(Path(workspace.path).is_dir())
        await manager.remove(workspace, delete_branch=True, force=True)

    async def test_create_preserves_active_review(self) -> None:
        workspace = await self._create("athena/plan/preserve-review")
        path = Path(workspace.path)
        (path / "model.py").write_text("reviewed\n", encoding="utf-8")
        approved = await self.manager.diff(workspace)

        recovered = await self.manager.create(
            self.base_commit, "athena/plan/preserve-review"
        )
        commit = await self.manager.commit(recovered, approved, "score 0.80")

        self.assertEqual(
            commit, self._git("rev-parse", "HEAD", cwd=path).stdout.strip()
        )

    async def test_create_preserves_accepted_commit_retry(self) -> None:
        workspace = await self._create("athena/plan/preserve-retry")
        path = Path(workspace.path)
        (path / "model.py").write_text("accepted\n", encoding="utf-8")
        approved = await self.manager.diff(workspace)
        commit = await self.manager.commit(workspace, approved, "score 0.80")

        recovered = await self.manager.create(
            self.base_commit, "athena/plan/preserve-retry"
        )

        self.assertEqual(
            commit,
            await self.manager.commit(recovered, approved, "uncertain response retry"),
        )

    async def test_create_reports_commit_made_through_rehydrated_handle(self) -> None:
        workspace = await self._create("athena/plan/rehydrated-handle")
        path = Path(workspace.path)
        (path / "model.py").write_text("accepted\n", encoding="utf-8")
        approved = await self.manager.diff(workspace)
        rehydrated = GitWorkBranch.model_validate(workspace.model_dump())

        commit = await self.manager.commit(rehydrated, approved, "score 0.80")
        recovered = await self.manager.create(
            self.base_commit, "athena/plan/rehydrated-handle"
        )

        self.assertEqual(commit, recovered.base_commit)

    async def test_commit_reconciles_installed_review_after_lost_update_response(
        self,
    ) -> None:
        workspace = await self._create("athena/plan/uncertain-update")
        path = Path(workspace.path)
        (path / "model.py").write_text("reviewed\n", encoding="utf-8")
        approved = await self.manager.diff(workspace)
        original_git = self.manager._git
        lost_response = False

        async def uncertain_git(
            *args: str, cwd: Path | None = None, check: bool = True
        ) -> bytes:
            nonlocal lost_response
            result = await original_git(*args, cwd=cwd, check=check)
            if (
                args[:2] == ("update-ref", "refs/heads/athena/plan/uncertain-update")
                and not lost_response
            ):
                lost_response = True
                raise GitWorkspaceError("lost update-ref response")
            return result

        self.manager._git = uncertain_git  # type: ignore[method-assign]
        with self.assertRaisesRegex(GitWorkspaceError, "lost update-ref response"):
            await self.manager.commit(workspace, approved, "score 0.80")
        self.manager._git = original_git  # type: ignore[method-assign]

        installed = self._git("rev-parse", "HEAD", cwd=path).stdout.strip()
        self.assertEqual(
            installed,
            await self.manager.commit(workspace, approved, "same reviewed commit"),
        )

        async def write_recovered_diff(content: bytes) -> str:
            digest = hashlib.sha256(content).hexdigest()
            ref = f"artifact://git-diff/{digest}"
            self.artifacts[ref] = content
            return ref

        fresh_manager = LocalGitWorkspace(
            self.repo, self.worktree_root, write_recovered_diff
        )
        recovered = await fresh_manager.create(
            self.base_commit, "athena/plan/uncertain-update"
        )
        self.assertEqual(
            installed,
            await fresh_manager.commit(recovered, approved, "fresh recovery retry"),
        )

    async def test_branch_delete_failure_can_be_retried(self) -> None:
        workspace = await self._create("experiment/retry-remove")
        path = Path(workspace.path)
        original_git = self.manager._git
        failed_once = False

        async def flaky_git(*args: str, cwd: Path | None = None) -> bytes:
            nonlocal failed_once
            if args[:2] == ("branch", "-D") and not failed_once:
                failed_once = True
                raise GitWorkspaceError("injected branch deletion failure")
            return await original_git(*args, cwd=cwd)

        self.manager._git = flaky_git  # type: ignore[method-assign]
        with self.assertRaises(GitWorkspaceError):
            await self.manager.remove(workspace, delete_branch=True)
        self.assertFalse(path.exists())

        self.manager._git = original_git  # type: ignore[method-assign]
        await self.manager.remove(workspace, delete_branch=True)


@pytest.mark.asyncio
async def test_diff_reports_paths_including_deletions(tmp_path) -> None:
    from athena.core.git_workspace import LocalGitWorkspace

    repo = tmp_path / "repo"
    workspace = LocalGitWorkspace(
        repo, tmp_path / "wt", lambda b: f"artifact://d/{len(b)}"
    )
    base = await workspace.init(repo_path=repo)

    branch = await workspace.create(base, "exp/t4")
    p = Path(branch.path)
    (p / "run_experiment.py").write_text("print(1)", encoding="utf-8")
    (p / "old.py").write_text("x", encoding="utf-8")
    diff = await workspace.diff(branch)
    assert "run_experiment.py" in diff.paths
    assert "old.py" in diff.paths
    assert diff.ref.startswith("artifact://")

    (p / "old.py").unlink()
    diff2 = await workspace.diff(branch)
    assert "old.py" in diff2.paths


@pytest.mark.asyncio
async def test_init_does_not_walk_up_to_parent_repo(tmp_path: Path) -> None:
    """项目 .athena/repo 位于外层源码仓工作树内时，init 不得误用外层仓。

    回归：空目录里的 `git rev-parse HEAD` 会向上走到父仓、误判成"已初始化"，
    导致后续 git 操作打在父仓上（branch 冲突 reference already exists）。
    """
    parent = tmp_path / "parent"
    parent.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(parent)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(parent), "config", "user.name", "Athena Test"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(parent), "config", "user.email", "athena@example.invalid"],
        check=True,
        capture_output=True,
    )
    (parent / "seed.txt").write_text("seed", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(parent), "add", "seed.txt"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(parent), "commit", "--no-gpg-sign", "-m", "seed"],
        check=True,
        capture_output=True,
    )
    parent_head = subprocess.run(
        ["git", "-C", str(parent), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    child = parent / "child"
    child.mkdir()
    manager = LocalGitWorkspace(
        child / "repo", child / "workspaces", lambda b: f"artifact://d/{len(b)}"
    )

    commit = await manager.init()

    assert commit != parent_head  # 不是外层仓的 HEAD
    assert (child / "repo" / ".git").is_dir()  # 真正初始化了子仓


if __name__ == "__main__":
    unittest.main()
