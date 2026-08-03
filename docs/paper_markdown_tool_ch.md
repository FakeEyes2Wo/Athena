# Paper Markdown Tool

本文档是 `paper_markdown` 工具的协作契约，面向工具开发者、上游数据提供方、模型适配器
开发者、RAG 摄取模块和人工审阅者。它描述工具负责什么、如何交换数据，以及调用方应当
如何判断结果是否可用。具体论文暴露的历史问题和回归记录另见
[RAG 质量文档](paper_markdown_rag_quality_ch.md)。
下游索引、关系展开、质量处理和真实 PaSa 字段示例见
[RAG 产物说明](paper_markdown_rag_output_ch.md)。

## 定位与原则

Paper Markdown Tool 将上游已经获得的 TeX Source 或 PDF 转换为持久、可追溯、适合
论文 RAG 的 Markdown 和检索单元。目标是尽量恢复论文语义结构，而不是复刻出版物版式。

工具遵循以下原则：

- **TeX 优先**：有 TeX Source 时以它作为正文事实来源；只有没有 TeX 时才解析 PDF。
- **确定性优先**：能由解析器可靠完成的工作不调用模型。
- **证据与检索分离**：保存源码顺序的证据，同时允许生成更适合检索的语义副本。
- **内容寻址**：请求、正文、图片、诊断和结果都通过 `ArtifactStore` 引用交接。
- **显式降级**：无法恢复的结构或视觉语义必须进入诊断，不以猜测填补。
- **面向 RAG**：章节、引用边、视觉关系和检索 metadata 比页面视觉还原更重要。

工具注册名为：

```text
paper_markdown
```

调用参数 `request_ref` 指向 `PaperConversionRequest` artifact；返回的 `ToolResult.data`
通过 `paper_content_ref` 指向持久化的 `PaperContent` artifact。

## 协作边界

| 模块 | 责任 |
| --- | --- |
| AcademicSurvey / Web Search | 搜索论文、解析论文标识、下载 TeX/PDF，并把源文件写入 `ArtifactStore` |
| Athena 运行时 | 创建共享 `ArtifactStore`、注册工具、注入模型适配器并调度调用 |
| Paper Markdown Tool | 选择来源、解析结构、处理视觉对象、构造 chunk、执行质量门禁并持久化结果 |
| `VisualInterpreter` 提供方 | 根据图片、表格和上下文生成忠实、可检索的视觉解释 |
| `StructureRefiner` 提供方 | 只修复解析器明确标记的低置信 Markdown 结构 |
| RAG 摄取模块 | 检查质量状态、加载 retrieval units、建立 embedding/BM25/关系索引 |
| 上层 Agent | 使用检索结果和证据引用，不直接解析 TeX/PDF 大文件 |

工具不负责：

- 网络搜索、arXiv/DOI 解析或下载；
- 模型供应商客户端、密钥、配额、重试和服务部署；
- 向量数据库、BM25 索引或 reranker；
- Athena Session、Scheduler 或 Agent 工作流；
- 把 Markdown 再排版为出版级 PDF。

## 数据流

```text
上游 TeX/PDF artifacts
        │
        ▼
PaperConversionRequest artifact
        │
        ▼
ToolRegistry → PaperMarkdownTool → PaperProcessor
        │
        ├─ TeX Source ─→ TeX parser
        └─ PDF only  ─→ PyMuPDF parser
                         │
                         ▼
                ParsedPaper + visuals
                         │
             ┌───────────┴───────────┐
             ▼                       ▼
     deterministic structure   injected model assistance
             └───────────┬───────────┘
                         ▼
             Markdown + chunks + diagnostics
                         │
                         ▼
                PaperContent artifact
                         │
                         ▼
              load_retrieval_units()
                         │
                         ▼
                 RAG ingestion/index
```

模型只接收 artifact 引用所指向的必要证据，不接管来源选择、工作流推进或正常正文重写。

## 输入契约

`PaperConversionRequest` 的主要字段如下：

| 字段 | 含义 |
| --- | --- |
| `tex_source_ref` | 上游提供的 TeX 文件或源码包 artifact；与 PDF 同时存在时优先使用 |
| `tex_source_format` | `auto`、`tar`、`tar.gz`、`zip`、`gzip` 或 `plain` |
| `tex_entrypoint` | 可选的根 TeX 相对路径；未提供时由加载器确定性选择 |
| `pdf_ref` | 上游提供的 PDF artifact；没有 TeX 时作为正文来源 |
| `paper_id` | 建议提供的规范论文 ID，例如 arXiv ID、DOI 或语料库 ID |
| `metadata` | 标题、venue、年份等小型上游 metadata |
| `visual_policy` | `required` 或 `best_effort`；默认 `required` |
| `chunking` | chunk 目标字符数和完整元素 overlap 数量 |

