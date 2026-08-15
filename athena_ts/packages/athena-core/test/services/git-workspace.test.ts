/** 真实临时仓库上的 LocalGitWorkspace 单元测试。 */

import { execFileSync, spawnSync } from "node:child_process"
import { createHash } from "node:crypto"
import {
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs"
import { tmpdir } from "node:os"
import path, { join, resolve } from "node:path"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { GitWorkspaceError, type GitDiff, type GitWorkBranch } from "../../src/services/workspace.js"
import { LocalGitWorkspace } from "../../src/services/git-workspace.js"

/** 等价 pathlib.Path.is_relative_to。 */
function isUnder(p: string, root: string): boolean {
  const r = resolve(root)
  const c = resolve(p)
  return c === r || c.startsWith(r + path.sep)
}

describe("LocalGitWorkspace", () => {
  let tempRoot: string
  let repo: string
  let worktreeRoot: string
  let baseCommit: string
  let artifacts: Record<string, Buffer>
  let manager: LocalGitWorkspace
  let created: GitWorkBranch[]

  function git(cwd: string, ...args: string[]): string {
    return execFileSync("git", ["-C", cwd, ...args], { encoding: "utf-8" }).trim()
  }

  /** 保留原始输出（含末尾换行），等价 Python 测试里 ``.stdout``。 */
  function gitRaw(cwd: string, ...args: string[]): string {
    return execFileSync("git", ["-C", cwd, ...args], { encoding: "utf-8" })
  }

  function writeDiff(content: Buffer): string {
    const digest = createHash("sha256").update(content).digest("hex")
    const ref = `artifact://git-diff/${digest}`
    artifacts[ref] = content
    return ref
  }

  async function createWorkspace(branch: string): Promise<GitWorkBranch> {
    const workspace = await manager.create(baseCommit, branch)
    created.push(workspace)
    return workspace
  }

  beforeEach(() => {
    tempRoot = mkdtempSync(join(tmpdir(), "athena-gw-"))
    repo = join(tempRoot, "repo")
    worktreeRoot = join(tempRoot, "worktrees")
    mkdirSync(repo, { recursive: true })

    git(repo, "init")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "config", "user.name", "Athena Test")
    git(repo, "config", "user.email", "athena@example.invalid")
    writeFileSync(join(repo, "seed.txt"), "baseline\n", "utf-8")
    git(repo, "add", "seed.txt")
    git(repo, "commit", "--no-gpg-sign", "-m", "baseline")
    baseCommit = git(repo, "rev-parse", "HEAD")
    git(repo, "branch", "already-exists", baseCommit)

    artifacts = {}
    manager = new LocalGitWorkspace(repo, worktreeRoot, writeDiff)
    created = []
  })

  afterEach(async () => {
    vi.restoreAllMocks()
    // 失败断言也不应把注册中的 worktree 留给 temp 清理。
    for (const workspace of [...created].reverse()) {
      try {
        await manager.remove(workspace, { deleteBranch: true, force: true })
      } catch (err) {
        if (!(err instanceof GitWorkspaceError)) throw err
      }
    }
    rmSync(tempRoot, { recursive: true, force: true })
  })

  it("test_diff_rejects_post_review_change_then_commits_new_review", async () => {
    const workspace = await createWorkspace("experiment/reviewed")
    const p = workspace.path
    const target = join(p, "new.txt")
    writeFileSync(target, "first version\n", "utf-8")
    const weights = Buffer.alloc(512)
    for (let i = 0; i < 512; i++) weights[i] = i % 256
    writeFileSync(join(p, "weights.bin"), weights)

    const firstDiff = await manager.diff(workspace)
    expect(artifacts[firstDiff.ref]!.includes(Buffer.from("diff --git a/new.txt b/new.txt"))).toBe(true)
    expect(artifacts[firstDiff.ref]!.includes(Buffer.from("first version"))).toBe(true)
    expect(artifacts[firstDiff.ref]!.includes(Buffer.from("GIT binary patch"))).toBe(true)

    // diff 之后的任何 tracked 变化都必须重新进入审查流程。
    writeFileSync(target, "approved version\n", "utf-8")
    await expect(manager.commit(workspace, firstDiff, "must not commit stale review")).rejects.toThrow(
      GitWorkspaceError,
    )

    const secondDiff = await manager.diff(workspace)
    expect(firstDiff.ref).not.toBe(secondDiff.ref)
    await expect(manager.commit(workspace, firstDiff, "must bind the approved reference")).rejects.toThrow(
      GitWorkspaceError,
    )
    const surprise = join(p, "not-reviewed.txt")
    writeFileSync(surprise, "not reviewed\n", "utf-8")
    await expect(manager.commit(workspace, secondDiff, "must not commit untracked file")).rejects.toThrow(
      GitWorkspaceError,
    )
    rmSync(surprise)
    const commit = await manager.commit(workspace, secondDiff, "checkpoint approved diff")
    const saved = gitRaw(p, "show", `${commit}:new.txt`)
    expect(saved).toBe("approved version\n")
    expect(git(p, "status", "--porcelain")).toBe("")
    expect(commit).toBe(
      await manager.commit(workspace, secondDiff, "idempotent retry after uncertain response"),
    )

    await manager.remove(workspace, { deleteBranch: true })
    expect(existsSync(p)).toBe(false)
    const branch = spawnSync("git", [
      "-C",
      repo,
      "show-ref",
      "--verify",
      "--quiet",
      "refs/heads/experiment/reviewed",
    ])
    expect(branch.status).toBe(1)
  })

  it("test_empty_diff_reuses_head_without_creating_commit", async () => {
    const workspace = await createWorkspace("experiment/config-only")

    const diff = await manager.diff(workspace)
    expect(artifacts[diff.ref]!.length).toBe(0)
    expect(await manager.commit(workspace, diff, "configuration-only experiment")).toBe(baseCommit)
    expect(git(workspace.path, "rev-parse", "HEAD")).toBe(baseCommit)
    await manager.remove(workspace, { deleteBranch: true })
  })

  it("test_workspace_supports_multiple_reviewed_commits", async () => {
    const workspace = await createWorkspace("athena/plan/h1")
    const p = workspace.path
    const model = join(p, "model.py")

    writeFileSync(model, "v1\n", "utf-8")
    const firstDiff = await manager.diff(workspace)
    const firstCommit = await manager.commit(workspace, firstDiff, "score 0.80")

    writeFileSync(model, "v2\n", "utf-8")
    const secondDiff = await manager.diff(workspace)
    const secondCommit = await manager.commit(workspace, secondDiff, "score 0.82")

    expect(firstCommit).not.toBe(secondCommit)
    expect(secondCommit).toBe(git(p, "rev-parse", "HEAD"))
    expect(gitRaw(p, "show", "HEAD:model.py")).toBe("v2\n")
  })

  it("test_create_recovers_stable_branch_without_losing_commits", async () => {
    const workspace = await createWorkspace("athena/plan/recover")
    const p = workspace.path
    const model = join(p, "model.py")
    writeFileSync(model, "v1\n", "utf-8")
    const reviewed = await manager.diff(workspace)
    const firstCommit = await manager.commit(workspace, reviewed, "score 0.80")

    const writeRecoveredDiff = (content: Buffer): string => {
      const digest = createHash("sha256").update(content).digest("hex")
      const ref = `artifact://git-diff/${digest}`
      artifacts[ref] = content
      return ref
    }

    const recoveredManager = new LocalGitWorkspace(repo, worktreeRoot, writeRecoveredDiff)
    const recovered = await recoveredManager.create(baseCommit, "athena/plan/recover")

    expect(workspace.path).toBe(recovered.path)
    expect(firstCommit).toBe(recovered.base_commit)
    expect(gitRaw(p, "show", "HEAD:model.py")).toBe("v1\n")

    writeFileSync(model, "v2\n", "utf-8")
    const secondDiff = await recoveredManager.diff(recovered)
    const secondCommit = await recoveredManager.commit(recovered, secondDiff, "score 0.82")
    expect(firstCommit).not.toBe(secondCommit)
    expect(secondCommit).toBe(git(p, "rev-parse", "HEAD"))
  })

  it("test_diff_ignores_runtime_outputs_excluded_by_gitignore", async () => {
    const workspace = await createWorkspace("experiment/ignored-runtime")
    const p = workspace.path
    writeFileSync(join(p, ".gitignore"), "runtime.log\n", "utf-8")
    writeFileSync(join(p, "run_experiment.py"), "print('ok')\n", "utf-8")
    writeFileSync(join(p, "runtime.log"), "observed output\n", "utf-8")

    const diff = await manager.diff(workspace)

    expect(artifacts[diff.ref]!.includes(Buffer.from("run_experiment.py"))).toBe(true)
    expect(artifacts[diff.ref]!.includes(Buffer.from("diff --git a/runtime.log"))).toBe(false)
  })

  it("test_restore_paths_restores_reviewed_output_and_preserves_source_change", async () => {
    const workspace = await createWorkspace("experiment/restore-reviewed-output")
    const root = workspace.path
    const source = join(root, "solution.py")
    const output = join(root, "predictions.csv")
    writeFileSync(source, "VERSION = 1\n", "utf-8")
    writeFileSync(output, "id,prediction\n1,0\n", "utf-8")
    const reviewed = await manager.diff(workspace)

    writeFileSync(source, "VERSION = 2\n", "utf-8")
    writeFileSync(output, "id,prediction\n1,1\n", "utf-8")
    await manager.restorePaths(workspace, ["predictions.csv"])

    expect(readFileSync(output, "utf-8")).toBe("id,prediction\n1,0\n")
    expect(readFileSync(source, "utf-8")).toBe("VERSION = 2\n")
    expect(await manager.diff(workspace)).not.toEqual(reviewed)
  })

  it("test_restore_paths_removes_execution_only_output", async () => {
    const workspace = await createWorkspace("experiment/remove-generated-output")
    const root = workspace.path
    const source = join(root, "solution.py")
    const output = join(root, "predictions.csv")
    writeFileSync(source, "VERSION = 1\n", "utf-8")
    const reviewed = await manager.diff(workspace)

    writeFileSync(source, "VERSION = 2\n", "utf-8")
    writeFileSync(output, "id,prediction\n1,1\n", "utf-8")
    await manager.restorePaths(workspace, ["predictions.csv"])

    expect(existsSync(output)).toBe(false)
    expect(readFileSync(source, "utf-8")).toBe("VERSION = 2\n")
    expect(await manager.diff(workspace)).not.toEqual(reviewed)
  })

  it("test_restore_paths_rejects_paths_outside_workspace", async () => {
    const workspace = await createWorkspace("experiment/reject-output-escape")
    const outside = join(tempRoot, "outside.csv")
    writeFileSync(outside, "keep\n", "utf-8")
    await manager.diff(workspace)

    for (const invalid of [outside, "../outside.csv", "nested/../outside.csv"]) {
      await expect(manager.restorePaths(workspace, [invalid])).rejects.toThrow(GitWorkspaceError)
    }

    expect(readFileSync(outside, "utf-8")).toBe("keep\n")
  })

  it("test_restore_paths_requires_reviewed_tree", async () => {
    const workspace = await createWorkspace("experiment/restore-before-review")

    await expect(manager.restorePaths(workspace, ["predictions.csv"])).rejects.toThrow(
      GitWorkspaceError,
    )
  })

  it("test_dirty_remove_requires_explicit_force", async () => {
    const workspace = await createWorkspace("experiment/discard")
    const p = workspace.path
    writeFileSync(join(p, "dirty.txt"), "discard me\n", "utf-8")

    await expect(manager.remove(workspace)).rejects.toThrow(GitWorkspaceError)
    expect(existsSync(p)).toBe(true)

    await manager.remove(workspace, { deleteBranch: true, force: true })
    expect(existsSync(p)).toBe(false)
  })

  it("test_rejects_invalid_or_unowned_inputs", async () => {
    await expect(manager.create("not-a-hash", "experiment/invalid-commit")).rejects.toThrow(
      GitWorkspaceError,
    )
    await expect(manager.create("f".repeat(40), "experiment/unknown-commit")).rejects.toThrow(
      GitWorkspaceError,
    )
    await expect(manager.create(baseCommit, "../invalid")).rejects.toThrow(GitWorkspaceError)
    await expect(manager.create(baseCommit, "already-exists")).rejects.toThrow(GitWorkspaceError)
  })

  it("test_create_is_idempotent", async () => {
    const first = await manager.create(baseCommit, "experiment/e1")
    created.push(first)
    const second = await manager.create(baseCommit, "experiment/e1")
    expect(first.path).toBe(second.path)
    expect(first.branch).toBe(second.branch)

    const forged: GitWorkBranch = {
      path: join(tempRoot, "outside"),
      branch: "experiment/forged",
      base_commit: baseCommit,
    }
    await expect(manager.diff(forged)).rejects.toThrow(GitWorkspaceError)

    const forgedInside: GitWorkBranch = { ...forged, path: join(worktreeRoot, "never-created") }
    await expect(manager.diff(forgedInside)).rejects.toThrow(GitWorkspaceError)

    const forgedRegistered: GitWorkBranch = { ...first, branch: "experiment/different-identity" }
    await expect(manager.diff(forgedRegistered)).rejects.toThrow(GitWorkspaceError)

    const workspace = await createWorkspace("experiment/no-review")
    await expect(
      manager.commit(workspace, { ref: "artifact://diff/missing", paths: [] } satisfies GitDiff, "not reviewed"),
    ).rejects.toThrow(GitWorkspaceError)
  })

  it("test_create_rebuilds_stale_worktree_after_project_copy", async () => {
    await createWorkspace("athena/prepare")

    const copyRoot = join(tempRoot, "copy")
    const copiedRepo = join(copyRoot, "repo")
    const copiedWorktrees = join(copyRoot, "worktrees")
    mkdirSync(copyRoot, { recursive: true })
    cpSync(repo, copiedRepo, { recursive: true })

    const writeDiffCopy = (content: Buffer): string => {
      const digest = createHash("sha256").update(content).digest("hex")
      const ref = `artifact://git-diff-copy/${digest}`
      artifacts[ref] = content
      return ref
    }

    const copied = new LocalGitWorkspace(copiedRepo, copiedWorktrees, writeDiffCopy)
    const fresh = await copied.create(baseCommit, "athena/prepare")
    expect(isUnder(fresh.path, copiedWorktrees)).toBe(true)
    expect(existsSync(fresh.path)).toBe(true)
  })

  it("test_init_creates_nested_repo_instead_of_walking_up", async () => {
    const nested = join(repo, "nested-project", "repo")
    mkdirSync(nested, { recursive: true })

    const writeDiffNested = (content: Buffer): string => {
      const digest = createHash("sha256").update(content).digest("hex")
      const ref = `artifact://git-diff-nested/${digest}`
      artifacts[ref] = content
      return ref
    }

    const mgr = new LocalGitWorkspace(
      nested,
      join(repo, "nested-project", "worktrees"),
      writeDiffNested,
    )
    const commit = await mgr.init()
    // 就地建 .git，而不是沿用父仓 self.repo 的 HEAD
    expect(existsSync(join(nested, ".git"))).toBe(true)
    expect(commit).toBe(git(nested, "rev-parse", "HEAD"))
    const workspace = await mgr.create(commit, "athena/prepare")
    expect(existsSync(workspace.path)).toBe(true)
    await mgr.remove(workspace, { deleteBranch: true, force: true })
  })

  it("test_create_preserves_active_review", async () => {
    const workspace = await createWorkspace("athena/plan/preserve-review")
    const p = workspace.path
    writeFileSync(join(p, "model.py"), "reviewed\n", "utf-8")
    const approved = await manager.diff(workspace)

    const recovered = await manager.create(baseCommit, "athena/plan/preserve-review")
    const commit = await manager.commit(recovered, approved, "score 0.80")

    expect(commit).toBe(git(p, "rev-parse", "HEAD"))
  })

  it("test_create_preserves_accepted_commit_retry", async () => {
    const workspace = await createWorkspace("athena/plan/preserve-retry")
    const p = workspace.path
    writeFileSync(join(p, "model.py"), "accepted\n", "utf-8")
    const approved = await manager.diff(workspace)
    const commit = await manager.commit(workspace, approved, "score 0.80")

    const recovered = await manager.create(baseCommit, "athena/plan/preserve-retry")

    expect(commit).toBe(
      await manager.commit(recovered, approved, "uncertain response retry"),
    )
  })

  it("test_create_reports_commit_made_through_rehydrated_handle", async () => {
    const workspace = await createWorkspace("athena/plan/rehydrated-handle")
    const p = workspace.path
    writeFileSync(join(p, "model.py"), "accepted\n", "utf-8")
    const approved = await manager.diff(workspace)
    const rehydrated: GitWorkBranch = { ...workspace }

    const commit = await manager.commit(rehydrated, approved, "score 0.80")
    const recovered = await manager.create(baseCommit, "athena/plan/rehydrated-handle")

    expect(commit).toBe(recovered.base_commit)
  })

  it("test_commit_reconciles_installed_review_after_lost_update_response", async () => {
    const workspace = await createWorkspace("athena/plan/uncertain-update")
    const p = workspace.path
    writeFileSync(join(p, "model.py"), "reviewed\n", "utf-8")
    const approved = await manager.diff(workspace)

    const originalGit = (manager as any)._git.bind(manager)
    let lostResponse = false
    const spy = vi.spyOn(manager as any, "_git")
    spy.mockImplementation(async (...callArgs: unknown[]) => {
      const args = callArgs[0] as string[]
      const opts = callArgs[1] as { cwd?: string; check?: boolean } | undefined
      const result = await originalGit(args, opts)
      if (
        args[0] === "update-ref" &&
        args[1] === "refs/heads/athena/plan/uncertain-update" &&
        !lostResponse
      ) {
        lostResponse = true
        throw new GitWorkspaceError("lost update-ref response")
      }
      return result
    })

    await expect(manager.commit(workspace, approved, "score 0.80")).rejects.toThrow(
      /lost update-ref response/,
    )
    spy.mockRestore()

    const installed = git(p, "rev-parse", "HEAD")
    expect(await manager.commit(workspace, approved, "same reviewed commit")).toBe(installed)

    const writeRecoveredDiff = (content: Buffer): string => {
      const digest = createHash("sha256").update(content).digest("hex")
      const ref = `artifact://git-diff/${digest}`
      artifacts[ref] = content
      return ref
    }

    const freshManager = new LocalGitWorkspace(repo, worktreeRoot, writeRecoveredDiff)
    const recovered = await freshManager.create(baseCommit, "athena/plan/uncertain-update")
    expect(await freshManager.commit(recovered, approved, "fresh recovery retry")).toBe(installed)
  })

  it("test_branch_delete_failure_can_be_retried", async () => {
    const workspace = await createWorkspace("experiment/retry-remove")
    const p = workspace.path

    const originalGit = (manager as any)._git.bind(manager)
    let failedOnce = false
    const spy = vi.spyOn(manager as any, "_git")
    spy.mockImplementation(async (...callArgs: unknown[]) => {
      const args = callArgs[0] as string[]
      const opts = callArgs[1] as { cwd?: string; check?: boolean } | undefined
      if (args[0] === "branch" && args[1] === "-D" && !failedOnce) {
        failedOnce = true
        throw new GitWorkspaceError("injected branch deletion failure")
      }
      return await originalGit(args, opts)
    })

    await expect(manager.remove(workspace, { deleteBranch: true })).rejects.toThrow(
      GitWorkspaceError,
    )
    expect(existsSync(p)).toBe(false)

    spy.mockRestore()
    await manager.remove(workspace, { deleteBranch: true })
  })
})

