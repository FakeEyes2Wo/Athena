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