至少需要 `tex_source_ref` 或 `pdf_ref` 之一。调用方只传 artifact ref，不传本地路径或
大体积内联内容。

### 来源选择

1. 提供 `tex_source_ref` 时始终走 TeX 路径。
2. 没有 TeX 时才使用 `pdf_ref`。
3. 两者同时存在时，PDF 保留在 provenance 中，但不参与正文解析。
4. 已提供的 TeX 无效时显式失败，不静默切换到 PDF。

这条规则可以让上游错误保持可见，也保证相同输入不会因环境差异选择不同来源。

### TeX 源码包

支持 tar、压缩 tar、zip、gzip 和单个 TeX 文件。自动入口选择会参考
`documentclass`、`document` 环境和常见根文件名。

源码包只在内存中展开。加载器会拒绝：

- 绝对路径和 `..` 路径穿越；
- 符号链接、硬链接和设备文件；
- 超出限制的文件数量或展开后大小；
- 递归 `input/include/subfile`；
- 无法确定或不存在的入口文件。

include 展开后仍保留原相对文件名和行号映射。

## 处理语义

### TeX 路径

TeX 解析器基于 `pylatexenc` 恢复文档结构，包括：

- 标题、作者、摘要和章节层级；
- 段落、列表、脚注、定理类正文和代码块；
- 行内公式和展示公式；
- citation keys、交叉引用目标和 labels；
- 独立 bibliography 条目；
- `includegraphics` 指向的 PDF、SVG 和栅格资源；
- 可以可靠展开的 `tabular` 表格。

无法可靠扁平化的复杂表格会保留证据并交给视觉/基础模型解释，而不是丢弃单元格或虚构
表格结构。每个元素保留 TeX 文件和行号定位。

TeX 浮动体的声明位置可能与正文讨论位置不同。工具同时保存 source heading path，并在
存在明确同章节引用时推断 semantic heading path。推断只影响检索语境，不移动原始
Markdown，也不改变源码 locator。

### PDF 路径

没有 TeX 时，PyMuPDF 路径根据文字块、字体和页面几何恢复：

- 页眉页脚过滤；
- 通栏和多栏阅读顺序；
- 标题层级、段落连接和断词；
- 页码及 bounding box；
- 可确定识别的 Markdown 表格；
- 嵌入式图片和图注；
- 矢量图区域和低文本页面预览。

PDF 路径不会把无法确认的公式重新发明为 TeX。低置信文本可以由可选
`StructureRefiner` 修复结构，但不得摘要或改变论文主张。

## 模型接口

### VisualInterpreter

`VisualInterpreter` 可以连接独立 VLM 或 Athena 基底模型。每次请求对应一个 figure、
table、equation 或低文本 page，包含：

| 字段 | 用途 |
| --- | --- |
| `visual_id` / `kind` | 稳定对象标识和视觉类型 |
| `asset_ref` / `media_type` | 规范化图片或原始视觉资源 |
| `structured_text_ref` | 确定性提取的表格或源文本 |
| `context_ref` | 图注、标签和邻近正文 |
| `locator` | 原 TeX 行号或 PDF 页码/bbox |

返回的 `VisualInterpretation` 包含：

- 忠实摘要 `summary`；
- 可独立进入检索的 `searchable_text`；
- 轴、趋势、节点、关系或表格字段等 `structured_data`；
- 固定到版本的模型标识；
- 可选置信度。

视觉适配器无法可靠解释时应抛出异常。它不应把 caption 原样返回后伪装为模型理解。

### StructureRefiner

`StructureRefiner` 只在解析器为元素写入明确的 `repair_issue_codes` 时调用。请求包含原始
片段引用、问题代码、locator 和限制性指令。返回结果必须保留所有事实、数字、引用和
公式；正常元素不会发送给该模型。

### 视觉策略

| 策略 | 模型缺失或调用失败时的行为 | 典型用途 |
| --- | --- | --- |
| `required` | 立即失败，不产生不完整结果 | 生产级多模态摄取 |
| `best_effort` | 保留 caption/表格文本等确定性证据，记录 warning | 无模型环境、故障恢复和结构测试 |

`best_effort` 的 unavailable visual 不生成独立视觉 retrieval unit，避免把 fallback 内容
重复索引。只有真正的模型解释才形成 `visual:*` retrieval unit。

