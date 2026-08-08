"""真实临时仓库上的 LocalGitWorkspace 单元测试。"""

import hashlib
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

    async def test_diff_ignores_runtime_outputs_excluded_by_gitignore(self) -> None:
        workspace = await self._create("experiment/ignored-runtime")
        path = Path(workspace.path)
        (path / ".gitignore").write_text("runtime.log\n", encoding="utf-8")
        (path / "run_experiment.py").write_text("print('ok')\n", encoding="utf-8")
        (path / "runtime.log").write_text("observed output\n", encoding="utf-8")

        diff = await self.manager.diff(workspace)

        self.assertIn(b"run_experiment.py", self.artifacts[diff.ref])
        self.assertNotIn(b"diff --git a/runtime.log", self.artifacts[diff.ref])

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

        workspace = await self._create("experiment/no-review")
        with self.assertRaises(GitWorkspaceError):
            await self.manager.commit(
                workspace,
                GitDiff(ref="artifact://diff/missing", paths=()),
                "not reviewed",
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


if __name__ == "__main__":
    unittest.main()
