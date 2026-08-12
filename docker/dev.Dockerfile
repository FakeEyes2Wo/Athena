# Athena 开发环境镜像（对齐 codex_docs Shared Execution Runtime §Development Dockerfile）。
# 仅复现 Linux 开发环境；不参与 Agent 执行构建，也不含任何凭据。
FROM ghcr.io/astral-sh/uv:0.9.7 AS uv
FROM python:3.11-slim

COPY --from=uv /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# 默认组含 dev（pyproject [tool.uv] default-groups）→ pytest/black/pre-commit 一并安装。
RUN uv sync --frozen

RUN groupadd --gid 65532 athena \
    && useradd --uid 65532 --gid 65532 --no-create-home athena

# UTF-8 输出 + 无头 Matplotlib（agent 图生成依赖 MPLBACKEND=Agg）。
ENV PATH="/app/.venv/bin:$PATH" \
    VIRTUAL_ENV="/app/.venv" \
    PYTHONUTF8="1" \
    LANG="C.UTF-8" \
    LC_ALL="C.UTF-8" \
    MPLBACKEND="Agg"

USER 65532:65532
CMD ["/app/.venv/bin/python"]
