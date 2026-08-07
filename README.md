# Athena

Athena 是面向 AI4ML/AI4S 的自动研究系统。当前 Python 后端覆盖数据准备、假设生成、
隔离实验、评价比较、验证报告以及 Thread/Turn 运行时。

工程目录、模块 owner、当前能力与目标架构统一收录于
[`docs/README.md`](docs/README.md)。

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
