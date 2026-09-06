# Athena

Athena 是一个自动研究工作台：通过对话描述任务，让智能体分析数据、提出假设、
运行实验并比较结果。推荐先用 Web GUI 体验，再查看研究树和实验报告。

## GUI-first 快速开始

### 前置条件

- Git
- Python 3.11 或更高版本
- [uv](https://docs.astral.sh/uv/)
- Node.js 20 或更高版本及 npm
- 一个可用的 LLM API key
- 本地 TESS 数据集；数据目录必须至少包含 `train.csv`

### 安装

```bash
git clone https://github.com/FakeEyes2Wo/Athena.git
cd Athena

# Python 依赖（使用仓库锁文件）
uv sync --frozen

# 本地配置；真实 key 只放在 .env，不要提交
cp .env.example .env
cp config.example.toml config.toml

```

Windows PowerShell 中，复制配置也可以使用以下命令（在仓库根目录执行）：

```powershell
Copy-Item .env.example .env
Copy-Item config.example.toml config.toml
```

`config.toml` 从仓库根目录读取，`.env` 中的环境变量优先于配置文件。至少配置一个
`DEEPSEEK_API_KEY`、`OPENAI_API_KEY` 或 `DASHSCOPE_API_KEY`；不要把密钥写进
`config.toml` 或提交到 Git。模板默认使用 DeepSeek；更换服务商时，请同步修改
`config.toml` 中的 `provider`、`base_url` 和 `model_name`，不能只换密钥。

### 启动 GUI

在仓库根目录打开终端，运行：

```bash
cd athena-gui
npm ci
npm run dev:web
```

然后打开 <http://localhost:1421>。该命令同时启动 Python WebSocket gateway；默认
gateway 地址是本机 `127.0.0.1:17601`，通常不需要手动设置。

### 运行一次 TESS SEARCH 演示

项目工作区和数据目录必须分开。工作区可以使用一个新的空目录，例如：

```text
<repository>/.athena/tess-gui-demo
```

数据集根目录应指向包含 `train.csv` 的目录，而不是文件本身，例如：

```text
<TESS_DATASET>/task/split
```

在 GUI 中：

1. 打开工作区选择器，选择或输入新的工作区目录，不要选择数据集目录。
2. 在“运行设置”中把“数据集根目录”设为上面的 `task/split` 目录。
3. 将“搜索上限”设为 `1`，并发设为 `1`。
4. 打开“跳过 VALIDATE”，点击“保存”；首次运行保持本地计算即可。
5. 在对话框输入 TESS 任务，明确使用 `<DATA_ROOT>/train.csv`，目标是
   `TESS_flare_win120min` 二分类，指标为 macro F1。
6. 完成任务澄清后确认并开始，观察 PREPARE 和一次 SEARCH 的实时事件。

例如任务描述可以写成：

```text
使用 <DATA_ROOT>/train.csv 完成 TESS_flare_win120min 二分类研究，优化
macro F1。只运行 1 次 SEARCH，使用 1 个并发 worker；本次演示跳过 VALIDATE/FINAL。
```

运行产生的状态、会话记录、artifacts 和实验工作区都写入所选项目目录下的
`.athena/` 与 `workspaces/`。请勿在已有生产研究目录上做首次演示，也不要点击
resume 继续旧任务。

出现 `Task understanding failed` 时先检查 API 配置与网络，再点击 Retry。
该阶段失败表示尚未开始训练，不代表数据集已完成验证。

SEARCH 上限不包含 PREPARE 的数据分析与基线工作，也不是 API 调用次数上限；
耗时与费用取决于模型服务和任务。需要中止时点击“停止”。

## 数据、结果与复现边界

数据集是外部输入，不随仓库提交。`task/split` 中的训练、SEARCH 和 FINAL 文件由
数据所有者管理；不要将标签或凭据提交到仓库。一次 GUI 演示只验证真实运行链路，不能
替代完整 TESS 复现，也不会自动产生可宣称的 FINAL 分数。

历史实验分数、数据划分和硬件环境不等于新 GUI 运行的结果。请在报告中区分当前运行的
SEARCH 结果、历史 Athena 结果和独立复现结果，并遵守 API 预算与数据使用限制。

可选的离线 TESS 推理/复现材料见
[`reproduction/tess/README.md`](reproduction/tess/README.md)；它不是 GUI 主流程，
也不包含数据集或密钥。架构与开发说明见 [`docs/README.md`](docs/README.md)，许可证
见 [`LICENSE`](LICENSE)，第三方依赖许可见 [`third_party_licenses.md`](third_party_licenses.md)。

## 开发验证

```bash
uv run pytest -q tests test/unit
cd athena-gui
npm test
```

`slow` 测试可能产生真实模型/API 请求，应在明确预算和凭据后单独运行。

对外提交使用本仓库 GitHub 链接，并附上 `git rev-parse HEAD` 的完整输出锁定版本；
不需要另做源码 ZIP。提交说明中还需补齐队伍联系人和数据获取方式。
