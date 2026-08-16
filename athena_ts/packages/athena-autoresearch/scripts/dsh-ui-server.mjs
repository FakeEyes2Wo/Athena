import { createServer } from "node:http"
import { readFileSync, existsSync } from "node:fs"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"
import { HypothesisSchema, ResearchTree, buildHypothesisGraph } from "@athena/core"
import {
  AutoResearchRuntime,
  HypothesisPool,
  MemoryConsolePort,
  PooledHypothesisSchema,
} from "../dist/index.js"

const scriptDir = dirname(fileURLToPath(import.meta.url))
const packageDir = resolve(scriptDir, "..")
const repoRoot = resolve(packageDir, "..", "..", "..")

function resolveDir(raw) {
  const direct = resolve(raw ?? "examples/titanic")
  return existsSync(direct) ? direct : resolve(repoRoot, raw ?? "examples/titanic")
}

function seedIfEmpty(pool) {
  if (pool.countQueued() > 0) return
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

let runtime = null
let consolePort = new MemoryConsolePort()
let running = false

function statusPayload() {
  if (!runtime) {
    return {
      run_id: null,
      phase: "NOT_STARTED",
      status: "NOT_STARTED",
      sota_experiment_id: null,
      queued_hypotheses: 0,
      console_lines: consolePort.lines,
    }
  }
  return {
    run_id: runtime.currentState.run_id,
    phase: runtime.currentState.phase,
    status: runtime.currentState.status,
    stopped_by: runtime.currentState.stopped_by ?? null,
    sota_experiment_id: runtime.getTree?.().bestExperimentId?.() ?? null,
    queued_hypotheses: runtime.getPool?.().countQueued?.() ?? 0,
    console_lines: consolePort.lines,
  }
}

function graphFromPool(pool) {
  const entries = pool?.list?.() ?? []
  const nodes = entries.map((entry) => {
    const hypothesis = entry.hypothesis
    return {
      id: entry.pool_id,
      experiment_id: entry.experiment_ids?.[0] ?? null,
      statement: hypothesis.statement,
      status: entry.pool_status,
      priority: hypothesis.priority,
      order: hypothesis.order,
      supersedes: [...(hypothesis.supersedes ?? [])],
      parent_id: hypothesis.parent_id ?? null,
      sources: [...(hypothesis.sources ?? [])],
      primary: entry.best_metric,
      sota: entry.pool_status === "SUPPORTED" && entry.comparison_outcome === "WIN",
    }
  })
  const ids = new Set(nodes.map((node) => node.id))
  const edges = []
  for (const node of nodes) {
    if (node.parent_id !== null && ids.has(node.parent_id)) {
      edges.push({
        id: `lineage:${node.parent_id}->${node.id}`,
        source: node.parent_id,
        target: node.id,
        kind: "lineage",
        label: "父→子",
      })
    }
    for (const superseded of node.supersedes) {
      if (ids.has(superseded)) {
        edges.push({
          id: `supersedes:${node.id}->${superseded}`,
          source: node.id,
          target: superseded,
          kind: "supersedes",
          label: "取代",
        })
      }
    }
  }
  return { nodes, edges }
}

function graphPayload() {
  if (!runtime) return { nodes: [], edges: [] }
  const tree = runtime.getTree?.()
  if (tree) {
    const graph = buildHypothesisGraph(tree)
    if (graph.nodes.length > 0) return graph
  }
  return graphFromPool(runtime.getPool?.())
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url, "http://127.0.0.1")
  res.setHeader("Access-Control-Allow-Origin", "*")
  res.setHeader("Access-Control-Allow-Headers", "Content-Type")
  if (req.method === "OPTIONS") {
    res.writeHead(204).end()
    return
  }

  if (url.pathname === "/api/status") {
    res.setHeader("Content-Type", "application/json; charset=utf-8")
    res.end(JSON.stringify(statusPayload()))
    return
  }

  if (url.pathname === "/api/run" && req.method === "POST") {
    if (running) {
      res.writeHead(409, { "Content-Type": "application/json; charset=utf-8" })
      res.end(JSON.stringify({ ok: false, error: "already running" }))
      return
    }
    let body = ""
    for await (const chunk of req) body += chunk
    const { dir } = JSON.parse(body || "{}")
    const projectRoot = resolveDir(dir)
    const autoResearchDir = join(projectRoot, ".athena", "autoresearch")
    const pool = new HypothesisPool(join(autoResearchDir, "hypothesis_pool.json"))
    pool.load()
    seedIfEmpty(pool)
    consolePort = new MemoryConsolePort()
    const treePath = join(projectRoot, ".athena", "research_tree.json")
    const tree = existsSync(treePath) ? ResearchTree.load(treePath) : new ResearchTree()
    runtime = new AutoResearchRuntime({
      projectRoot,
      pool,
      tree,
      console: consolePort,
      runSpec: { run_id: `ar_ui_${Date.now().toString(36)}` },
    })
    running = true
    try {
      const result = await runtime.start()
      res.setHeader("Content-Type", "application/json; charset=utf-8")
      res.end(JSON.stringify({ ok: true, result: { status: result.status, phase: runtime.currentState.phase } }))
    } catch (error) {
      res.writeHead(500, { "Content-Type": "application/json; charset=utf-8" })
      res.end(JSON.stringify({ ok: false, error: String(error) }))
    } finally {
      running = false
    }
    return
  }

  if (url.pathname === "/api/hypothesis_graph") {
    res.setHeader("Content-Type", "application/json; charset=utf-8")
    res.end(JSON.stringify(graphPayload()))
    return
  }

  if (url.pathname === "/" || url.pathname === "/index.html") {
    res.setHeader("Content-Type", "text/html; charset=utf-8")
    res.end(readFileSync(join(packageDir, "dsh-ui", "index.html")))
    return
  }
  if (url.pathname === "/app.js") {
    res.setHeader("Content-Type", "text/javascript; charset=utf-8")
    res.end(readFileSync(join(packageDir, "dsh-ui", "app.js")))
    return
  }
  if (url.pathname === "/hypothesis-graph.js") {
    res.setHeader("Content-Type", "text/javascript; charset=utf-8")
    res.end(readFileSync(join(packageDir, "dsh-ui", "hypothesis-graph.js")))
    return
  }

  res.writeHead(404).end("not found")
})

const port = Number(process.env.PORT ?? 8787)
server.listen(port, () => {
  console.log(`Athena for DSH UI running at http://127.0.0.1:${port}`)
})
