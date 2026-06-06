# Li-Shou AI Scientist 主框架设计

版本：0.1  
日期：2026-06-06  
范围：赛题理解、外部调研、系统主框架、交付路线。本文暂不包含代码实现。

## 1. 设计前提与结论

### 1.1 明确假设

1. 本仓库中的 `XH-202619_基于国产开源大模型的AI Scientist的研发与应用.pdf` 是当前唯一权威赛题材料。
2. 团队尚未指定最终学科方向，因此主框架采用“领域可插拔”设计：同一套智能体流水线可以接入自然科学或人文社科数据。
3. 赛题要求“基于国产开源大模型 Qwen 系列”和“通过阿里云百炼平台调用模型 API 并提供凭证/截图”。因此设计默认以百炼 API 为主模型入口，以 Qwen-Agent 或自研轻量编排作为多智能体实现载体。
4. 当前阶段目标是完整主框架设计，不做代码实现；但框架需要能自然落到后续源码、前端、演示视频和 20 页内技术方案文档。

### 1.2 总体结论

建议将作品定位为：

> 面向特定学科问题的多智能体 AI Scientist 系统，从文献和数据输入开始，经过事实抽取、知识缺口识别、假设生成、多智能体质询、人在回路修订和小规模可行性验证，输出结构化的《科学假设与研究计划》。

主框架不应只是“聊天问答 + 报告生成”。比赛得分点集中在：

1. 可验证科学假设是否有新意、自洽、可落地。
2. 多智能体协作是否真实存在且可展示。
3. 文献和数据是否真实、引用是否可核验。
4. 是否能用真实案例支撑实际场景，并保证结果可复现。

### 1.3 推荐示例方向

如果团队没有既定学科方向，推荐首个案例选择：

> 天文/空间科学中的异常天体或瞬变源机制假设生成。

理由：

1. 发榜方包含中国科学院国家天文台人工智能推进委员会，学科贴合度高。
2. NASA ADS、HEASARC、ESA Gaia 等公开资源成熟，容易证明文献和数据真实合规。
3. 天文数据天然多模态，包含文献、表格、时序、光谱、图像，能支撑“多模态科学数据处理”评分项。
4. 异常现象识别、内在机制挖掘、极端事件预测都与题面自然科学方向表述高度一致。

该方向只是推荐样例。若团队已有优势学科，可以替换领域适配层，保留主流水线。

## 2. 赛题要求拆解

### 2.1 核心任务

题面要求围绕特定学科领域，基于超级智能体或多智能体系统，使用 Qwen 系列模型开发 AI 系统原型，实现：

1. 问题理解。
2. 知识整合。
3. 关联发现。
4. 可验证科学假设生成。
5. 从“数据/文献输入”到“可验证科学假设输出”的智能闭环。

设计对应：

| 赛题能力 | 系统模块 |
| --- | --- |
| 问题理解 | Problem Framer |
| 文献挖掘与事实提取 | Literature Miner, Fact Extractor |
| 知识整合 | Evidence Store, Knowledge Graph Builder |
| 关联发现 | Gap Detector, Cross-domain Analogist |
| 假设生成 | Hypothesis Generator |
| 论证可行与多轮迭代 | Skeptic Reviewer, Experiment Designer, Feasibility Executor |
| 智能体思辨与人在回路 | Debate Orchestrator, Human Checkpoint |
| 结果规范化 | Report Composer, Citation Auditor |

### 2.2 结果字段

系统最终必须输出《科学假设与研究计划》，包含：

1. Problem Statement：当前领域具体局限性。
2. Rationale：创新点和推导链条。
3. Technical Details：验证假设所需技术手段。
4. Datasets：真实合规数据集。
5. Source：假设推演依据的历史数据。
6. Target：验证实验所需拟采集数据特征。
7. Paper Title：学术标题。
8. Paper Abstract：背景、方法、预期结果。
9. Methods：实施步骤、模型架构或实验流程。
10. Experiments：基线对比和评估指标。
11. Results：公式推导或实际执行后的可行性验证结果。
12. References：真实文献列表，严禁虚构。

