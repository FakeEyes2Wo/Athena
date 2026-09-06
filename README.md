# Athena

面向 AI4ML/AI4S 的自动研究系统。Supervisor 驱动 `PREPARE -> SEARCH -> VALIDATE`，
`ResearchRuntime`（`src/athena/research/`）是唯一组合根，TUI / CLI / headless 共享。
模块结构与目标架构见 [`docs/README.md`](docs/README.md)。

## TESS 开源与复现交付

TESS 的无标签离线推理、任务声明和交付缺口见
[`reproduction/tess/README.md`](reproduction/tess/README.md)。它与 Athena
研究运行入口分离，不依赖 GUI 或 API，不套用 JW-FD 的任务和输出格式。
已验证新导出模型的全部 SEARCH 预测与历史 SOTA 按 ID 一致；未重跑 FINAL。
本地交付包与不可变源码标识见复现目录的 `RELEASE.md`。
许可证见根目录 `LICENSE`；依赖许可范围见
[`third_party_licenses.md`](third_party_licenses.md)。

## 环境

前置：Python ≥3.11 + [uv](https://docs.astral.sh/uv/)。

```bash
uv sync                              # 安装锁定依赖
cp .env.example .env                 # 填 DEEPSEEK_API_KEY（或 OPENAI_API_KEY）
uv run pytest -q tests test/unit     # 验证；slow 用 `-m slow`
```

`.env` 被 Git 忽略，`.env.example` 为模板。

## 运行

```bash
# TUI（推荐）：第一条消息即研究任务，自动跑全流程
uv run Athena-tui --project .athena/tui-run

# CLI
uv run Athena-cli run --project .athena/titanic-run \
  --data examples/titanic/train.csv --task "预测泰坦尼克号乘客是否存活" --mode auto
uv run Athena-cli status|pause|resume|stop --project .athena/titanic-run

# headless（跑完即退）：真实 LLM 全流程 PREPARE→SEARCH→VALIDATE
# `--data` 须为绝对路径——数据集在 workspace 之外，PREPARE Agent 用该绝对路径读取
# `--search-limit` 默认 10，加速验证可调小（如 2）
uv run python scripts/run_headless.py \
  --project .athena/titanic-run \
  --task "Predict Titanic passenger survival (target = Survived: 0 died, 1 survived). Build a baseline model and improve it." \
  --data "$(pwd)/examples/titanic"

```

## TUI 引导

- **布局**：顶栏（phase·status·进度·SOTA 指标）/ 中部可滚动历史 / 底部输入框。
- **键**：`Enter` 发送 · `Shift+Enter`/`Ctrl+J` 换行 · `PageUp`/`PageDown` 滚动 ·
  `End` 最新 · `?` 帮助 · `Esc` 关闭 · `Ctrl+C` 退出。
- **斜杠命令**：`/pause` `/resume` `/stop` `/quit`；确认时 `Y`/`N`/`Esc`。
- **流程**：PREPARE（EDA + 可信 baseline）→ SEARCH（supervisor 纯调度，启动 ideator
  绑定 EDA 目录探索提出假设，独立 worktree 实验比 SOTA）→ VALIDATE（`--mode auto` 时复现验证）。

## 测试 / 构建

```bash
uv run pytest -q tests test/unit     # 默认排除 slow
uv run pytest -q -m slow             # 真实 LLM 凭据
uv lock                              # 改 pyproject 后更新锁
uv build
```

## Paper Markdown Tool

TeX-first 论文摄取流水线（RAG）。TeX 优先、回退 PyMuPDF；图表经 vision 接口解释。
详见 [docs/paper_markdown_tool.md](docs/paper_markdown_tool.md)。
