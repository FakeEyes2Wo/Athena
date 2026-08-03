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

## 模型配置

模型走 OpenAI 兼容协议。复制 `.env.example` 为 `.env` 后填写：

```ini
OPENAI_API_KEY=...
OPENAI_BASE_URL=...        # 留空走 OpenAI 官方地址；接百炼 / vLLM 等填对应地址
ATHENA_TUI_MODEL=...       # TUI 默认模型
```

`.env` 已被 `.gitignore` 忽略。`python -m athena.tui` 会先读当前目录的 `.env`，
再用项目根目录的 `.env` 兜底，所以从任意目录启动都能拿到配置。

配置好后自检一遍（会真的打一次接口，确认端点支持流式工具调用）：

```bash
uv run python -m athena.tui --check
```

## TUI

Athena 带一个终端界面，跑在 `app_server` 协议之上（`thread/start` → `turn/start` →
`thread/subscribe`），不直接 import 任何 agent。

```bash
uv sync --extra tui          # prompt_toolkit + rich
uv run python -m athena.tui  # 或者 uv run athena-tui
```

没有 API key 时用离线 runner 跑通完整链路：

```bash
uv run python -m athena.tui --mock
```

常用开关：`--model`、`--agent`、`--mode {ask,auto,deny}`、`--no-approval`、
`--verbose`、`--debug`（日志写到 `~/.athena/tui.log`，不会冲掉界面）。
进入后 `/help` 列出全部命令与键位。

设计说明见 [docs/tui_design_ch.md](docs/tui_design_ch.md)。

## Paper Markdown Tool

Athena includes a TeX-first paper ingestion pipeline for RAG. The upstream search stage
provides a TeX source package, a PDF, or both as artifacts. TeX is authoritative when
present; otherwise the tool falls back to PyMuPDF. Figures, tables, and image-only pages
are interpreted through an injected vision/base-model interface. Model-derived
interpretations become separate retrieval units; unavailable fallback evidence remains in
its body chunk to avoid duplicate indexing.

See [docs/paper_markdown_tool.md](docs/paper_markdown_tool.md) for the source contract,
ToolRegistry integration, persistence model, and quality behavior.