设计要求：上述字段不能由最后一个模型一次性自由发挥生成，而应由前序证据、实验、审计模块逐步填充。

### 2.3 评分导向

| 评分项 | 分值 | 设计抓手 |
| --- | ---: | --- |
| 科学价值 | 40 | 新颖性检索、证据链、可证伪预测、可落地实验 |
| 技术深度 | 30 | 多智能体日志、RAG、知识图谱、多模态处理、工具调用、代码执行 |
| 应用潜力 | 30 | 真实场景案例、成果转化叙事、源码和结果可复现、前端与视频 |

## 3. 外部调研摘要

### 3.1 百炼与 Qwen

阿里云百炼官方文档显示，百炼集成千问模型，提供千问官方 API 和 OpenAI 兼容 API，覆盖文本、图像、音视频等多模态场景。文档也说明百炼智能体可连接知识库、插件和工具，知识库使用 RAG 技术整合外部信息源，MCP 可作为大模型与外部工具之间的信息通道。

设计启发：

1. 模型入口走百炼，便于满足比赛“平台调用凭证/截图”的硬要求。
2. 文献知识和领域资料可放入百炼知识库，也可自建向量库；两者并行时需要记录检索日志。
3. 工具调用可通过 Qwen-Agent function calling、百炼插件或 MCP 接入。

### 3.2 Qwen-Agent

Qwen-Agent 是 QwenLM 官方仓库，定位为基于 Qwen 的 LLM 应用开发框架，包含工具使用、规划、记忆、RAG、代码解释器、MCP 等能力。

设计启发：

1. 适合作为多智能体原型的工程底座。
2. 每个智能体可以封装为带系统提示词、工具集、输入输出 schema 的 Agent。
3. 代码解释器能力适合做小规模统计验证和图表生成，但生产级执行需要隔离沙箱。

### 3.3 AI Scientist 相关工作

The AI Scientist v1 展示了从想法生成、代码执行、实验、可视化、论文撰写到模拟评审的端到端流程。AI Scientist-v2 进一步使用 agentic tree search 和实验管理智能体，降低对人工代码模板的依赖。

设计启发：

1. 本赛题不要求完整自动写论文和投稿，更重视“可验证科学假设与研究计划”。
2. 可以借鉴“假设生成 - 实验执行 - 审稿反馈 - 迭代”的闭环。
3. 与 v1/v2 相比，本作品应额外强调真实文献、引用审计、人在回路和领域数据合规。

### 3.4 科学文献 RAG 与多智能体发现

PaperQA 展示了面向科学文献的 RAG agent：检索全文、评估来源和段落相关性，并带出处回答。Sibyl 作为 2026 年 AI4Science Workshop 方案，强调从文献生成可证伪预测、人类检查点、来源审计和时间回测。

设计启发：

1. 科学假设生成必须有 provenance，即每个关键判断能追溯到文献或数据。
2. 应设置强制人类检查点，而不是完全自动放行。
3. 可以设计“时间回测”作为亮点：只用某一时间点前的文献生成预测，再用之后的文献验证命中情况。

## 4. 系统总览

### 4.1 分层架构

