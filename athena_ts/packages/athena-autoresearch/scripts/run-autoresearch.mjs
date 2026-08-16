/**
 * Generic AutoResearch smoke runner.
 *
 * Usage:
 *   node packages/athena-autoresearch/scripts/run-autoresearch.mjs <data-directory>
 *
 * The script only receives a directory. It contains no dataset-specific logic;
 * exploration and paper content are expected to be produced by LLM providers
 * once wired. The current stub providers run the full stage machine so the
 * plugin/runtime path can be tested against any directory (e.g. examples/titanic).
 */
import { existsSync } from "node:fs"
import { dirname, resolve, join } from "node:path"
import { fileURLToPath } from "node:url"
import { HypothesisSchema } from "@athena/core"
import {
  AutoResearchRuntime,
  HypothesisPool,
  PooledHypothesisSchema,
} from "../dist/index.js"

const scriptDir = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(scriptDir, "..", "..", "..", "..")
const rawDir = process.argv[2] ?? "examples/titanic"
const fromCwd = resolve(rawDir)
const inputDir = existsSync(fromCwd) ? fromCwd : resolve(repoRoot, rawDir)
const runId = `ar_${Date.now().toString(36)}`
const poolPath = join(inputDir, ".athena", "autoresearch", "hypothesis_pool.json")
const pool = new HypothesisPool(poolPath)
pool.load()

if (pool.countQueued() === 0) {
  const now = new Date().toISOString()
  pool.upsert(
    PooledHypothesisSchema.parse({
      pool_id: `h_${Date.now().toString(36)}`,
      hypothesis: HypothesisSchema.parse({
        id: null,
        statement: "Explore the provided data directory and establish a baseline.",
        intervention: "Run a generic first exploration of the provided data directory.",
        expected_effect: "Produce an initial result to seed the research loop.",
      }),
      pool_status: "QUEUED",
      origin: "manual",
      created_at: now,
      updated_at: now,
    }),
  )
}

const runtime = new AutoResearchRuntime({
  projectRoot: inputDir,
  pool,
  runSpec: { run_id: runId },
})

const result = await runtime.start()
console.log(
  JSON.stringify(
    {
      input_dir: inputDir,
      run_id: runId,
      status: result.status,
      phase: runtime.currentState.phase,
      stopped_by: runtime.currentState.stopped_by ?? null,
      paper_draft: join(inputDir, ".athena", "autoresearch", "paper", runId, "paper_draft.md"),
      packaging: join(inputDir, ".athena", "autoresearch", "packaging", runId),
    },
    null,
    2,
  ),
)
