import type { Stage, StageContext, StageResult } from "./core/pipeline-types.js"
import type {
  CompilerProvider,
  ExperimentEngineProvider,
  FigureSkillProvider,
  TemplateProvider,
} from "./providers/types.js"
import { PaperComposer, paperRoot } from "./services/paper-composer.js"
import { PackagingService } from "./services/packaging.js"

type Handler = (ctx: StageContext) => Promise<StageResult>

const defineStage = (
  id: string,
  canEnter: (ctx: StageContext) => boolean,
  run: Handler,
): Stage => ({ id, canEnter, run })

const always = () => true

const composer = new PaperComposer()
const packaging = new PackagingService()

export const IntakeStage: Stage = defineStage("intake", always, async (ctx) => {
  const template = ctx.providers.get<TemplateProvider>(ctx.spec.providers.template.id)
  const dir = await template.fetch(String(ctx.state.paper_spec.template ?? "plain_latex"))
  ctx.console.info(`[intake] template ready: ${dir.root} (${dir.source})`)
  return { status: "COMPLETED", artifacts: { template_dir: dir.root } }
})

export const IdeationStage: Stage = defineStage(
  "ideation",
  (ctx) => ctx.budgets.ideasRemaining(ctx.pool) > 0,
  async (ctx) => {
    const remaining = ctx.budgets.ideasRemaining(ctx.pool)
    ctx.console.info(`[ideation] budget allows ${remaining} more ideas`)
    return { status: "COMPLETED" }
  },
)

export const ExperimentStage: Stage = defineStage(
  "experiment",
  (ctx) => ctx.pool.countQueued() > 0,
  async (ctx) => {
    const engine = ctx.providers.get<ExperimentEngineProvider>(ctx.spec.providers.experiment_engine.id)
    await engine.start(ctx)
    for await (const settlement of engine.settleEvents(ctx)) {
      ctx.console.info(`[experiment] settled ${settlement.hypothesisId}: ${settlement.outcome}`)
    }
    return { status: "COMPLETED" }
  },
)

export const WritingStage: Stage = defineStage("writing", always, async (ctx) => {
  const figure = ctx.providers.get<FigureSkillProvider>(ctx.spec.providers.figure_skill.id)
  const selfTest = await figure.selfTest(ctx)
  if (!selfTest.ok) ctx.console.error(`[writing] figure skill self-test failed: ${selfTest.issues.join("; ")}`)
  const generated = await figure.generate(ctx, {
    runId: ctx.runId,
    name: "architecture",
    kind: "architecture",
    title: String(ctx.state.paper_spec.title ?? "architecture"),
    context: { sotaPath: [], evidence: {}, draftSectionRefs: [] },
    constraints: {
      format: "svg",
      maxWidthPx: 1600,
      maxHeightPx: 1200,
      palette: ["#ffffff", "#333333", "#111111"],
      noExternalAssets: true,
    },
  })
  const paper = await composer.compose(ctx)
  return {
    status: "COMPLETED",
    artifacts: { architecture_svg: generated.svgRef, paper_draft: paper.draftRef, paper_dir: paper.root },
  }
})

export const RefinementStage: Stage = defineStage("refinement", always, async (ctx) => {
  const compiler = ctx.providers.get<CompilerProvider>(ctx.spec.providers.compiler.id)
  const root = paperRoot(ctx)
  const paperDir = { root, draftPath: `${root}/paper_draft.md`, draftRef: "" }

  let compileResult = await compiler.compile(ctx, root)
  let attempts = 0
  while (!compileResult.ok && !ctx.budgets.timeLimitReached()) {
    attempts += 1
    await composer.reviseLatex(ctx, paperDir, compileResult.consoleText)
    ctx.console.compileOutput({ ok: false, consoleText: compileResult.consoleText, logRef: compileResult.logRef, compiler: compileResult.compiler })
    compileResult = await compiler.compile(ctx, root)
  }
  ctx.console.compileOutput({ ok: compileResult.ok, consoleText: compileResult.consoleText, logRef: compileResult.logRef, compiler: compileResult.compiler })

  return compileResult.ok
    ? { status: "COMPLETED", artifacts: { compile_log: compileResult.logRef } }
    : { status: "WAITING", artifacts: { compile_log: compileResult.logRef }, consoleLines: [`compile failed after ${attempts} repair attempt(s)`] }
})

export const PackagingStage: Stage = defineStage("packaging", always, async (ctx) => {
  const result = await packaging.package(ctx)
  ctx.console.info(`[packaging] run ${ctx.runId} packaged at ${result.root}`)
  return { status: "COMPLETED", artifacts: { packaging_dir: result.root, experiment_evidence: result.evidenceRef } }
})

export function createDefaultStages(): Map<string, Stage> {
  return new Map<string, Stage>([
    ["intake", IntakeStage],
    ["ideation", IdeationStage],
    ["experiment", ExperimentStage],
    ["writing", WritingStage],
    ["refinement", RefinementStage],
    ["packaging", PackagingStage],
  ])
}
