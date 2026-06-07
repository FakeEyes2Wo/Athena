# 后端执行 Harness 使用指南

版本：0.1  
日期：2026-06-07  
范围：指导 AI Scientist 后端如何调用代码执行/文件修改/实验验证能力。本文是设计与使用约定，暂不要求完成代码实现。

## 1. 目标与边界

### 1.1 目标

AI Scientist 系统需要一个后端执行 harness，用来承接智能体产生的工程任务，例如：

1. 读取和分析项目文件。
2. 生成或修改实验脚本。
3. 运行小规模数据验证。
4. 执行测试、格式化、静态检查。
5. 产出可复现实验日志、结果文件和报告片段。

本项目不应让业务智能体直接调用某个具体 CLI。推荐设计为：

```text
AI Scientist Orchestrator
  -> ExecutionHarness 统一协议
      -> QwenCodeHarness
      -> CodexHarness
      -> MinimalHarness
```

业务层只关心任务输入、执行事件、产物和退出状态；具体使用 Qwen Code、Codex 还是 minimal harness，由配置决定。

### 1.2 非目标

1. 不把 harness 做成通用云沙箱平台。
2. 不允许任意网络爬取、任意系统命令和无限时长任务。
3. 不把最终科学判断交给执行 harness；harness 只负责“执行、记录、返回证据”。
4. 当前阶段不实现完整代码，只固化接口、使用方式和演进路线。

## 2. 设计假设

1. Qwen Code/CLI、Codex、Minimal Harness 三种路径都可能存在，因此需要统一 adapter。
2. 赛题要求使用 Qwen/百炼作为核心模型能力；所以默认优先考虑 Qwen Code 作为后端执行代理，Codex 作为备选执行面或工程增强工具。
3. Qwen Code 和 Codex 都是面向软件工程任务的 agent/CLI，不应直接暴露给科研业务层。
4. Minimal Harness 是最小可控实现，重点是复现关键机制：任务状态机、工具注册、沙箱命令执行、文件补丁、事件日志、产物归档。
5. 如果从外部开源项目提取模块，必须先确认许可证、依赖边界和 attribution 要求。

## 3. 总体架构

```text
┌────────────────────────────────────────────────────────────┐
│ Scientific Workflow                                         │
│ Hypothesis / Experiment / Report Agents                     │
└────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌────────────────────────────────────────────────────────────┐
│ Execution Service                                           │
│ job queue / policy / adapter selection / artifact registry  │
└────────────────────────────────────────────────────────────┘
                          │
         ┌────────────────┼────────────────┐
         ▼                ▼                ▼
┌────────────────┐ ┌────────────────┐ ┌────────────────┐
│ QwenCodeHarness│ │ CodexHarness   │ │ MinimalHarness │
└────────────────┘ └────────────────┘ └────────────────┘
         │                │                │
         ▼                ▼                ▼
┌────────────────────────────────────────────────────────────┐
│ Workspace Sandbox                                           │
│ git worktree / temp dir / datasets / logs / artifacts       │
└────────────────────────────────────────────────────────────┘
```

## 4. 统一 ExecutionHarness 协议

### 4.1 Job 输入

```yaml
job_id: string
project_id: string
workspace: string
backend: qwen_code | codex | minimal | auto
intent: inspect | patch | run_experiment | test | summarize
prompt: string
context_files:
  - path: string
    reason: string
allowed_paths:
  - string
forbidden_paths:
  - string
commands_allowlist:
  - string
network_policy: disabled | allowlist | unrestricted
timeout_seconds: number
artifact_policy:
  collect:
    - string
  max_size_mb: number
approval_policy: never | on_request | manual_gate
metadata:
  hypothesis_id: string
  experiment_id: string
  model_hint: string
```

### 4.2 Event 输出

harness 必须流式记录结构化事件，便于前端展示和复现。

```yaml
event_id: string
job_id: string
ts: string
type: job_started | model_message | tool_call | command_started | command_finished | file_changed | artifact_created | error | job_finished
message: string
payload: object
```

### 4.3 Job 结果

```yaml
job_id: string
status: succeeded | failed | cancelled | timeout | needs_human
backend: qwen_code | codex | minimal
summary: string
changed_files:
  - path: string
    diff_path: string
commands:
  - command: string
    exit_code: number
    stdout_path: string
    stderr_path: string
artifacts:
  - path: string
    type: table | figure | report | log | model | other
evidence:
  - claim: string
    source: string
    artifact_path: string
risks:
  - string
next_actions:
  - string
```

