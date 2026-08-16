import { mkdirSync, writeFileSync } from "node:fs"
import { join } from "node:path"
import type { StageContext } from "../core/pipeline-types.js"
import { paperRoot } from "./paper-composer.js"

export interface PackagingResult {
  root: string
  evidenceRef: string
}

export class PackagingService {
  async package(ctx: StageContext): Promise<PackagingResult> {
    const root = join(ctx.projectRoot, ".athena", "autoresearch", "packaging", ctx.runId)
    mkdirSync(root, { recursive: true })

    const evidence = {
      version: 1,
      run_id: ctx.runId,
      sota_experiment_id: ctx.tree.bestExperimentId(),
      hypotheses: Object.values(ctx.tree.toDict().hypotheses).map((hypothesis) => ({
        id: hypothesis.id,
        statement: hypothesis.statement,
        status: hypothesis.status,
        priority: hypothesis.priority,
      })),
    }
    const evidenceText = JSON.stringify(evidence, null, 2)
    writeFileSync(join(root, "experiment_evidence.json"), evidenceText)
    const evidenceRef = await ctx.artifacts.putText(evidenceText)

    const draftPath = join(paperRoot(ctx), "paper_draft.md")
    writeFileSync(join(root, "paper_draft.md"), `# ${String(ctx.state.paper_spec.title ?? "Untitled AutoResearch")}\n`)
    void draftPath

    return { root, evidenceRef }
  }
}
