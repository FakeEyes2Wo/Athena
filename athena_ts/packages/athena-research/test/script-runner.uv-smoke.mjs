// Explicit integration check: build packages, then run this file with Node.
// Requires uv and a locally installed Python >= 3.11; never downloads dependencies.
import assert from "node:assert/strict"
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { LocalArtifactStore } from "@athena/core"
import { DataScriptRunner, TrustedEvaluator } from "../dist/index.js"

const root = mkdtempSync(join(tmpdir(), "athena-uv-smoke-"))
process.env.UV_OFFLINE = "1"
process.env.UV_PYTHON_DOWNLOADS = "never"
process.env.UV_CACHE_DIR = join(root, "uv-cache")
try {
  const draft = join(root, "draft")
  mkdirSync(draft)
  writeFileSync(join(draft, "pyproject.toml"), '[project]\nname = "athena-evaluator-smoke"\nversion = "0.1.0"\nrequires-python = ">=3.11"\ndependencies = []\n')
  writeFileSync(join(draft, "labels.csv"), "1\n")
  writeFileSync(join(draft, "result.json"), '{"primary":999}')
  writeFileSync(join(draft, "evaluate.py"), [
    "import argparse, json",
    "from pathlib import Path",
    "parser = argparse.ArgumentParser()",
    "parser.add_argument('--request')",
    "parser.add_argument('--output')",
    "args = parser.parse_args()",
    "assert json.loads(Path(args.request).read_text()) == {}",
    "label = float(Path('labels.csv').read_text())",
    "prediction = float(Path('predictions/value.csv').read_text())",
    "Path(args.output).write_text(json.dumps({'primary': abs(label - prediction)}))",
  ].join("\n"))
  const store = new LocalArtifactStore(join(root, "artifacts"))
  const runner = new DataScriptRunner(store, join(root, "runs"))
  const bundle = await runner.freeze(draft, "evaluate.py")
  // Evaluation must use frozen labels/source, not the mutable draft.
  writeFileSync(join(draft, "labels.csv"), "100\n")
  writeFileSync(join(draft, "evaluate.py"), "raise RuntimeError('draft must not execute')")
  const result = await new TrustedEvaluator(runner).score({
    evalBundle: bundle,
    predictions: { "value.csv": Buffer.from("0.75\n") },
    candidateId: "smoke",
    direction: "minimize",
    predictionsRoot: "predictions",
  })
  assert.equal(result.test_score, 0.25)
  assert.equal(result.candidate_id, "smoke")
  assert.equal(result.direction, "minimize")
  console.log("PASS: offline uv freeze -> restored frozen evaluator -> trusted score 0.25")
} finally {
  rmSync(root, { recursive: true, force: true })
}
