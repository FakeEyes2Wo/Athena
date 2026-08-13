/** Local Git implementation of the Git workspace contract. */

import { execFile } from "node:child_process"
import { createHash, randomUUID } from "node:crypto"
import fs from "node:fs"
import path from "node:path"
import { promisify } from "node:util"
import type { ArtifactRef, CommitHash } from "../models/contracts.js"
import { GitWorkspaceError } from "./workspace.js"
import type { BinaryDiffWriter, GitDiff, GitWorkBranch, GitWorkspace } from "./workspace.js"

const execFileP = promisify(execFile)

/** pathlib.Path.is_relative_to 等价：c 等于 root 或在 root 子树内。 */
function isRelativeTo(p: string, root: string): boolean {
  const r = path.resolve(root)
  const c = path.resolve(p)
  return c === r || c.startsWith(r + path.sep)
}

/** 最小异步互斥锁（promise 链），等价 Python asyncio.Lock。 */
class Mutex {
  private tail: Promise<unknown> = Promise.resolve()
  async run<T>(fn: () => Promise<T>): Promise<T> {
    const prev = this.tail
    let release!: () => void
    this.tail = new Promise((resolve) => {
      release = () => resolve(undefined)
    })
    await prev
    try {
      return await fn()
    } finally {
      release()
    }
  }
}

interface _Review {
  artifact: ArtifactRef
  sha256: string
  tree: string
  head: CommitHash
  empty: boolean
  pendingCommit?: CommitHash | undefined
}

interface _Committed {
  artifact: ArtifactRef
  commit: CommitHash
}

interface _WorkspaceState {
  workspace: GitWorkBranch
  review?: _Review | undefined
  committed?: _Committed | undefined
}

/** Manage Git worktrees on the local filesystem. */
export class LocalGitWorkspace implements GitWorkspace {
  private _repo: string
  private _root: string
  private _diffWriter: BinaryDiffWriter
  private _lock: Mutex
  private _states: Map<string, _WorkspaceState>
  private _repoInitialized: boolean

  constructor(repoPath: string, worktreeRoot: string, diffWriter: BinaryDiffWriter) {
    this._repo = path.resolve(repoPath)
    this._root = path.resolve(worktreeRoot)
    fs.mkdirSync(this._root, { recursive: true })
    this._diffWriter = diffWriter
    this._lock = new Mutex()
    this._states = new Map()
    this._repoInitialized = false
  }

  async init(
    repoPath?: string,
    initialFile = "README.md",
    initialContent = "# Experiment Base",
  ): Promise<CommitHash> {
    const p = path.resolve(repoPath ?? this._repo)
    if (this._repoInitialized && p === this._repo) {
      const output = await this._git(["rev-parse", "HEAD"], { cwd: p })
      return output.toString().trim()
    }

    fs.mkdirSync(p, { recursive: true })
    // 断点续传：仓库已初始化时直接返回，不重跑 init/commit（避免第二次打开
    // 同一项目时 `git commit -m "initial commit"` 因 "nothing to commit" 失败）。
    // 注意不能用 `git rev-parse HEAD` 探测是否已初始化——项目 .athena/repo 位于
    // Athena 源码仓的工作树内，空目录会让 rev-parse 向上走到源码仓、误判成
    // "已初始化"，导致所有项目的 git 操作都打在源码仓上（branch 冲突：
    // reference already exists）。必须确认本目录自己就是 git 仓库。
    if (!fs.existsSync(path.join(p, ".git"))) {
      await this._git(["init", "-b", "main"], { cwd: p })

      const initFile = path.join(p, initialFile)
      fs.writeFileSync(initFile, initialContent)
      await this._git(["add", "-A"], { cwd: p })
      await this._git(["commit", "-m", "initial commit"], { cwd: p })
    }

    const output = await this._git(["rev-parse", "HEAD"], { cwd: p })
    const commitHash = output.toString().trim()
    if (this._repo !== p) {
      this._repo = p
    }
    this._repoInitialized = true
    return commitHash
  }