### 4.4 成功标准

一次执行任务只有在满足以下条件时才算成功：

1. 退出状态为 `succeeded`。
2. 关键命令均有退出码记录。
3. 所有文件变更都有 diff。
4. 产物路径存在且已登记。
5. 若任务要求验证，必须包含测试或实验输出。
6. 若任务失败，必须返回失败原因和可重试建议。

## 5. 后端方案 A：Qwen Code/CLI

### 5.1 Qwen Code 功能概览

Qwen Code 是面向终端和工程仓库的开源 coding agent。对本项目而言，它不是普通模型调用接口，而是可以被后端 harness 包装的“工程执行代理”。根据当前官方仓库与文档，可概括为以下能力：

| 能力 | 概括 | 对本项目的价值 |
| --- | --- | --- |
| 交互式 CLI | 在终端中理解仓库、读取文件、规划修改、执行命令并反馈结果 | 适合开发期人工协作和调试 |
| Headless 自动化 | 通过 `qwen -p` 等非交互方式执行一次性任务 | 适合作为 `QwenCodeHarness` 的 MVP 接入方式 |
| SDK 集成 | 提供 Node.js/Python SDK，用程序方式驱动 agent | 适合后端服务获取更细粒度控制和事件流 |
| Server/daemon 模式 | 可用 `qwen serve` 暴露 HTTP API，当前应按实验能力处理 | 可作为后续服务化方案，但不建议演示唯一依赖 |
| 仓库理解 | 可结合文件搜索、读取、编辑、shell 命令等工具完成代码库任务 | 支撑实验脚本生成、数据处理脚本修改和测试闭环 |
| 工具调用 | 通过内置工具和外部工具执行文件、命令、搜索等操作 | 可映射到统一 `ExecutionHarness` 事件和权限策略 |
| MCP 扩展 | 可通过 MCP 接入外部工具和上下文服务 | 后续可接数据目录、文献服务或内部实验工具 |
| Skills | 可把可复用任务流程沉淀为技能 | 可沉淀数据画像、baseline 实验、报告导出等固定流程 |
| Subagents | 可为复杂任务拆分专门角色 | 可对应 Data Profiler、Experiment Runner、Result Summarizer 等执行角色 |
| 上下文与记忆 | 支持项目级指令、上下文压缩和任务记忆类机制 | 便于保留项目约定，但关键规则仍应由 harness 显式注入 |
| 权限与审批 | 支持命令/工具执行前的确认或策略控制 | 可与 `needs_human` 和 allowlist 策略对齐 |
| 沙箱与检查点 | 支持隔离执行、变更检查或回滚类工作流 | 有助于降低实验执行和文件修改风险 |
| IDE/CI 集成 | 文档提供 IDE、GitHub Actions 等集成路径 | 适合开发期增强，比赛演示不应强依赖 |
| 多模型/Provider | 可配置不同 provider 或认证方式 | 本项目应默认绑定 Qwen/百炼链路，避免偏离赛题主线 |

使用原则：

1. Qwen Code 的强项是“带工具的工程执行”，不是替代 Citation Auditor 或科学评审。
2. 业务层不直接依赖 Qwen Code 的 CLI 参数；所有参数由 `QwenCodeHarness` adapter 管理。
3. Qwen Code 输出必须被转换成统一事件、diff、artifact 和 evidence，而不是直接进入最终报告。
4. Skills、Subagents、MCP 等高级能力适合后续增强；MVP 先使用 headless CLI 跑通闭环。
5. 对比赛材料，Qwen Code 应被描述为 Qwen 生态下的执行代理，用来支撑可复现实验和工程闭环。

### 5.2 适用场景

优先用于：

1. 需要更贴近 Qwen 生态和赛题国产模型叙事的执行任务。
2. 需要 headless CLI 或 SDK 方式接入的后端执行。
3. 需要将“百炼/Qwen + 工程执行代理”作为完整链路展示的场景。

### 5.3 可用接入形态

根据 Qwen Code 官方资料，当前可考虑三种接入形态：

1. CLI headless：使用 `qwen -p` 执行非交互任务。
2. SDK：通过 Node.js 或 Python SDK 集成到服务端。
3. Server 模式：使用实验性的 `qwen serve`，通过 HTTP API 调用。

推荐顺序：