it("test_diff_reports_paths_including_deletions", async () => {
  const tmp = mkdtempSync(join(tmpdir(), "athena-gw-del-"))
  const repoDir = join(tmp, "repo")
  const workspace = new LocalGitWorkspace(
    repoDir,
    join(tmp, "wt"),
    (b) => `artifact://d/${b.length}`,
  )
  const base = await workspace.init()
  const branch = await workspace.create(base, "exp/t4")
  const p = branch.path
  writeFileSync(join(p, "run_experiment.py"), "print(1)", "utf-8")
  writeFileSync(join(p, "old.py"), "x", "utf-8")
  const diff = await workspace.diff(branch)
  expect(diff.paths).toContain("run_experiment.py")
  expect(diff.paths).toContain("old.py")
  expect(diff.ref.startsWith("artifact://")).toBe(true)

  rmSync(join(p, "old.py"))
  const diff2 = await workspace.diff(branch)
  expect(diff2.paths).toContain("old.py")
  rmSync(tmp, { recursive: true, force: true })
})

it("test_init_does_not_walk_up_to_parent_repo", async () => {
  const tmp = mkdtempSync(join(tmpdir(), "athena-gw-parent-"))
  const parent = join(tmp, "parent")
  mkdirSync(parent, { recursive: true })
  execFileSync("git", ["init", "-b", "main", parent])
  execFileSync("git", ["-C", parent, "config", "user.name", "Athena Test"])
  execFileSync("git", ["-C", parent, "config", "user.email", "athena@example.invalid"])
  writeFileSync(join(parent, "seed.txt"), "seed", "utf-8")
  execFileSync("git", ["-C", parent, "add", "seed.txt"])
  execFileSync("git", ["-C", parent, "commit", "--no-gpg-sign", "-m", "seed"])
  const parentHead = execFileSync("git", ["-C", parent, "rev-parse", "HEAD"], {
    encoding: "utf-8",
  }).trim()

  const child = join(parent, "child")
  mkdirSync(child, { recursive: true })
  const manager = new LocalGitWorkspace(
    join(child, "repo"),
    join(child, "workspaces"),
    (b) => `artifact://d/${b.length}`,
  )

  const commit = await manager.init()

  expect(commit).not.toBe(parentHead)
  expect(existsSync(join(child, "repo", ".git"))).toBe(true)
  rmSync(tmp, { recursive: true, force: true })
})