  async create(
    baseCommit: CommitHash,
    branch: string,
    opts?: { name?: string },
  ): Promise<GitWorkBranch> {
    return this._lock.run(async () => {
      const commit = await this._resolveCommit(baseCommit)
      this._validateBranch(branch)

      const existing = await this._git(["show-ref", "--verify", `refs/heads/${branch}`], {
        check: false,
      })
      if (existing.toString().trim()) {
        // 幂等恢复：分支已存在 → 返回匹配的现有 worktree（design §idempotent）
        const workspace = await this._findWorktree(branch)
        if (workspace !== null) {
          if (isRelativeTo(workspace.path, this._root)) {
            return workspace
          }
          // 项目被复制/迁移后，worktree 注册仍指向旧项目路径（如
          // hell），直接复用会因路径越界在 runtime 的 relative_to 处
          // 崩溃。git worktree remove/prune 都无法清除跨仓库的陈旧
          // 注册，需直接删注册目录（不删旧项目目录），并删 ref 绕过
          // 「分支已签出」保护，随后走下方重建 fresh worktree。
          const registration = path.join(
            this._repo,
            ".git",
            "worktrees",
            path.basename(workspace.path),
          )
          fs.rmSync(registration, { recursive: true, force: true })
          await this._git(["update-ref", "-d", `refs/heads/${branch}`], { check: false })
        } else {
          throw new GitWorkspaceError(`分支已存在但无 worktree：${branch}`)
        }
      }

      let dirname: string
      if (opts?.name === undefined) {
        dirname = `athena-${randomUUID().replace(/-/g, "")}`
      } else if (
        !opts.name ||
        opts.name === "." ||
        opts.name === ".." ||
        opts.name.includes("/") ||
        opts.name.includes("\\")
      ) {
        throw new GitWorkspaceError("非法 worktree 目录名")
      } else {
        dirname = opts.name
      }
      const p = path.join(this._root, dirname)
      const branchRef = `refs/heads/${branch}`
      try {
        await this._git(["update-ref", branchRef, commit, "0".repeat(40)])
      } catch (exc) {
        // 并发进程在 show-ref 与本行之间创建了同名分支（per-instance 锁无法
        // 跨进程互斥）→ 幂等恢复现有 worktree，而不是裸抛 reference already exists。
        if (!String(exc).includes("already exists")) {
          throw exc
        }
        const workspace = await this._findWorktree(branch)
        if (workspace !== null && isRelativeTo(workspace.path, this._root)) {
          return workspace
        }
        throw exc
      }
      try {
        await this._git(["worktree", "add", p, branch])
      } catch {
        // worktree add 失败 → 回滚已创建的分支引用后抛出领域错误
        await this._git(["branch", "-D", branch], { check: false })
        throw new GitWorkspaceError("Branch 创建失败")
      }

      const workspace: GitWorkBranch = { path: p, branch, base_commit: commit }
      this._states.set(path.resolve(p), { workspace })
      return workspace
    })
  }