1. MVP：优先用 CLI headless，接入成本低。
2. Alpha：切到 SDK，获得更细粒度事件和控制。
3. Beta：评估 `qwen serve`，仅在稳定性满足演示要求时使用。

### 5.4 CLI 适配约定

QwenCodeHarness 负责：

1. 构造 prompt 文件。
2. 设置工作目录。
3. 注入允许路径、任务目标和输出格式要求。
4. 调用 `qwen -p`。
5. 捕获 stdout/stderr。
6. 解析结果摘要、diff 和产物。

示例命令形态：

```powershell
qwen -p "@prompts/job_001.md"
```

实际参数以当前安装版本为准，不能在业务层硬编码。

### 5.5 Prompt 模板

```markdown
你是 AI Scientist 后端执行代理。

任务目标：
{{intent}}

工作目录：
{{workspace}}

允许读取/修改路径：
{{allowed_paths}}

禁止访问路径：
{{forbidden_paths}}

允许命令：
{{commands_allowlist}}

必须遵守：
1. 只执行任务要求的最小变更。
2. 每个命令说明目的。
3. 产物写入 artifacts/{{job_id}}。
4. 最终按指定 JSON schema 输出执行摘要。
5. 如果需要超出权限，停止并返回 needs_human。

用户任务：
{{prompt}}
```

### 5.6 优点

1. 与 Qwen 生态一致，方便赛题叙事。
2. 官方资料显示支持 headless、SDK、server 等多种后端接入方式。
3. 可作为“国产开源大模型驱动的执行代理”展示。

### 5.7 风险

1. CLI 参数和 server 模式可能随版本变化。
2. Headless 输出未必天然结构化，需要适配层做解析和校验。
3. 若底层工具权限过宽，可能影响复现和安全。

### 5.8 使用建议

1. `QwenCodeHarness` 是默认候选，但不要让业务层直接调用 `qwen`。
2. 所有 CLI 参数放进 `harness.backends.qwen_code` 配置。
3. 每次执行记录 `qwen --version`。
4. 演示前固定版本，避免现场升级导致行为变化。

## 6. 后端方案 B：Codex

### 6.1 适用场景

适合用于：

1. 工程任务复杂、需要稳定代码修改和测试闭环的场景。
2. 需要 JSONL 事件流、非交互执行或 SDK 接入的场景。
3. Qwen Code 不稳定或缺少某些工程能力时的 fallback。

### 6.2 可用接入形态

Codex 官方文档中，CLI 支持：

1. 交互式本地 coding agent。
2. 非交互模式 `codex exec`。
3. `--json` 输出 JSONL 事件。
4. `--output-schema` 约束最终响应。
5. SDK 方式在程序中调用。

推荐顺序：

1. MVP：`codex exec --json`。
2. Alpha：加入 `--output-schema`，强制结构化结果。
3. Beta：评估 Codex SDK，以便更自然地集成到 Execution Service。

### 6.3 CLI 适配约定

CodexHarness 负责：

1. 生成任务 prompt。
2. 设定 cwd、sandbox、网络策略和超时。
3. 调用 `codex exec --json`。
4. 把 JSONL 事件转换成统一 `Event`。
5. 收集 diff、命令输出和 artifacts。

示例命令形态：

```powershell
codex exec --json "@prompts/job_001.md"
```

如需强制最终结构：

```powershell
codex exec --json --output-schema schemas/harness_result.schema.json "@prompts/job_001.md"
```

实际参数以当前 Codex CLI 版本为准。

### 6.4 优点

1. 非交互执行和 JSONL 事件流适合服务端集成。
2. 工程能力强，适合文件修改、测试、审查和调试。
3. 可作为高可靠 fallback，用来验证 Qwen Code 结果或生成对照。

### 6.5 风险

1. 赛题硬约束偏向 Qwen/百炼，Codex 不宜作为唯一执行叙事。
2. 权限、模型和网络策略需要与本项目安全策略显式对齐。
3. 若使用云端或账号能力，可能引入不可复现因素。

### 6.6 使用建议

1. Codex 作为 `fallback` 或 `engineering_strong` 后端。
2. 对比赛演示，主链路仍应展示 Qwen/百炼；Codex 可用于工程实现辅助或对照验证。
3. 任何 Codex 生成的科学报告内容，仍必须经过 Citation Auditor 和 Evidence Store。

## 7. 后端方案 C：Minimal Harness

### 7.1 定位