## Chunk 与检索语义

chunk 以完整结构元素为边界，不切断公式、表格、图片或 bibliography 条目。表格、图片
和每条参考文献保持独立。overlap 只复用同章节内的 paragraph、list 或 abstract 元素。

每个 `PaperChunk` 有两份文本语义：

- `content_ref`：源码顺序的 chunk Markdown，用作证据和字符区间回放；
- `retrieval_text_ref`：实际用于 embedding、BM25 和 rerank 的检索文本。

当 source path 与 semantic path 不同，检索文本使用 semantic section 前缀并清除相邻
源码标题；原始证据保持不变。旧 schema 没有 `retrieval_text_ref` 时，读取器回退到
`content_ref`。

chunk 还保存：

- source 和 semantic heading paths；
- 全文字符区间与近似 token 数；
- TeX/PDF locators；
- citation keys；
- 本地 `reference_keys` 和该 chunk 定义的 `labels`；
- 关联的 `visual_ids`。

这些字段允许消费者执行章节过滤、citation 检索和按交叉引用展开表格、公式或附录。

## 持久化模型

当前 `PaperContent` schema 为 `1.3`。`PaperContent` 是轻量索引，不内联大正文或图片。

| 数据 | 持久化方式 |
| --- | --- |
| 完整 Markdown | `markdown_ref` |
| 摘要 / bibliography | `abstract_ref` / `bibliography_ref` |
| chunk 证据与检索文本 | 每个 `PaperChunk` 的两个 artifact refs |
| 原始或裁剪视觉资源 | `asset_ref`，可选规范化 `preview_ref` |
| 结构化表格文本 | `structured_text_ref` |
| 模型解释与视觉搜索文本 | `interpretation_ref` / `search_text_ref` |
| 处理诊断 | `diagnostics_ref` |
| 来源与转换器信息 | `PaperProvenance` |

`PaperVisual` 连接 visual、原元素、source/semantic headings、父 chunk、模型版本和解释
状态。消费者可以从检索结果回到 chunk，再回到视觉证据和源码 locator。

schema 1.0--1.2 继续可读；缺少的新字段使用兼容默认值。新增持久化字段或改变字段语义时
必须提升 schema 版本，并为旧版本提供读取策略。

## RetrievalUnit 契约

调用：

```python
units = await content.load_retrieval_units(store)
```

可以得到 provider-neutral 的正文和视觉检索单元。调用方应使用这个方法，而不是自行把
所有 artifact 拼成索引。

每个单元包含：

- 全局 `unit_id`：`paper_id:local_id`；没有 paper ID 时使用 source fingerprint；
- `kind` 和实际待索引 `text`；
- semantic `heading_path`；
- 源码 locator；
- paper ID、标题、作者、来源和质量状态；
- chunk 或 visual 的局部 ID 与关系 metadata。

正文 metadata 还包含 citation keys、reference keys、labels、visual IDs 和 source heading
path。视觉 metadata 包含 label、caption、element ID、父 chunk、模型和解释状态。

RAG 摄取模块应：

1. 先检查 `quality_status` 和 `quality_codes`；
2. 以 `unit_id` 作为跨论文唯一主键；
3. 索引 `RetrievalUnit.text`，不要绕过它读取原始 `content_ref`；
4. 保留 metadata 供章节过滤、关系展开和结果回溯；
5. 使用 `chunk_ids` / `visual_ids` 对同一证据的多粒度命中做去重或分组。

## 质量状态与失败

| 状态 | 含义 | 摄取建议 |
| --- | --- | --- |
| `pass` | 没有 warning/error 诊断 | 可进入常规索引 |
| `pass_with_notes` | 内容完整，只是记账不理想——chunk 超出目标大小、一个 chunk 含多个视觉、`\input` 大小写与磁盘不符但已找回等 | 可进入常规索引；重跑不会改善 |
| `degraded` | 内容确实没进语料——视觉解释缺失或失败、`\input` 未解析、表格/公式 Markdown 失效、入口靠弱推断选出 | 由质量策略按 code 接纳、隔离或重跑 |
| `unknown` | 旧产物没有完整质量结论 | 按保守策略处理 |

分级按**内容是否真的丢了**，不按"有没有 warning"。旧规则是任何 warning 都降级，于是"整个
视觉模态缺失"和"某个 chunk 比目标大 40%"拿到同一个标签，摄取侧无法判断重跑是否有意义。
代码归类见 `quality.CONTENT_LOSS_CODES` 与 `BOOKKEEPING_CODES`；未登记的新代码按 `degraded`
处理，且有覆盖测试强制显式归类。