```text
┌────────────────────────────────────────────────────────────┐
│ 前端交互层                                                   │
│ 选题配置 / 数据上传 / 假设看板 / 智能体辩论 / 报告导出       │
└────────────────────────────────────────────────────────────┘
                          │
┌────────────────────────────────────────────────────────────┐
│ 应用编排层                                                   │
│ Project Orchestrator / Workflow DAG / Agent Runtime         │
└────────────────────────────────────────────────────────────┘
                          │
┌────────────────────────────────────────────────────────────┐
│ 多智能体层                                                   │
│ 问题定义 / 文献挖掘 / 数据画像 / 知识建图 / 缺口发现 /       │
│ 假设生成 / 怀疑者评审 / 实验设计 / 可行性执行 / 引用审计     │
└────────────────────────────────────────────────────────────┘
                          │
┌────────────────────────────────────────────────────────────┐
│ 工具与知识层                                                 │
│ 文献 API / 数据 API / PDF 解析 / 向量检索 / 图数据库 /       │
│ 代码沙箱 / 统计工具 / 多模态模型 / 日志与审计                │
└────────────────────────────────────────────────────────────┘
                          │
┌────────────────────────────────────────────────────────────┐
│ 模型与基础设施层                                             │
│ 阿里云百炼 Qwen API / Qwen-Agent / 对象存储 / 任务队列       │
└────────────────────────────────────────────────────────────┘
```

### 4.2 主数据流

```text
研究问题 + 领域配置
  ↓
文献检索 + 数据集接入
  ↓
事实抽取 + 数据画像
  ↓
证据库 + 知识图谱
  ↓
知识缺口与矛盾点识别
  ↓
候选假设生成
  ↓
多智能体辩论与引用审计
  ↓
人在回路选择与修订
  ↓
小规模可行性验证
  ↓
结构化研究计划输出
```

### 4.3 推荐技术栈

| 层级 | 推荐选择 | 说明 |
| --- | --- | --- |
| 模型服务 | 阿里云百炼 Qwen API | 满足赛题硬要求，模型版本以百炼控制台可用项为准 |
| Agent 框架 | Qwen-Agent 或自研轻量 DAG | Qwen-Agent 更贴近 Qwen 生态；自研 DAG 更易做审计和可控流程 |
| 后端 | Python + FastAPI | 科学计算、文献处理和 Agent 生态更方便 |
| 任务队列 | Celery/RQ 或轻量 asyncio worker | 文献抓取、PDF 解析、实验执行需要异步化 |
| 向量库 | 百炼知识库 / Milvus / pgvector | MVP 可先用 pgvector 或百炼知识库 |
| 图谱 | NetworkX / Neo4j | MVP 可先用 NetworkX，展示时可导出图谱 |
| 数据处理 | pandas, numpy, scipy, scikit-learn | 支撑表格、时序和基线模型 |
| 多模态 | Qwen-VL 类模型 + 专用解析工具 | 图像、图表、光谱、PDF 图表理解 |
| 前端 | React / Vue + 可视化组件 | 假设看板、证据链、Agent 日志、报告预览 |
| 报告导出 | Markdown -> PDF | 便于生成赛题技术方案和案例报告 |

## 5. 核心智能体设计

### 5.1 Project Orchestrator

职责：

1. 管理一次研究任务的状态机。
2. 控制各智能体执行顺序和重试策略。
3. 维护证据 ID、假设 ID、实验 ID、审计状态。
4. 决定何时进入人工确认节点。

输入：

1. 领域配置。
2. 初始研究问题。
3. 文献、数据和约束。

输出：

1. 结构化项目状态。
2. 下一步任务列表。
3. 最终报告草案。

### 5.2 Problem Framer

职责：

1. 将用户的宽泛选题压缩成具体科学问题。
2. 明确对象、变量、现象、目标、边界条件。
3. 生成检索关键词和数据需求。

输出 schema：

```yaml
problem_id: string
domain: string
research_object: string
phenomenon: string
known_limitations: [string]
key_variables: [string]
search_queries: [string]
dataset_requirements: [string]
success_criteria: [string]
```

### 5.3 Literature Miner

职责：

1. 检索论文、综述、数据说明和方法论文。
2. 去重并获取元数据、摘要、DOI/arXiv ID/ADS bibcode。
3. 标记文献类型：背景、证据、反例、方法、数据来源。

推荐工具：

