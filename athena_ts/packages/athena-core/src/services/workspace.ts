import { z } from "zod"
import path from "node:path"
import { AthenaError } from "../errors.js"
import { ArtifactRef, CommitHash } from "../models/contracts.js"

export type BinaryDiffWriter = (content: Buffer) => Promise<ArtifactRef> | ArtifactRef

/** 解析 workspace 相对路径并拒绝越界（等价 resolve_workspace_path）。 */
export function resolveWorkspacePath(root: string, rel: string): string {
  const resolvedRoot = path.resolve(root)
  const candidate = path.resolve(root, rel)
  if (candidate !== resolvedRoot && !candidate.startsWith(resolvedRoot + path.sep)) {
    throw new AthenaError(`path escapes workspace: ${rel}`)
  }
  return candidate
}

/** Git workspace 操作无法完成时抛出的领域错误。 */
export class GitWorkspaceError extends AthenaError {}

/** 一个 Git worktree 的路径、分支与基线提交。 */
export const GitWorkBranchSchema = z.object({
  path: z.string(),
  branch: z.string(),
  base_commit: CommitHash,
})
export type GitWorkBranch = z.infer<typeof GitWorkBranchSchema>

/** 内容寻址的二进制 diff 与从基线变更的路径。 */
export const GitDiffSchema = z.object({
  ref: ArtifactRef,
  paths: z.array(z.string()),
})
export type GitDiff = z.infer<typeof GitDiffSchema>

/** 隔离 Git worktree 的抽象操作（等价 GitWorkspace ABC）。 */
export interface GitWorkspace {
  init(repoPath?: string, initialFile?: string, initialContent?: string): Promise<CommitHash>
  create(baseCommit: CommitHash, branch: string, opts?: { name?: string }): Promise<GitWorkBranch>
  diff(workspace: GitWorkBranch): Promise<GitDiff>
  commit(workspace: GitWorkBranch, approvedDiff: GitDiff, message: string): Promise<CommitHash>
  restorePaths(workspace: GitWorkBranch, paths: string[]): Promise<void>
  remove(workspace: GitWorkBranch, opts?: { deleteBranch?: boolean; force?: boolean }): Promise<void>
}