典型显式失败包括：

- 请求没有任何来源；
- TeX 包不安全、损坏或入口无效；
- PDF 无效或没有可恢复内容；
- `required` 策略下视觉解释不可用；
- artifact 缺失或哈希校验失败。

质量门禁会检查元素 ID、字符区间、标题路径、公式 Markdown、表格矩形性、label/reference
图谱和检索章节上下文。视觉对象缺少父元素或正文占位符时会显式失败。warning code 是
摄取模块可以依赖的稳定机器接口；完整证据保存在 diagnostics artifact 中。

## ToolRegistry 接入

运行时必须显式使用共享 `ArtifactStore` 构造并注册工具。当前工具不依赖自动发现机制。

```python
import asyncio

from athena.core.tool import ToolRegistry
from athena.core.tool_types import ToolContext
from athena.research.paper_markdown import (
    PaperContent,
    PaperConversionRequest,
    PaperMarkdownTool,
)

request = PaperConversionRequest(
    paper_id="arxiv:2501.10120",
    tex_source_ref=tex_ref,
    pdf_ref=pdf_ref,
    visual_policy="required",
)
request_ref = await store.put_text(request.model_dump_json())

tool = PaperMarkdownTool(store, visual_interpreter, structure_refiner=None)
tools = ToolRegistry()
tools.register(tool)

async def emit(_kind, _ref, _data=None):
    pass

context = ToolContext(tool.spec.name, "manual-paper-call", emit, asyncio.Event())
result = await tools.resolve("paper_markdown").ainvoke(
    context,
    request_ref=request_ref,
)
if not result.success:
    raise RuntimeError(result.error)
result_ref = result.data["paper_content_ref"]
content = PaperContent.model_validate_json(await store.get_text(result_ref))
units = await content.load_retrieval_units(store)
```

上游写入来源、工具读取请求和消费者读取结果时必须使用同一个逻辑 ArtifactStore。
只有 artifact ref 而没有对应内容并不足以执行工具。

## 可复现性与并发

确定性来源、请求、工具版本和模型输出相同时，`PaperContent` 的 result ref 相同。
`LocalArtifactStore` 对相同内容幂等写入，并在读取时校验 SHA-256。

真实模型可能破坏字节级复现。需要精确复现模型产物时，模型适配层应固定模型 revision、
prompt version、解码参数和预处理版本，并按输入证据与配置缓存响应。

工具实例不保存跨请求业务状态。由于注入的视觉与结构模型协议不承诺并发安全，工具在
`ToolSpec` 中声明为串行执行；共享存储和模型适配器的生命周期由运行时负责。

## 模块协作地图

| 模块 | 关注点 |
| --- | --- |
| `schemas.py` | 请求、持久化对象和 retrieval contract |
| `interfaces.py` | 视觉解释与结构修复的 provider-neutral 协议 |
| `tex_source.py` | TeX 包安全读取、入口选择和 include 展开 |
| `tex_parser.py` | TeX AST 到结构化论文元素 |
| `pdf_parser.py` | PDF 页面结构、表格和视觉证据恢复 |
| `chunking.py` | 全文连接、证据 chunk 和检索文本构造 |
| `visuals.py` | 视觉预览、fallback 和正文视觉表示 |
| `quality.py` | 确定性 RAG 质量门禁 |
| `processor.py` | 端到端编排和 artifact 持久化 |
| `tool.py` | `paper_markdown` 的 `BaseTool` 与 `ToolRegistry` 边界 |

新增来源解析能力时应先归一化为共享的 `ParsedPaper`，再复用视觉、chunk、质量和持久化
流程。新增模型供应商时实现协议，不在 processor 中加入 provider 特例。

## 测试与验收

基础验证：

```powershell
$env:PYTHONPATH='src'
python -m pytest -q
python -m black --check src test
python -m compileall -q src test
git diff --check
```

完整工具验收至少覆盖：

- TeX 优先与 PDF fallback；
- 源码包安全和 include 定位；
- 标题、公式、表格、bibliography 和引用图谱；
- chunk 边界、检索章节和跨论文 ID；
- `required` 与 `best_effort` 视觉策略；
- 模型成功、失败，以及模型适配器对无效结果的拒绝；
- artifact 完整性和相同输入的确定性结果；
- 真实论文的 lexical/vector 检索探针。

PaSa 的工具级回归入口位于：