Minimal Harness 是从 Qwen Code/Codex 的核心思想中抽象出的最小执行层，不追求完整 coding agent 能力。

它只实现：

1. 任务状态机。
2. 模型调用。
3. 工具注册。
4. 沙箱命令执行。
5. 文件读写与 patch。
6. 事件日志。
7. artifact 收集。
8. 输出 schema 校验。

### 7.2 适用场景

1. 需要强可控、强可复现的比赛演示。
2. 只做固定类型实验，例如运行数据画像、baseline、统计检验。
3. CLI 后端行为不稳定，或不适合嵌入服务端。
4. 希望最小化外部依赖。

### 7.3 模块划分

```text
minimal_harness/
  runner          任务状态机
  model_client    百炼 Qwen API 调用
  tool_registry   工具注册与权限校验
  shell_tool      命令执行封装
  file_tool       文件读写与 patch
  artifact_store  产物归档
  event_log       JSONL 事件日志
  schema_guard    输入输出 schema 校验
  policy          路径、命令、网络、超时策略
```

### 7.4 工具集

Minimal Harness 初期只开放以下工具：

| 工具 | 用途 | 风险控制 |
| --- | --- | --- |
| `read_file` | 读取指定文件 | 只能读 allowed_paths |
| `list_files` | 枚举工作区 | 限定深度和根目录 |
| `apply_patch` | 修改文件 | 记录 diff，禁止越界 |
| `run_command` | 运行命令 | 命令 allowlist、超时、输出截断 |
| `write_artifact` | 写入产物 | 只能写 artifacts/job_id |
| `record_evidence` | 登记证据 | 必须绑定 artifact 或 source |

### 7.5 优点

1. 最可控，最容易解释。
2. 易于做安全限制和复现。
3. 可以把百炼 Qwen 作为唯一模型入口，完全贴合赛题。
4. 便于在论文/方案中展示核心机制。

### 7.6 风险

1. 需要自己实现 agent loop、工具调用和错误恢复。
2. 工程能力可能弱于成熟 CLI。
3. 初期只能覆盖固定任务类型，不适合复杂开放式代码修改。

### 7.7 使用建议

1. Minimal Harness 作为最终可控底座。
2. Qwen Code/Codex 作为增强后端和参考实现。
3. 如果时间紧，先用 Qwen Code/Codex 打通 MVP，再逐步沉淀 Minimal Harness。

## 8. 后端选择策略

### 8.1 默认策略

```yaml
harness:
  default_backend: qwen_code
  fallback_backend: codex
  deterministic_backend: minimal
  selection:
    inspect: qwen_code
    patch: codex
    run_experiment: minimal
    test: codex
    summarize: qwen_code
```

解释：

1. 科研叙事和 Qwen 生态优先走 Qwen Code。
2. 复杂工程修改和测试闭环可用 Codex。
3. 可复现实验执行优先走 Minimal Harness。

### 8.2 自动选择规则

| 任务 | 推荐后端 | 理由 |
| --- | --- | --- |
| 文献处理脚本生成 | Qwen Code | 与 Qwen 主链路一致 |
| 快速代码修复 | Codex | 工程闭环更强 |
| 固定 baseline 实验 | Minimal Harness | 可控、可复现 |
| 前端小改动 | Codex | 文件修改和测试方便 |
| 报告片段生成 | Qwen Code | 保持主模型一致 |
| 产物复核 | Minimal Harness | 不引入额外推理不确定性 |

### 8.3 手动覆盖

每个 job 可以显式指定：

```yaml
backend: codex
```

但必须记录覆盖原因：

```yaml
metadata:
  backend_override_reason: "需要复杂代码修改和测试闭环"
```

## 9. 安全与权限策略

### 9.1 路径限制

1. 默认只允许访问当前项目工作区。
2. 数据集目录只读。
3. artifacts 目录可写。
4. 禁止访问用户主目录、SSH key、云凭证、浏览器 profile、系统配置。

### 9.2 命令限制

默认允许：

1. `python`。
2. `pytest`。
3. `ruff` 或项目既有 lint 命令。
4. `npm test` 或项目既有前端测试命令。
5. `git diff`、`git status`。

默认禁止：

1. 删除工作区外文件。
2. 修改 git 历史。
3. 上传数据。
4. 安装不明来源二进制。
5. 无限循环或长时间训练任务。

### 9.3 网络策略

推荐：

