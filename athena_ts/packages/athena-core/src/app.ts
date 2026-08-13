import { createHash } from "node:crypto"
import { Context } from "cordis"
import { LocalArtifactStore } from "./services/artifact-store.js"
import { LocalGitWorkspace } from "./services/git-workspace.js"
import { resolveWorkspacePath } from "./services/workspace.js"
import type { BinaryDiffWriter } from "./services/workspace.js"

/** M0 组合根配置——工作区根、仓库路径与 worktree 根。 */
export interface AthenaAppConfig {
  workspaceRoot: string
  repoPath: string
  worktreeRoot: string
  diffWriter?: BinaryDiffWriter
}

/** M0 workspace 服务——工作区根 + 越界拒绝的路径解析。 */
export interface WorkspaceService {
  root: string
  resolve(rel: string): string
}

declare module "cordis" {
  interface Context {
    workspace: WorkspaceService
    artifacts: LocalArtifactStore
    gitWorkspace: LocalGitWorkspace
  }
}

const defaultDiffWriter: BinaryDiffWriter = (content) =>
  `artifact://git-diff/${createHash("sha256").update(content).digest("hex")}`

/**
 * 组合根：创建根 Context 并注册基础服务（workspace / artifacts / gitWorkspace）。
 *
 * cordis@4.0.0-rc.8 的服务注册经 ``ctx.provide(name, value)`` 在插件 fiber 内完成，
 * 因此本函数异步（``await ctx.plugin(...)``）后返回可访问三服务的根 Context。
 */
export async function createAthenaApp(config: AthenaAppConfig): Promise<Context> {
  const ctx = new Context()
  await ctx.plugin((c) => {
    c.provide("workspace", {
      root: config.workspaceRoot,
      resolve: (rel: string) => resolveWorkspacePath(config.workspaceRoot, rel),
    } satisfies WorkspaceService)
    c.provide("artifacts", new LocalArtifactStore(config.workspaceRoot))
    c.provide(
      "gitWorkspace",
      new LocalGitWorkspace(config.repoPath, config.worktreeRoot, config.diffWriter ?? defaultDiffWriter)
    )
  })
  return ctx
}