1. arXiv API。
2. Crossref。
3. Semantic Scholar。
4. NASA ADS，若选择天文方向。
5. 领域数据库文献接口。

关键约束：

1. 不把未核验网页当作正式参考论文。
2. 每条文献都要有稳定标识符。
3. 文献检索结果与后续引用必须同源可追溯。

### 5.4 Fact Extractor

职责：

1. 从论文中抽取事实、实验结论、局限性、数据条件。
2. 将事实拆成可审计原子断言。
3. 给每个事实绑定来源、页码或段落、可信等级。

事实 schema：

```yaml
fact_id: string
claim: string
entity: [string]
relation: string
evidence_text: string
source_id: string
locator: string
confidence: low | medium | high
stance: supports | contradicts | background
```

设计要点：

1. 原子事实粒度要小，避免把论文摘要整体塞进上下文。
2. 所有事实进入 Evidence Store 前必须有来源。
3. 模型生成的解释不能直接作为事实，只能作为候选推理。

### 5.5 Data Profiler

职责：

1. 识别数据类型：表格、时序、图像、光谱、文本、行为记录等。
2. 生成字段说明、缺失率、异常值、采样偏差和合规说明。
3. 为假设生成提供可观测变量与可检验指标。

输出：

```yaml
dataset_id: string
source_url: string
license_or_access_note: string
modalities: [table | time_series | image | spectrum | text | graph]
fields: [{name: string, type: string, meaning: string}]
quality_report: [string]
usable_variables: [string]
limitations: [string]
```

### 5.6 Knowledge Graph Builder

职责：

1. 将事实组织成实体、关系和证据图。
2. 支持查询“哪些证据支持某变量影响某现象”。
3. 支持可视化展示机制链。

图谱节点：

1. Paper。
2. Dataset。
3. Entity。
4. Variable。
5. Method。
6. Observation。
7. Hypothesis。
8. Experiment。

图谱边：

1. supports。
2. contradicts。
3. uses_method。
4. observed_in。
5. predicts。
6. requires_data。
7. analogous_to。

MVP 可用 NetworkX 存储和导出 JSON；后续再接 Neo4j。

### 5.7 Gap Detector

职责：

1. 识别文献中的未解释现象。
2. 找出不同论文结论冲突。
3. 发现数据覆盖空白。
4. 识别跨学科方法迁移机会。

输出：

```yaml
gap_id: string
description: string
gap_type: unexplained_observation | contradiction | missing_data | method_transfer
supporting_facts: [fact_id]
candidate_variables: [string]
why_it_matters: string
```

### 5.8 Hypothesis Generator

职责：

1. 基于事实、缺口和数据变量生成候选假设。
2. 使用三种推理模式：归纳、演绎、类比。
3. 每个假设都必须包含可证伪预测。

假设 schema：

```yaml
hypothesis_id: string
statement: string
mechanism: string
reasoning_chain: [string]
supporting_facts: [fact_id]
contradicting_facts: [fact_id]
testable_predictions: [string]
falsification_conditions: [string]
required_datasets: [dataset_id]
novelty_rationale: string
```

质量门槛：

1. 没有证据链的假设不得进入候选池。
2. 没有可证伪条件的假设不得进入报告。
3. 与已有论文结论完全重复的假设必须标记为低新颖性。

### 5.9 Skeptic Reviewer

职责：

1. 扮演反方评审，寻找逻辑漏洞、替代解释和不可验证点。
2. 检查假设是否只是相关性描述而非机制假设。
3. 要求 Hypothesis Generator 修订或淘汰弱假设。

审查维度：

1. 新颖性。
2. 自洽性。
3. 可证伪性。
4. 数据可得性。
5. 方法可行性。
6. 潜在混杂因素。
7. 引用真实性。

### 5.10 Cross-domain Analogist

职责：

1. 从其他学科查找可迁移技术或机制类比。
2. 将迁移建议约束在“可验证、可解释、可落地”的范围内。
3. 避免只做表面类比。

