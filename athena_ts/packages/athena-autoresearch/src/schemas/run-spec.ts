import { z } from "zod"

export const RUN_SPEC_SCHEMA = "autoresearch-run-spec/v1"

export const ProviderRefSchema = z.strictObject({
  id: z.string().min(1),
  version: z.string().optional(),
  config: z.record(z.string(), z.unknown()).optional(),
})
export type ProviderRef = z.infer<typeof ProviderRefSchema>

export const StageSpecSchema = z.strictObject({
  id: z.string().min(1),
  provider: z.string().min(1),
  config: z.record(z.string(), z.unknown()).optional(),
  depends_on: z.array(z.string()).optional(),
  gates: z.array(z.string()).optional(),
  allow_skip: z.boolean().optional(),
  retry: z
    .strictObject({
      max: z.number().int().min(0),
      backoff_ms: z.number().int().min(0),
    })
    .optional(),
})
export type StageSpec = z.infer<typeof StageSpecSchema>

export const BudgetsSchema = z.strictObject({
  max_ideas: z.number().int().min(1),
  max_experiments: z.number().int().min(1),
  max_paper_rounds: z.number().int().min(1),
  max_tokens: z.number().int().min(0),
  project_time_limit: z.string(),
})
export type Budgets = z.infer<typeof BudgetsSchema>

export const PaperSpecSchema = z.strictObject({
  template: z.enum(["iclr2026", "icml2026", "neurips2026", "plain_latex", "markdown"]),
  venue: z.string(),
  main_language: z.enum(["latex", "markdown"]),
  latex_via: z.enum(["overleaf", "local", "none"]),
  local_compiler: z.enum(["latexmk", "tectonic", "pdflatex"]).optional(),
  title: z.string(),
  authors: z.array(z.string()),
  abstract: z.string().default(""),
  figure_skill: z.string().default("native-svg"),
})
export type PaperSpec = z.infer<typeof PaperSpecSchema>

export const RunSpecSchema = z.strictObject({
  schema: z.literal(RUN_SPEC_SCHEMA),
  run_id: z.string().optional(),
  stages: z.array(StageSpecSchema).min(1),
  providers: z.strictObject({
    experiment_engine: ProviderRefSchema,
    literature: ProviderRefSchema.optional(),
    figure_skill: ProviderRefSchema,
    template: ProviderRefSchema,
    compiler: ProviderRefSchema,
    reviewer: ProviderRefSchema.optional(),
  }),
  budgets: BudgetsSchema,
  paper_spec: PaperSpecSchema,
  timeout: z.strictObject({
    stage_ms: z.number().int().min(1000),
    project: z.string(),
  }),
})
export type RunSpec = z.infer<typeof RunSpecSchema>

export const DEFAULT_RUN_SPEC: RunSpec = {
  schema: RUN_SPEC_SCHEMA,
  stages: [
    { id: "intake", provider: "intake" },
    { id: "ideation", provider: "ideation" },
    { id: "experiment", provider: "experiment" },
    { id: "writing", provider: "writing" },
    { id: "refinement", provider: "refinement" },
    { id: "packaging", provider: "packaging" },
  ],
  providers: {
    experiment_engine: { id: "athena" },
    literature: { id: "none-literature" },
    figure_skill: { id: "native-svg" },
    template: { id: "bundled" },
    compiler: { id: "none" },
    reviewer: { id: "builtin" },
  },
  budgets: {
    max_ideas: 20,
    max_experiments: 10,
    max_paper_rounds: 5,
    max_tokens: 0,
    project_time_limit: "PT12H",
  },
  paper_spec: {
    template: "plain_latex",
    venue: "workshop",
    main_language: "latex",
    latex_via: "none",
    title: "Untitled AutoResearch",
    authors: [],
    abstract: "",
    figure_skill: "native-svg",
  },
  timeout: { stage_ms: 600_000, project: "PT12H" },
}

export interface RunSpecOverride {
  run_id?: string
  stages?: StageSpec[]
  providers?: Partial<RunSpec["providers"]>
  budgets?: Partial<Budgets>
  paper_spec?: Partial<PaperSpec>
  timeout?: Partial<RunSpec["timeout"]>
}

export function parseRunSpec(data: unknown): RunSpec {
  return RunSpecSchema.parse(data)
}

export function mergeRunSpecs(base: RunSpec, override: RunSpecOverride = {}): RunSpec {
  return parseRunSpec({
    ...base,
    ...(override.run_id ? { run_id: override.run_id } : {}),
    ...(override.stages ? { stages: override.stages } : {}),
    providers: { ...base.providers, ...override.providers },
    budgets: { ...base.budgets, ...override.budgets },
    paper_spec: { ...base.paper_spec, ...override.paper_spec },
    timeout: { ...base.timeout, ...override.timeout },
  })
}