1. 文献检索 Agent 使用网络。
2. Execution Harness 默认不使用网络。
3. 如需下载公开数据，必须通过 allowlist。
4. 所有下载记录 URL、时间、hash 和许可证说明。

### 9.4 人工门禁

以下情况必须返回 `needs_human`：

1. 需要访问 forbidden path。
2. 需要运行不在 allowlist 的命令。
3. 需要联网下载大文件。
4. 需要安装系统级依赖。
5. 预计执行时间超过 job timeout。
6. 输出结果会被作为科学结论提交，但缺少证据来源。

## 10. 事件与产物规范

### 10.1 目录结构

```text
runs/
  {{project_id}}/
    {{job_id}}/
      job.yaml
      events.jsonl
      stdout.log
      stderr.log
      result.yaml
      diff.patch
      artifacts/
        figures/
        tables/
        reports/
```

### 10.2 Artifact 命名

```text
artifacts/{{job_id}}/{{type}}/{{short_name}}.{{ext}}
```

示例：

```text
artifacts/job_001/figures/light_curve_baseline.png
artifacts/job_001/tables/profile_summary.csv
artifacts/job_001/reports/feasibility_result.md
```

### 10.3 Evidence 记录

所有会进入最终 `Results` 字段的内容，都必须登记 evidence：

```yaml
claim: "baseline 在样例数据上达到 F1=0.71"
source: "job_001/artifacts/tables/metrics.csv"
artifact_path: "runs/demo/job_001/artifacts/tables/metrics.csv"
method: "python scripts/run_baseline.py --seed 42"
```

## 11. 与 AI Scientist 主框架的关系

Execution Harness 只服务于以下模块：

1. Data Profiler：生成数据画像、质量报告。
2. Experiment Designer：验证实验脚本是否可运行。
3. Feasibility Executor：跑 baseline、统计检验、模拟实验。
4. Report Composer：读取已登记 evidence，生成 Results 草案。

Execution Harness 不直接决定：

1. 假设是否科学成立。
2. 引用是否真实。
3. 研究是否有创新性。
4. 作品是否可提交。

这些判断仍由 Hypothesis Generator、Skeptic Reviewer、Citation Auditor 和人在回路共同完成。

## 12. 推荐演进路线

### 12.1 第一阶段：CLI MVP

目标：最快跑通。

1. 实现 `ExecutionHarness` 抽象。
2. 接入 `QwenCodeHarness` 的 headless CLI。
3. 记录 stdout/stderr、diff、artifacts。
4. 对失败任务返回 `failed` 或 `needs_human`。

验收：

1. 能让 Qwen Code 在样例项目中生成一个数据画像脚本。
2. 能运行脚本并保存结果。
3. 能把结果登记到 `runs/project/job/result.yaml`。

### 12.2 第二阶段：Codex Fallback

目标：提高工程任务稳定性。

1. 接入 `CodexHarness`。
2. 支持 JSONL 事件转换。
3. 支持 output schema。
4. 支持 backend 手动覆盖。

验收：

1. 同一个 job 可切换 Qwen Code 和 Codex。
2. 两个后端产出的事件都能在前端统一展示。
3. 失败时 fallback 策略可用。

### 12.3 第三阶段：Minimal Harness

目标：沉淀可控执行底座。

1. 实现最小 agent loop。
2. 接入百炼 Qwen API。
3. 实现工具 allowlist。
4. 实现 artifact store 和 evidence registry。

验收：

1. 固定 baseline 实验完全不依赖外部 coding CLI。
2. 同一任务重复执行结果稳定。
3. 能清楚说明安全边界和复现路径。

### 12.4 第四阶段：比赛演示整合

目标：把 harness 能力放入 AI Scientist 演示。

1. 前端展示执行事件流。
2. 报告引用 harness 产物作为 `Results`。
3. 演示 Qwen 主链路和 fallback 后端。
4. 固定版本、配置、截图和运行日志。

验收：

1. 10 分钟演示中能展示一次完整执行闭环。
2. 评审能看到真实命令、真实产物和真实 evidence。
3. 文档能解释为什么使用多后端而不破坏 Qwen 主线。

## 13. 配置草案