示例：

1. 天文光变曲线异常检测可借鉴医学心电时序异常检测。
2. 材料性能优化可借鉴贝叶斯优化和主动学习。
3. 社会风险预警可借鉴网络科学中的传播模型。

### 5.11 Experiment Designer

职责：

1. 为候选假设生成最小可行实验。
2. 定义 baseline、metrics、数据切分、消融实验。
3. 输出可复现实验计划。

实验 schema：

```yaml
experiment_id: string
hypothesis_id: string
objective: string
datasets:
  source: [dataset_id]
  target: [string]
methods: [string]
baselines: [string]
metrics: [string]
expected_results: [string]
reproducibility_notes: [string]
```

### 5.12 Feasibility Executor

职责：

1. 执行小规模统计分析、公式推导、可视化或模拟。
2. 不要求完成完整科研验证，但要证明验证路径可走通。
3. 生成 Results 字段所需的初步证据。

可执行内容：

1. 数据字段统计。
2. 简单 baseline。
3. 相关性或差异检验。
4. 时序异常检测初跑。
5. 合成数据模拟。
6. 公式推导检查。

安全要求：

1. 代码执行应使用隔离环境。
2. 记录执行脚本、参数、随机种子和输出。
3. 失败结果也要保留，避免只展示成功样本。

### 5.13 Citation Auditor

职责：

1. 检查 References 中每条文献是否真实存在。
2. 检查引用是否支持对应断言。
3. 标记弱引用、错引和无法核验引用。

审计规则：

1. 每条参考文献必须有 DOI、arXiv ID、ADS bibcode 或稳定 URL。
2. 每个关键事实至少绑定一个来源。
3. 模型不得凭空生成作者、标题、年份。
4. 引用列表由文献库记录生成，而不是由报告生成模型自由编写。

### 5.14 Report Composer

职责：

1. 按赛题字段组合最终报告。
2. 保留证据链和实验摘要。
3. 生成 PDF/Markdown/前端预览。

输出原则：

1. 主报告面向专家评审，语言学术、简洁。
2. 附录保留 Agent 过程、证据 ID、检索记录和实验记录。
3. 对不确定性进行显式标注。

## 6. 人在回路设计

系统至少设置两个强制人工检查点。

### 6.1 检查点 A：问题与语料确认

位置：Problem Framer 和 Literature Miner 之后。

用户需要确认：

1. 研究问题是否足够具体。
2. 文献范围是否偏离主题。
3. 数据来源是否合规可用。
4. 是否补充专家指定论文或数据。

### 6.2 检查点 B：候选假设选择

位置：Skeptic Reviewer 第一轮审查之后。

用户需要确认：

1. 保留哪些假设。
2. 合并哪些假设。
3. 淘汰哪些泛泛而谈或不可信假设。
4. 是否要求系统重跑某一推理路径。

### 6.3 可选检查点 C：报告提交前审阅

位置：Report Composer 之后。

用户需要确认：

1. 引用是否符合团队学科判断。
2. 研究计划是否有明显伦理或合规风险。
3. 演示案例是否足以代表系统能力。

## 7. 领域适配层

### 7.1 通用领域配置

每个领域用一个配置文件描述：

```yaml
domain_name: string
research_questions: [string]
literature_sources: [arxiv, crossref, semantic_scholar, ads]
dataset_sources: [string]
modalities: [string]
domain_entities: [string]
domain_methods: [string]
preferred_metrics: [string]
forbidden_data: [string]
ethics_notes: [string]
```

### 7.2 天文方向样例配置

