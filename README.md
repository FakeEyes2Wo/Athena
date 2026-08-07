# Athena

Athena 是面向 AI4ML/AI4S 的自动研究系统。当前 Python 后端覆盖数据准备、假设生成、
隔离实验、评价比较、验证报告以及 Thread/Turn 运行时。

工程目录、模块 owner、当前能力与目标架构统一收录于
[`docs/README.md`](docs/README.md)。

## Run the complete Agent workflow

`src/main.py` is the composition root for PREPARE -> SEARCH -> VALIDATE ->
REPORT. It accepts one CSV dataset, freezes deterministic train/validation/test
splits and the evaluator, asks agents to generate experiment code, commits each
experiment in an isolated Git worktree, validates the selected result, and writes
a report backed by the recorded evidence.

Install the locked Python environment first. This includes
`qoder-agent-sdk==1.0.12`:

```bash
uv sync
```

The workflow has two independent model settings:

- `--model` is the PydanticAI model used for hypothesis generation and report
  narration. Its provider credentials must be available in the environment.
- `--backend` selects the experiment code generator. `codex` requires an
  installed, authenticated `codex` CLI on `PATH`. `qoder` uses the installed
  Python SDK and authenticates with `QODER_PERSONAL_ACCESS_TOKEN` or an existing
  authenticated `qodercli` session. `auto` routes between both backends and
  therefore requires both to be available.

Example using the Codex CLI backend from PowerShell:

```powershell
uv run python src/main.py `
  --data path\to\data.csv `
  --target label `
  --model openai:gpt-5 `
  --backend codex `
  --output-dir .athena\run
```

Use `--backend qoder` to generate experiment code through Qoder instead. Pass
`--code-model` only when the selected backend needs an explicit model override.
With `--hil`, Athena pauses after each completed search experiment and waits for
Enter before continuing. Run `uv run python src/main.py --help` for task type,
metric, budget, HIL, and debug options.

Each `--output-dir` is single-use and contains the frozen configuration in
`config/`, content-addressed data and evidence in `artifacts/`, the experiment
Git repository and worktrees, `research_tree.json`, a Markdown report in
`reports/`, and `run_summary.json` after successful completion. Athena exits
with code 2 for invalid configuration, 1 for a workflow/backend failure, and 130
when interrupted. It attempts to persist `research_tree.json` on workflow
failure; it does not emit a placeholder success or report without complete
validation evidence.

Experiments run in `local` mode — the default and only `--execution` value in
this build. Local execution grants the generated program no strong filesystem
or network isolation from the current user, so `run_summary.json` records
`execution: local` and `strong_isolation: false`, and Athena prints this
limitation once at startup. Generated code must expose `run_experiment.py` and
may create regular auxiliary files (for example `model.py`). Every experiment
writes predictions as exactly two columns, `__athena_row_id,prediction`, and
never sees trusted labels: baseline, SEARCH, and ablation receive train data
plus validation features; the frozen final-test receives train data plus test
features through a Git-ignored `.athena/phase_manifest.json`. The selected SOTA
is re-run unchanged on the final-test split without invoking any
code-generation backend, and its Git commit equals the SOTA commit. Logs,
predictions, and evaluation results are content-addressed (`sha256:`) in
`artifacts/objects/` and remain readable after the experiment worktree is
removed. The frozen final-test `diff` is not a `sha256:` object: because the
SOTA commit is unchanged there is no new diff to store, so it is a stable
`artifact://diffs/<experiment_id>` placeholder recording that the same commit
ran unchanged on the final-test features.

## Titanic example

The bundled Titanic dataset in `examples/titanic/` runs the complete workflow
end-to-end. `train.csv` (with the `Survived` target) is the single dataset
input; `test.csv` and `gender_submission.csv` are included for reference:

```powershell
uv run python src/main.py `
  --data examples/titanic/train.csv `
  --target Survived `
  --model openai:deepseek-chat `
  --backend codex `
  --output-dir .athena/titanic-run `
  --max-experiments 1 `
  --max-no-improve 1
```

Set `OPENAI_API_KEY` (or an `ATHENA_IDEATOR_MODEL` override) and, for the
DeepSeek-compatible endpoint, `OPENAI_BASE_URL=https://api.deepseek.com`.
A successful run writes `run_summary.json` (with `execution: local` and
`strong_isolation: false`), a Markdown report in `reports/`, the persistent
`research_tree.json`, and content-addressed evidence in `artifacts/objects/`.
A verified local run reached a final-test `f1_macro` of `0.8126` on the Titanic
validation, with the frozen final-test recorded under the unchanged SOTA commit.

## Development

Install uv, then create or update the project environment:

```bash
uv sync
```

运行 Python 全量测试：

```bash
uv run pytest -q tests test/unit
```

Update the dependency lock file after changing `pyproject.toml`:

```bash
uv lock
```

Build distributions only when a release artifact is needed:

```bash
uv build
```

The generated virtual environment and build artifacts are excluded from Git.

## Paper Markdown Tool

Athena includes a TeX-first paper ingestion pipeline for RAG. The upstream search stage
provides a TeX source package, a PDF, or both as artifacts. TeX is authoritative when
present; otherwise the tool falls back to PyMuPDF. Figures, tables, and image-only pages
are interpreted through an injected vision/base-model interface. Model-derived
interpretations become separate retrieval units; unavailable fallback evidence remains in
its body chunk to avoid duplicate indexing.

See [docs/paper_markdown_tool.md](docs/paper_markdown_tool.md) for the source contract,
ToolRegistry integration, persistence model, and quality behavior.
