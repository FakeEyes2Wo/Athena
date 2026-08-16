import type { Provider } from "../core/provider-registry.js"
import type { StageContext } from "../core/pipeline-types.js"

// ── Figure ────────────────────────────────────────────────────────────
export type FigureKind = "architecture" | "method" | "flow" | "ablation_overview"

export interface FigureDesignInput {
  runId: string
  name: string
  kind: FigureKind
  title: string
  context: {
    sotaPath: string[]
    evidence: Record<string, unknown>
    draftSectionRefs: string[]
  }
  constraints: {
    format: "svg"
    maxWidthPx: number
    maxHeightPx: number
    palette: string[]
    noExternalAssets: true
  }
}

export interface FigureDesignResult {
  svgRef: string
  svgPath: string
  renderedPdfRef?: string
  renderedPngRef?: string
  selfCheck: {
    syntaxOk: boolean
    viewboxOk: boolean
    textLegible: boolean
    paletteOk: boolean
    issues: string[]
  }
}

export interface FigureSkillProvider extends Provider {
  readonly id: "native-svg" | "fireworks-tech-graph" | "drawio-skill" | "ink-graph"
  selfTest(ctx: StageContext): Promise<{ ok: boolean; issues: string[] }>
  generate(ctx: StageContext, input: FigureDesignInput): Promise<FigureDesignResult>
}

// ── Experiment ─────────────────────────────────────────────────────────
export interface SettlementEvent {
  hypothesisId: string
  experimentId: string
  outcome: "WIN" | "DRAW" | "LOSS" | "INCONCLUSIVE"
  metric: number | null
  referenceMetric: number | null
  direction: "maximize" | "minimize"
  sotaAfter: string | null
}

export interface ExperimentEngineProvider extends Provider {
  readonly id: "athena" | "mcts" | "external" | "stub"
  start(ctx: StageContext): Promise<void>
  recover(ctx: StageContext): Promise<void>
  settleEvents(ctx: StageContext): AsyncIterable<SettlementEvent>
}

// ── Template ───────────────────────────────────────────────────────────
export interface TemplateDir {
  root: string
  source: "network" | "cache" | "bundled"
  mainFile: string
  styleFiles: string[]
  editableFiles: string[]
}

export interface TemplateProvider extends Provider {
  readonly id: "curl" | "cache" | "bundled"
  fetch(venue: string): Promise<TemplateDir>
  list(): string[]
}

// ── Compiler ───────────────────────────────────────────────────────────
export interface CompileResult {
  ok: boolean
  pdfRef?: string
  logRef: string
  consoleText: string
  compiler: string
}

export interface CompilerProvider extends Provider {
  readonly id: "local" | "overleaf" | "none"
  compile(ctx: StageContext, paperDir: string): Promise<CompileResult>
}

// ── Literature ─────────────────────────────────────────────────────────
export interface LiteratureRecord {
  title: string
  url: string
  abstract: string
}

export interface LiteratureProvider extends Provider {
  readonly id: "none-literature" | "paper-rag"
  search(ctx: StageContext, query: string): Promise<LiteratureRecord[]>
  fetchEvidence(ctx: StageContext, refs: string[]): Promise<string[]>
}