```yaml
domain_name: astronomy_transient_anomaly
research_questions:
  - 罕见 X 射线双星光变异常是否对应新的吸积状态转变机制
  - Gaia 光变源中是否存在可由多波段特征预测的异常类别
literature_sources:
  - NASA ADS
  - arXiv
  - Crossref
dataset_sources:
  - NASA HEASARC
  - ESA Gaia Archive
  - SIMBAD/VizieR
modalities:
  - literature
  - table
  - time_series
  - spectrum
  - image
domain_entities:
  - source
  - band
  - flux
  - period
  - spectrum
  - outburst
preferred_metrics:
  - ROC-AUC
  - F1
  - precision@k
  - false discovery rate
  - temporal backtest hit rate
```

## 8. 前端与演示设计

前端不是题面硬性要求，但题面鼓励演示，且前端能显著增强答辩表达。

### 8.1 首屏工作台

功能：

1. 创建研究项目。
2. 输入研究问题。
3. 选择领域配置。
4. 上传论文、数据或填写公开数据链接。
5. 启动流水线。

### 8.2 证据链视图

功能：

1. 展示事实卡片。
2. 展示事实来源。
3. 展示支持和反驳关系。
4. 点击跳转到原文段落或元数据。

### 8.3 假设看板

每个假设卡片展示：

1. 假设陈述。
2. 机制解释。
3. 新颖性评分。
4. 可验证性评分。
5. 支持证据数。
6. 反驳证据数。
7. 人工操作：保留、合并、重写、淘汰。

### 8.4 智能体辩论视图

功能：

1. 展示 Scientist、Skeptic、Methodologist、Citation Auditor 的轮次发言。
2. 高亮每轮导致的假设修改。
3. 显示被审计失败的引用和原因。

### 8.5 报告导出视图

功能：

1. 预览赛题字段。
2. 导出 Markdown/PDF。
3. 下载证据附录和实验日志。

## 9. 可信度与安全机制

### 9.1 反幻觉机制

1. 引用列表只能从 Literature Miner 的文献库中选择。
2. 报告生成时禁止创建新引用。
3. 关键事实必须带 `fact_id` 和 `source_id`。
4. Citation Auditor 对标题、作者、年份、DOI/arXiv/ADS ID 做二次核验。

### 9.2 可复现机制

1. 保存每次任务的配置、模型版本、提示词版本、工具调用日志。
2. 实验执行保存脚本、参数、数据版本、随机种子。
3. 报告附录记录假设从生成到审查的变更链。

### 9.3 合规机制

1. 数据集必须记录来源、许可证或访问说明。
2. 人文社科方向必须加入隐私、伦理和监管检查。
3. 医疗、金融、法律等高风险方向必须把系统定位为研究辅助，不输出直接决策建议。

## 10. 输出报告模板

```markdown
# Paper Title

## Problem Statement
...

## Rationale
- 推理链 1 ...
- 推理链 2 ...

## Technical Details
...

## Datasets
### Source
...

### Target
...

## Paper Abstract
Background: ...
Methods: ...
Expected Results: ...

## Methods
...

## Experiments
### Baselines
...

### Metrics
...

## Results
...

## References
1. ...
```

## 11. MVP 到参赛版路线

### 11.1 MVP：2-3 周

目标：跑通一条最小闭环。

范围：

1. 单一领域配置。
2. 10-30 篇论文。
3. 一个公开数据集。
4. 文献检索、事实抽取、假设生成、怀疑者评审、报告生成。
5. 命令行或极简前端展示。

验收：

1. 生成至少 3 个候选假设。
2. 每个假设有证据链和可证伪条件。
3. 输出完整赛题字段。
4. References 无虚构。

### 11.2 Alpha：4-6 周

目标：形成可演示系统。

范围：

1. 加入数据画像。
2. 加入知识图谱。
3. 加入实验设计和小规模执行。
4. 加入前端假设看板。
5. 加入 Agent 辩论日志。

验收：

1. 至少一个真实案例从输入跑到报告。
2. 可展示多智能体协作过程。
3. 可展示小规模可行性验证结果。
4. 报告能导出 PDF。

### 11.3 Beta：7-10 周