  async _findWorktree(branch: string): Promise<GitWorkBranch | null> {
    const listing = await this._git(["worktree", "list", "--porcelain"], { cwd: this._repo })
    for (const block of listing.toString("utf-8").split(/\r?\n\r?\n/)) {
      let p: string | null = null
      let br: string | null = null
      for (const line of block.split(/\r?\n/)) {
        if (line.startsWith("worktree ")) {
          p = line.slice("worktree ".length)
        } else if (line.startsWith("branch ")) {
          br = line.slice("branch ".length).replace(/^refs\/heads\//, "")
        }
      }
      if (br === branch && p) {
        const worktreePath = path.resolve(p)
        const registered = this._states.get(worktreePath)
        if (registered !== undefined) {
          const workspace = registered.workspace
          if (workspace.branch !== branch) {
            throw new GitWorkspaceError("worktree 分支与已注册状态不匹配")
          }
          return workspace
        }
        const workspace: GitWorkBranch = {
          path: worktreePath,
          branch,
          base_commit: await this._resolveCommit(branch),
        }
        this._states.set(worktreePath, { workspace })
        return workspace
      }
    }
    return null
  }

  async diff(workspace: GitWorkBranch): Promise<GitDiff> {
    return this._lock.run(async () => {
      const state = this._getState(workspace)
      const p = workspace.path
      const currentHead = (await this._git(["rev-parse", "HEAD"], { cwd: p }))
        .toString()
        .trim()

      await this._git(["add", "-A"], { cwd: p })
      const staged = await this._git(["diff", "--cached", "--binary", currentHead], { cwd: p })
      if (
        (await this._git(["diff"], { cwd: p })).length ||
        (await this._git(["ls-files", "--others", "--exclude-standard"], { cwd: p })).length
      ) {
        throw new GitWorkspaceError("工作区仍有未暂存变更")
      }

      const names = await this._git(["diff", "--cached", "--name-only", "-z", currentHead], {
        cwd: p,
      })
      const changed = new Set(
        names
          .toString("utf-8")
          .split("\0")
          .filter((n) => n !== ""),
      )
      // 相对 base 的净 diff 会吞掉「先暂存、后删除且从未提交」的文件；
      // 用上一轮暂存树再 diff 一次，还原这类删除路径，保证 paths 不漏掉删除。
      const previousTree = state.review ? state.review.tree : currentHead
      const previousNames = await this._git(
        ["diff", "--cached", "--name-only", "-z", previousTree],
        { cwd: p },
      )
      for (const n of previousNames.toString("utf-8").split("\0")) {
        if (n !== "") changed.add(n)
      }
      const paths = [...changed].sort()

      const result = this._diffWriter(staged)
      const artifact = result instanceof Promise ? await result : result
      state.review = {
        artifact,
        sha256: createHash("sha256").update(staged).digest("hex"),
        tree: (await this._git(["write-tree"], { cwd: p })).toString().trim(),
        head: currentHead,
        empty: staged.length === 0,
      }
      return { ref: artifact, paths }
    })
  }

  async commit(workspace: GitWorkBranch, approvedDiff: GitDiff, message: string): Promise<CommitHash> {
    return this._lock.run(async () => {
      const state = this._getState(workspace)
      const review = state.review
      if (!review) {
        const committed = state.committed
        if (committed && committed.artifact === approvedDiff.ref) {
          LocalGitWorkspace._recordCommit(state, workspace, approvedDiff.ref, committed.commit)
          return committed.commit
        }
        const reconciled = await this._reconcileInstalledReview(workspace, approvedDiff)
        if (reconciled !== null) {
          LocalGitWorkspace._recordCommit(state, workspace, approvedDiff.ref, reconciled)
          return reconciled
        }
        throw new GitWorkspaceError("批准的 artifact 与当前 diff 不匹配")
      }
      if (review.artifact !== approvedDiff.ref) {
        throw new GitWorkspaceError("批准的 artifact 与当前 diff 不匹配")
      }

      const p = workspace.path
      const currentHead = (await this._git(["rev-parse", "HEAD"], { cwd: p }))
        .toString()
        .trim()
      const pendingCommit = review.pendingCommit
      if (pendingCommit && currentHead === pendingCommit) {
        const marker = await this._resolveReviewMarker(workspace.branch)
        if (marker !== pendingCommit) {
          throw new GitWorkspaceError("已安装的提交缺少匹配的审查标记")
        }
        LocalGitWorkspace._recordCommit(state, workspace, approvedDiff.ref, pendingCommit)
        return pendingCommit
      }
      const currentTree = (await this._git(["write-tree"], { cwd: p })).toString().trim()
      const staged = await this._git(["diff", "--cached", "--binary", review.head], { cwd: p })
      const unstaged = await this._git(["diff"], { cwd: p })
      const untracked = await this._git(["ls-files", "--others", "--exclude-standard"], { cwd: p })
      if (
        createHash("sha256").update(staged).digest("hex") !== review.sha256 ||
        currentHead !== review.head ||
        currentTree !== review.tree ||
        unstaged.length ||
        untracked.length
      ) {
        throw new GitWorkspaceError("工作区在审查后被修改")
      }

      if (review.empty) {
        LocalGitWorkspace._recordCommit(state, workspace, approvedDiff.ref, currentHead)
        return currentHead
      }

      let newCommit = review.pendingCommit
      if (newCommit === undefined) {
        newCommit = (
          await this._git(["commit-tree", currentTree, "-p", currentHead, "-m", message], {
            cwd: p,
          })
        )
          .toString()
          .trim()
        review.pendingCommit = newCommit
      }
      await this._git(["update-ref", LocalGitWorkspace._reviewRef(workspace.branch), newCommit], { cwd: p })
      await this._git(["update-ref", `refs/heads/${workspace.branch}`, newCommit, currentHead], {
        cwd: p,
      })
      LocalGitWorkspace._recordCommit(state, workspace, approvedDiff.ref, newCommit)
      return newCommit
    })
  }

  async restorePaths(workspace: GitWorkBranch, paths: string[]): Promise<void> {
    await this._lock.run(async () => {
      const state = this._getState(workspace)
      if (state.review === undefined) {
        throw new GitWorkspaceError("restore_paths requires a reviewed diff")
      }
      const root = path.resolve(workspace.path)
      const resolved: Array<[string, string]> = []
      for (const rel of paths) {
        const candidate = path.resolve(root, rel)
        if (
          !rel ||
          path.isAbsolute(rel) ||
          rel.split(/[\\/]/).includes("..") ||
          candidate === root ||
          !isRelativeTo(candidate, root)
        ) {
          throw new GitWorkspaceError("restore path escapes workspace")
        }
        resolved.push([rel, candidate])
      }

      for (const [rel, candidate] of resolved) {
        const reviewedPath = await this._git(
          ["ls-tree", "--name-only", "-z", state.review.tree, "--", rel],
          { cwd: root },
        )
        if (reviewedPath.length) {
          await this._git(
            ["restore", `--source=${state.review.tree}`, "--staged", "--worktree", "--", rel],
            { cwd: root },
          )
          continue
        }
        await this._git(["rm", "--cached", "--ignore-unmatch", "--", rel], { cwd: root })
        fs.rmSync(candidate, { force: true })
      }
    })
  }

  static _recordCommit(
    state: _WorkspaceState,
    workspace: GitWorkBranch,
    artifact: ArtifactRef,
    commit: CommitHash,
  ): void {
    state.review = undefined
    state.committed = { artifact, commit }
    state.workspace.base_commit = commit
    workspace.base_commit = commit
  }

  async _reconcileInstalledReview(
    workspace: GitWorkBranch,
    approvedDiff: GitDiff,
  ): Promise<CommitHash | null> {
    const marker = await this._resolveReviewMarker(workspace.branch)
    if (marker === null) {
      return null
    }

    const p = workspace.path
    const currentHead = (await this._git(["rev-parse", "HEAD"], { cwd: p })).toString().trim()
    if (currentHead !== marker) {
      return null
    }

    const parent = await this._git(["rev-parse", "--verify", `${marker}^{commit}^`], {
      cwd: p,
      check: false,
    })
    if (!parent.toString().trim()) {
      return null
    }
    const reviewedDiff = await this._git(
      ["diff", "--binary", parent.toString("ascii").trim(), marker],
      { cwd: p },
    )
    const result = this._diffWriter(reviewedDiff)
    const artifact = result instanceof Promise ? await result : result
    if (artifact !== approvedDiff.ref) {
      return null
    }
    return marker
  }

  async _resolveReviewMarker(branch: string): Promise<CommitHash | null> {
    const output = await this._git(
      ["rev-parse", "--verify", `${LocalGitWorkspace._reviewRef(branch)}^{commit}`],
      { cwd: this._repo, check: false },
    )
    return output.toString("ascii").trim() || null
  }

  static _reviewRef(branch: string): string {
    return `refs/athena/reviews/${branch}`
  }

  async remove(
    workspace: GitWorkBranch,
    opts?: { deleteBranch?: boolean; force?: boolean },
  ): Promise<void> {
    await this._lock.run(async () => {
      this._getState(workspace)
      const p = workspace.path
      if (fs.existsSync(p)) {
        if (!opts?.force && (await this._git(["status", "--porcelain"], { cwd: p })).length) {
          throw new GitWorkspaceError("worktree 包含未提交变更")
        }
        const args = ["worktree", "remove"]
        if (opts?.force) {
          args.push("--force")
        }
        args.push(p)
        await this._git(args)
      }
      if (opts?.deleteBranch) {
        await this._git(["branch", "-D", workspace.branch])
      }
      this._states.delete(path.resolve(p))
    })
  }

  _getState(workspace: GitWorkBranch): _WorkspaceState {
    const p = path.resolve(workspace.path)
    const state = this._states.get(p)
    if (state === undefined) {
      throw new GitWorkspaceError("未知的 worktree")
    }
    if (state.workspace.branch !== workspace.branch) {
      throw new GitWorkspaceError("worktree 分支与已注册状态不匹配")
    }
    return state
  }

  async _resolveCommit(commit: string): Promise<CommitHash> {
    if (!commit) {
      throw new GitWorkspaceError("commit 不能为空")
    }
    const trimmed = commit.trim()
    if (!trimmed) {
      throw new GitWorkspaceError("commit 不能为空")
    }
    let output: Buffer
    try {
      output = await this._git(["rev-parse", "--verify", `${trimmed}^{commit}`])
    } catch (exc) {
      if (exc instanceof GitWorkspaceError) {
        throw new GitWorkspaceError(`未知或非 commit 对象：${trimmed}`)
      }
      throw exc
    }
    return output.toString("ascii").trim()
  }

  _validateBranch(branch: string): void {
    if (
      !branch ||
      branch.startsWith("-") ||
      branch.startsWith("/") ||
      branch.startsWith(".") ||
      branch.endsWith("/") ||
      branch.endsWith(".") ||
      branch.endsWith(".lock") ||
      branch.includes("..") ||
      branch.includes("@{") ||
      [...branch].some((c) => /\s/.test(c) || "~^:?*[\\".includes(c))
    ) {
      throw new GitWorkspaceError("非法分支名")
    }
  }

  async _git(args: string[], opts: { cwd?: string; check?: boolean } = {}): Promise<Buffer> {
    const cwd = opts.cwd ?? this._repo
    try {
      const { stdout } = (await execFileP("git", ["-C", cwd, ...args], {
        encoding: "buffer" as const,
        maxBuffer: 100 * 1024 * 1024,
      })) as { stdout: Buffer }
      return Buffer.from(stdout)
    } catch (err) {
      if (opts.check === false) return Buffer.alloc(0)
      const e = err as { stderr?: Buffer | string }
      const stderr =
        typeof e.stderr === "string"
          ? e.stderr
          : e.stderr
            ? Buffer.from(e.stderr).toString()
            : String(err)
      throw new GitWorkspaceError(`git ${args.join(" ")} 失败: ${stderr.slice(0, 200)}`)
    }
  }
}