```text
../papers/PaSa_2501.10120_paper_markdown_tool_v3/reproduce.py
```

PDF fallback 的同链路回归入口位于：

```text
../papers/PaSa_2501.10120_paper_markdown_tool_pdf/reproduce_pdf.py
```

工具级回归必须经过正式的 `ToolRegistry → PaperMarkdownTool → PaperProcessor` 路径。
导出文件只用于人工检查，不参与转换结果生成；PDF 在未注入视觉解释器时会明确保持
`best_effort/degraded`，不会伪造视觉 retrieval unit。

### 视觉解释并发（2026-08-03）

每张图表是一次模型往返，此前 `enrich_visuals` 是 `for` 循环里逐张 `await`，转换耗时与
图表数线性相关。改为在信号量约束下 `gather`，顺序敏感的三件事——占位符替换、heading
归属、诊断合并——留在 gather 之后按源顺序完成，产物与串行版本一致。

在同一端点上实测：单次调用隔离 30.2 秒；6 路并发墙钟 119.9 秒而逐次延迟之和 431.0 秒，
即有效并行度 3.60×（并发下单次延迟从 36 秒升到 120 秒，因此拿不满 6×）。**上游本身确实
慢，但线性增长是编排造成的。**

真实链路前后对照：

| | 修改前 | 修改后 |
| --- | ---: | ---: |
| 转换总耗时 | 3637.4s | 669.5s |
| 图表数 | 58 | 44 |
| **单张摊薄** | **62.7 s/张** | **15.2 s/张** |

最干净的单篇对照是 `arxiv:2210.13701`：两轮同为 22 张图，**1776.1s → 401.3s（4.4×）**。

并发度由 `visual_concurrency` 配置（默认 6），`PaperMarkdownTool` 透传——合适的并发度依
端点特性而定，不该由工具替调用方决定。

### 一条被否决的借鉴：chunk 级断言标签（2026-08-03）

试过在转换期给每个 chunk 生成一行断言标签，供下游 `paper_rag` 做廉价扫描层。**已否决，
代码未保留**，结论与数据记在 [paper_rag 文档](paper_rag_tool_ch.md)。

这里只记与本模块有关的那一条代价：**"一篇论文一次模型请求"在真实论文上不成立。**
Attention Is All You Need 有 86 个 chunk，一次请求 48KB prompt，**>300 秒未返回**；同一篇
的 12 张图表在 6 路并发下总共只要约 180 秒。也就是说打标签一项就超过了整篇视觉解释的总和，
必须由适配器分批才能跑完，于是"一次调用"这个卖点本身也没剩下。

定因过程值得记下来，因为前三个猜测全错：端点被单独探针测过是健康的（文本 4.6s、图像
8.9s、编码 0.1s）；图片并不大（最大 1.6MB，多数 <500KB）；把并发从 6 拉到 16 反而更慢。
真因只有把每一段单独计时才暴露出来。**教训是定因要逐段计时，不要逐个猜。**

顺带发现并已修的一处：视觉解释器此前没有 `max_tokens` 上限，模型会为一张表写很长的
`structured_data`，单张耗时 63–99 秒。

### `\input` 大小写回退（2026-08-03）

arXiv 包多来自大小写不敏感的文件系统，源码写 `\input{prompts/ALFWorld}` 而磁盘上是
`alfworld.tex` 很常见。此前解析失败会把宏原样写回正文，内容静默丢失：ReAct
（arXiv:2210.03629）因此丢掉附录里 6789 字符的提示词，而全篇只留下一条 warning。

改为精确匹配优先、失败后做一次不区分大小写回退，命中时记 `tex_include_case_mismatch`
而不是静默吞掉；同名多候选按名称排序取定。真实包复核：该篇由 `degraded` 转
`pass_with_notes`，提示词正文确认恢复。

极端情况下若对不上的是主 `\input` 链，丢的会是大半篇论文，因此这条不是个案修补。

## 已知限制

- 复杂自定义 TeX 宏和非常规表格可能只能部分确定性展开；
- PDF 的阅读顺序、公式和无框表格无法达到 TeX Source 的可靠度；
- 图片中的数值、趋势和关系依赖实际注入的视觉模型；
- `best_effort` 结果可能适合文本 RAG，但不代表多模态质量通过；
- Athena 完整运行时仍需负责工具注册、上游来源交接和下游 RAG 摄取。

出现新质量问题时，应把可复现证据、影响、修复决策和验收标准记录到
[RAG 质量文档](paper_markdown_rag_quality_ch.md)，主文档只维护当前稳定契约。