目标：对齐比赛评分。

范围：

1. 完善引用审计。
2. 增加多模态处理样例。
3. 增加时间回测或专家打分机制。
4. 固化百炼调用凭证截图。
5. 编写 20 页内技术方案。

验收：

1. 科学价值部分有明确创新性论证。
2. 技术深度部分有架构图、Agent 流程、工具调用日志。
3. 应用潜力部分有真实案例、复现说明和视频脚本。

### 11.4 提交前：最后 2 周

目标：材料稳定、可复现、可答辩。

范围：

1. 压缩技术方案到 20 页内。
2. 录制 10 分钟内演示视频。
3. 整理源码、README、运行说明。
4. 整理报名表、压缩包、网盘截图。

验收：

1. 新机器按 README 能复现实例。
2. 演示过程不依赖临时手工修补。
3. 所有引用和数据源可打开核验。

## 12. 风险与对策

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| 假引用或错引 | 科学价值和可信度直接失分 | 引用只能来自文献库，强制 Citation Auditor |
| 假设泛泛而谈 | 无法体现科学创新 | 强制机制、预测、可证伪条件和实验计划 |
| 多智能体只是包装 | 技术深度不足 | 保存角色分工、工具调用、审查修订日志 |
| 数据不可用或不合规 | 无法复现 | 优先使用公开数据集，记录许可证和版本 |
| 实验过大无法完成 | 影响演示稳定 | 只承诺小规模可行性验证，完整实验作为后续计划 |
| 模型输出不稳定 | 复现性差 | 固定提示词版本、模型版本、随机种子和缓存结果 |
| 前端开发拖慢核心 | 时间风险 | 前端只做工作台、看板、日志、报告四个核心视图 |

## 13. 推荐答辩叙事

1. 我们不是让模型直接写一个“看似像论文”的报告，而是构建了一个有证据、有反驳、有实验、有审计的科研灵感流水线。
2. 系统把科学假设生成拆成多个可检查环节：事实抽取、缺口发现、假设生成、怀疑者反驳、实验设计、引用审计。
3. 系统保留人在回路，符合真实科研流程，也降低模型幻觉风险。
4. 系统基于百炼 Qwen API 和 Qwen-Agent/自研编排，满足国产开源大模型和多智能体架构要求。
5. 真实案例使用公开文献和公开数据，能复现、能审计、能继续扩展。

## 14. 外部参考来源

1. 阿里云百炼产品简介：<https://www.alibabacloud.com/help/zh/model-studio/what-is-model-studio>
2. 阿里云百炼智能体应用：<https://help.aliyun.com/zh/model-studio/user-guide/single-agent-application>
3. 阿里云百炼知识库/RAG：<https://help.aliyun.com/zh/model-studio/using-the-knowledge-base>
4. 阿里云百炼知识检索工具：<https://help.aliyun.com/zh/model-studio/file-search>
5. 阿里云百炼 MCP 工具接入：<https://help.aliyun.com/zh/model-studio/rich-code-application-mcp>
6. Qwen-Agent 官方仓库：<https://github.com/QwenLM/Qwen-Agent>
7. The AI Scientist v1：<https://arxiv.org/abs/2408.06292>
8. The AI Scientist-v2：<https://arxiv.org/abs/2504.08066>
9. PaperQA：<https://arxiv.org/abs/2312.07559>
10. PaperQA2 仓库：<https://github.com/Future-House/paper-qa>
11. Sibyl: A Multi-Agent System for Literature-Based Scientific Discovery：<https://openreview.net/forum?id=ztVgK3iWCm>
12. NASA HEASARC API：<https://heasarc.gsfc.nasa.gov/docs/archive/apis.html>
13. NASA Astrophysics Data Centers：<https://science.nasa.gov/astrophysics/data/>
14. ESA Gaia Data Access：<https://www.cosmos.esa.int/web/gaia/data-access>

