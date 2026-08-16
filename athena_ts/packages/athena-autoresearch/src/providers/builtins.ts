import type { Gate, GateResult } from "../core/gate-runner.js"
import type { StageContext, StageResult } from "../core/pipeline-types.js"
import type {
  CompileResult,
  CompilerProvider,
  ExperimentEngineProvider,
  FigureDesignInput,
  FigureDesignResult,
  FigureSkillProvider,
  LiteratureProvider,
  LiteratureRecord,
  SettlementEvent,
  TemplateDir,
  TemplateProvider,
} from "./types.js"

// ── Native SVG figure skill ────────────────────────────────────────────
const escapeXml = (text: string) =>
  text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&apos;")

export class NativeSvgProvider implements FigureSkillProvider {
  readonly id = "native-svg" as const
  readonly version = "0.1.0"
  readonly capabilities = ["figure.svg"]

  async selfTest(_ctx: StageContext): Promise<{ ok: boolean; issues: string[] }> {
    return { ok: true, issues: [] }
  }

  async generate(ctx: StageContext, input: FigureDesignInput): Promise<FigureDesignResult> {
    const width = input.constraints.maxWidthPx
    const height = input.constraints.maxHeightPx
    const [fill, stroke, text] = input.constraints.palette
    const blocks = input.context.sotaPath
      .map((label, index) => {
        const x = 40 + index * 180
        return [
          `<rect x="${x}" y="80" width="140" height="60" rx="8" fill="${fill}" stroke="${stroke}" stroke-width="2"/>`,
          `<text x="${x + 70}" y="115" text-anchor="middle" font-family="sans-serif" font-size="14" fill="${text}">${escapeXml(label)}</text>`,
        ].join("")
      })
      .join("\n")
    const svg = [
      `<?xml version="1.0" encoding="UTF-8"?>`,
      `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="img" aria-label="${escapeXml(input.title)}">`,
      `<title>${escapeXml(input.title)}</title>`,
      blocks,
      `<text x="${Math.floor(width / 2)}" y="${Math.floor(height - 20)}" text-anchor="middle" font-family="sans-serif" font-size="12" fill="${text}">${escapeXml(input.title)}</text>`,
      `</svg>`,
    ].join("\n")
    const svgRef = await ctx.artifacts.putText(svg)
    return {
      svgRef,
      svgPath: `figures/${input.name}.svg`,
      selfCheck: { syntaxOk: true, viewboxOk: true, textLegible: true, paletteOk: true, issues: [] },
    }
  }
}

// ── Experiment stub (occupies default athena slot) ─────────────────────
export class StubExperimentEngine implements ExperimentEngineProvider {
  readonly id = "athena" as const
  readonly version = "0.1.0"
  readonly capabilities = ["experiment.engine"]

  async start(ctx: StageContext): Promise<void> {
    ctx.console.info(`[stub-engine] start with ${ctx.pool.countQueued()} queued hypotheses`)
  }

  async recover(ctx: StageContext): Promise<void> {
    ctx.console.info("[stub-engine] recover")
  }

  async *settleEvents(_ctx: StageContext): AsyncIterable<SettlementEvent> {
    yield* []
  }
}

// ── Bundled template ───────────────────────────────────────────────────
export class BundledTemplateProvider implements TemplateProvider {
  readonly id = "bundled" as const
  readonly version = "0.1.0"
  readonly capabilities = ["paper.template"]

  async fetch(venue: string): Promise<TemplateDir> {
    return {
      root: `bundled/${venue}`,
      source: "bundled",
      mainFile: venue === "markdown" ? "paper_draft.md" : "main.tex",
      styleFiles: [],
      editableFiles: venue === "markdown" ? ["paper_draft.md"] : ["main.tex", "sections"],
    }
  }

  list(): string[] {
    return ["plain_latex", "markdown"]
  }
}

// ── No-compile compiler ────────────────────────────────────────────────
export class NoneCompilerProvider implements CompilerProvider {
  readonly id = "none" as const
  readonly version = "0.1.0"
  readonly capabilities = ["paper.compile"]

  async compile(ctx: StageContext, paperDir: string): Promise<CompileResult> {
    const consoleText = `no compile: ${paperDir} uses markdown draft path`
    const logRef = await ctx.artifacts.putText(consoleText)
    return { ok: true, logRef, consoleText, compiler: "none" }
  }
}

// ── Empty literature ───────────────────────────────────────────────────
export class NoneLiteratureProvider implements LiteratureProvider {
  readonly id = "none-literature" as const
  readonly version = "0.1.0"
  readonly capabilities = ["literature.search"]

  async search(_ctx: StageContext, _query: string): Promise<LiteratureRecord[]> {
    return []
  }

  async fetchEvidence(_ctx: StageContext, _refs: string[]): Promise<string[]> {
    return []
  }
}

// ── Built-in gates ─────────────────────────────────────────────────────
export class PassGate implements Gate {
  readonly id = "always_pass"
  readonly retryable = false

  async check(_ctx: unknown, _stageResult: StageResult): Promise<GateResult> {
    return { pass: true, itemScores: [{ item: "pass", score: 1, evidence: "always pass" }], feedback: [] }
  }
}

export class FailGate implements Gate {
  readonly id = "always_fail"
  readonly retryable = true

  async check(_ctx: unknown, _stageResult: StageResult): Promise<GateResult> {
    return {
      pass: false,
      blockingFactor: "always_fail",
      itemScores: [{ item: "fail", score: 0, evidence: "always fail" }],
      feedback: ["built-in fail gate triggered"],
    }
  }
}

export function createBuiltinGates(): Gate[] {
  return [new PassGate(), new FailGate()]
}
