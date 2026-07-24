# Athena

Athena is an automated AI4S system. The Python project and its dependencies are
managed with [uv](https://docs.astral.sh/uv/).

## Development

Install uv, then create or update the project environment:

```bash
uv sync
```

Run the test suite:

```bash
uv run pytest
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