```yaml
harness:
  default_backend: qwen_code
  fallback_backend: codex
  run_root: runs
  artifact_root: artifacts
  timeout_seconds: 600
  network_policy: disabled

  backends:
    qwen_code:
      enabled: true
      command: qwen
      mode: headless
      version_command: qwen --version
      prompt_arg: -p

    codex:
      enabled: true
      command: codex
      mode: exec_json
      version_command: codex --version
      base_args:
        - exec
        - --json

    minimal:
      enabled: false
      model_provider: bailian
      model: qwen-plus
      tool_policy: strict

  policy:
    allowed_paths:
      - .
      - data
      - scripts
      - runs
      - artifacts
    forbidden_paths:
      - ~/.ssh
      - ~/.config
      - ~/.codex
      - ~/.qwen
    commands_allowlist:
      - python
      - pytest
      - git status
      - git diff
```

## 14. 文档与提交材料中的表述

推荐在技术方案中这样表述：

> 本系统将后端执行能力抽象为 Execution Harness。默认链路使用 Qwen Code/CLI 承接工程执行任务，以保持 Qwen 生态一致性；同时提供 Codex 作为工程增强与 fallback；对于需要强复现的小规模实验，沉淀 Minimal Harness，直接使用百炼 Qwen API、受限工具集和事件日志完成可控执行。

需要避免的表述：

1. “所有科研结论由 CLI 自动生成。”
2. “Codex 是核心模型能力。”
3. “Minimal Harness 复制了某项目源码。”
4. “执行结果无需人工或引用审计。”

更稳妥的表述：

1. “CLI/SDK 是执行后端，不是科学评审者。”
2. “Qwen/百炼保持主模型链路。”
3. “Codex 是工程执行 fallback。”
4. “Minimal Harness 复现执行框架的必要机制，并遵守许可证边界。”

## 15. 待确认问题

1. 最终是否允许在比赛材料中展示 Codex 作为 fallback，还是只在内部开发使用。
2. Qwen Code/CLI 的目标版本、安装方式和百炼认证方式。
3. 是否需要把 `qwen serve` 纳入演示；若仍属实验能力，建议不作为唯一依赖。
4. Minimal Harness 是 Python 实现还是 TypeScript 实现。
5. 是否需要支持远程沙箱，或只在本地/服务器工作区运行。
6. 哪些实验命令进入 allowlist。
7. artifacts 是否需要上传对象存储。

## 16. 参考资料

1. Qwen Code 官方仓库：<https://github.com/QwenLM/qwen-code>
2. Qwen Code 文档：<https://qwenlm.github.io/qwen-code-docs/>
3. Qwen Code 架构文档：<https://qwenlm.github.io/qwen-code-docs/en/developers/architecture/>
4. Qwen Code automation/headless 文档：<https://qwenlm.github.io/qwen-code-docs/en/cli/automation/>
5. Qwen Code SDK 文档：<https://qwenlm.github.io/qwen-code-docs/en/sdk/>
6. Qwen Code server 文档：<https://qwenlm.github.io/qwen-code-docs/en/cli/server/>
7. Qwen Code Subagents 文档：<https://qwenlm.github.io/qwen-code-docs/en/users/features/sub-agents/>
8. Qwen Code Skills 文档：<https://qwenlm.github.io/qwen-code-docs/en/users/features/skills/>
9. Qwen Code Memory 文档：<https://qwenlm.github.io/qwen-code-docs/en/users/features/memory/>
10. Qwen Code Approval Mode 文档：<https://qwenlm.github.io/qwen-code-docs/en/users/features/approval-mode/>
11. Qwen Code MCP 文档：<https://qwenlm.github.io/qwen-code-docs/en/users/features/mcp/>
12. Qwen Code Sandbox 文档：<https://qwenlm.github.io/qwen-code-docs/en/users/features/sandbox/>
13. Qwen Code Checkpointing 文档：<https://qwenlm.github.io/qwen-code-docs/en/users/features/checkpointing/>
14. Qwen Code Hooks 文档：<https://qwenlm.github.io/qwen-code-docs/en/users/features/hooks/>
15. Qwen Code VS Code 文档：<https://qwenlm.github.io/qwen-code-docs/en/users/vscode/>
16. Qwen Code GitHub Actions 文档：<https://qwenlm.github.io/qwen-code-docs/en/users/github-actions/>
17. Qwen Code Model Providers 文档：<https://qwenlm.github.io/qwen-code-docs/en/users/model-providers/>
18. Codex CLI 文档：<https://developers.openai.com/codex/cli>
19. Codex non-interactive 文档：<https://developers.openai.com/codex/noninteractive>
20. Codex SDK 文档：<https://developers.openai.com/codex/sdk>
