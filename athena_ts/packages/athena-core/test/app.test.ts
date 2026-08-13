import { mkdtempSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { describe, expect, it } from "vitest"
import { createAthenaApp } from "../src/app.js"

describe("createAthenaApp", () => {
  it("exposes workspace, artifacts, gitWorkspace on the root context", async () => {
    const root = mkdtempSync(join(tmpdir(), "athena-app-"))
    try {
      const ctx = await createAthenaApp({
        workspaceRoot: join(root, "ws"),
        repoPath: join(root, "repo"),
        worktreeRoot: join(root, "worktrees"),
      })
      expect(ctx.workspace).toBeTruthy()
      expect(ctx.artifacts).toBeTruthy()
      expect(ctx.gitWorkspace).toBeTruthy()
      const ref = await ctx.artifacts.putText("hello")
      expect(await ctx.artifacts.getText(ref)).toBe("hello")
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })
})
