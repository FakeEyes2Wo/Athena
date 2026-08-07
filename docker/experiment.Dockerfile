FROM ghcr.io/astral-sh/uv:0.9.7 AS uv
FROM python:3.11-slim

COPY --from=uv /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

RUN groupadd --gid 65532 athena \
    && useradd --uid 65532 --gid 65532 --no-create-home athena

ENV PATH="/app/.venv/bin:$PATH" \
    VIRTUAL_ENV="/app/.venv"

USER 65532:65532
ENTRYPOINT ["/app/.venv/bin/python"]
