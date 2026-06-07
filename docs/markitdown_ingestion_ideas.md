# MarkItDown 利用设想

版本：0.1  
日期：2026-06-07  
相关 submodule：`external/markitdown`  
范围：说明 MarkItDown 在 Li-Shou AI Scientist 项目中可能如何使用。本文是资料接入方案设想，不是实施承诺。

## 1. 定位

MarkItDown 是 Microsoft 开源的轻量 Python 工具，用于把多种文件和资源转换成 Markdown，服务于 LLM 和文本分析流水线。它强调保留标题、列表、表格、链接等结构信息，而不是做面向人类排版的高保真文档转换。

对 Li-Shou AI Scientist 而言，它最适合放在输入层：

```text
PDF / DOCX / PPTX / XLSX / HTML / 图片 / 音频 / ZIP / 数据说明
  -> MarkItDown
  -> Markdown + metadata
  -> Chunker
  -> Evidence Store / Vector Store / Fact Extractor
```

换句话说，MarkItDown 不负责科学推理，也不负责引用审计；它负责把复杂资料转换成更适合模型读取、检索和切块的 Markdown。

## 2. MarkItDown 已具备的能力

根据 `external/markitdown/README.md` 和包内文档，MarkItDown 当前支持：

| 输入类型 | 用途 | 对 Li-Shou 的价值 |
| --- | --- | --- |
| PDF | 论文、题面、技术报告 | 文献进入 RAG 和事实抽取前的标准格式 |
| Word | 项目文档、实验记录 | 团队材料和领域资料统一转 Markdown |
| PowerPoint | 演示材料、课程资料 | 提取竞赛培训或专家课件内容 |
| Excel | 数据字典、实验表格 | 转为结构化文本供 Data Profiler 读取 |
| 图片 | EXIF、OCR 或图像描述 | 处理图表、截图、扫描页 |
| 音频 | 元数据和语音转录 | 可处理访谈、会议记录、答辩反馈 |
| HTML | 网页文档 | 处理公开文档、数据库说明页 |
| CSV/JSON/XML | 文本格式数据 | 进入数据画像前的辅助说明 |
| ZIP | 遍历压缩包内容 | 批量处理材料包 |
| YouTube URL | 视频转录 | 处理公开视频教程或讲座 |
| EPUB | 电子书资料 | 处理长文档背景资料 |

它还提供：

1. CLI：`markitdown input.pdf -o output.md`。
2. Python API：`MarkItDown().convert(...)`。
3. 可选依赖：按格式安装，如 `[pdf]`、`[docx]`、`[xlsx]`、`[all]`。
4. 插件机制：默认关闭，可用 `--use-plugins` 启用。
5. OCR 插件：`markitdown-ocr` 可增强 PDF/DOCX/PPTX/XLSX 内图片文字提取。
6. MCP server：`markitdown-mcp` 可让本地 trusted agents 通过 MCP 调用转换工具。
7. Azure Document Intelligence / Content Understanding 集成：用于更高质量的云端文档解析和结构化字段提取。

## 3. 与 AI Scientist 主框架的关系

MarkItDown 可服务于以下模块：

| 主框架模块 | MarkItDown 作用 |
| --- | --- |
| Literature Miner | 把论文 PDF、网页资料转为 Markdown |
| Fact Extractor | 提供带结构的文本输入，减少断章取义 |
| Data Profiler | 转换 Excel、CSV、数据说明、压缩包文档 |
| Knowledge Graph Builder | 从 Markdown 中提取实体、关系和引用上下文 |
| Citation Auditor | 保留来源文件、页码线索和转换日志 |
| Report Composer | 把中间材料整理成 Markdown 附录 |

它应位于“原始文件入库”和“模型抽取事实”之间，不应位于“模型生成报告”之后。

## 4. 推荐接入方式

### 4.1 MVP：CLI 转换

先用 CLI 打通最小路径：

```powershell
markitdown data\papers\paper.pdf -o runs\demo\ingest\paper.md
```

适合：

1. 手动处理少量论文和数据说明。
2. 验证 Markdown 切块质量。
3. 快速接入现有 RAG 流水线。

输出登记：

```yaml
source_path: data/papers/paper.pdf
output_path: runs/demo/ingest/paper.md
converter: markitdown
converter_version: unknown
hash: "<source file hash>"
notes:
  - "converted via CLI"
```

### 4.2 Alpha：Python API

后端正式接入时建议使用 Python API：

```python
from markitdown import MarkItDown

md = MarkItDown(enable_plugins=False)
result = md.convert_local("data/papers/paper.pdf")
markdown = result.text_content
```

原则：

1. 对本地文件优先使用 `convert_local()`。
2. 对上传文件流优先使用 `convert_stream()`。
3. 不直接把未验证 URL 交给宽泛的 `convert()`。
4. 每次转换记录输入 hash、输出 hash、转换器版本和时间。

### 4.3 Beta：MCP 工具

如果 Qwen Code、Codex 或 Minimal Harness 需要通过 MCP 调用文档转换能力，可以评估 `markitdown-mcp`。

建议：

1. 只在本机 trusted agents 场景使用。
2. HTTP/SSE 模式只绑定 `127.0.0.1`。
3. 不暴露到公网或局域网。
4. 仍然由 Li-Shou 的 policy 层限制可访问路径。

### 4.4 高质量解析：OCR 与云服务

对于扫描版 PDF、复杂表格或图像较多的材料，可考虑：

1. `markitdown-ocr`：使用 LLM Vision 做图片和扫描文档 OCR。
2. Azure Document Intelligence：更好的版面和表格解析。
3. Azure Content Understanding：多模态和结构化字段抽取。

这些能力可能产生额外 API 成本和数据合规问题。比赛 MVP 优先用本地转换；只有当样例材料确实需要时再启用 OCR 或云服务。

## 5. 在 Li-Shou 中的输入规范

建议定义一个 ingest job：

```yaml
job_id: ingest_001
project_id: astronomy_demo
tool: markitdown
input:
  path: data/papers/example.pdf
  type: pdf
output:
  markdown_path: runs/astronomy_demo/ingest/example.md
  metadata_path: runs/astronomy_demo/ingest/example.meta.yaml
policy:
  local_only: true
  allow_remote_uri: false
  enable_plugins: false
  max_file_size_mb: 100
```

对应产物：

```text
runs/
  astronomy_demo/
    ingest/
      example.md
      example.meta.yaml
      example.conversion.log
```

meta 文件建议包含：

```yaml
source_id: paper_001
source_path: data/papers/example.pdf
source_hash: sha256:...
source_type: pdf
converter: markitdown
converter_version: ...
converted_at: ...
output_path: runs/astronomy_demo/ingest/example.md
warnings:
  - "table extraction may be lossy"
```

## 6. 与证据链的连接

Markdown 转换后不能直接视为“事实”。推荐流程：

```text
raw file
  -> MarkItDown markdown
  -> chunk by section/table/page marker
  -> Literature Miner registers source_id
  -> Fact Extractor extracts fact_id
  -> Citation Auditor checks fact-source relation
```

每个事实至少记录：

```yaml
fact_id: fact_001
claim: "..."
source_id: paper_001
markdown_path: runs/astronomy_demo/ingest/example.md
locator:
  heading: "Methods"
  chunk_id: chunk_018
confidence: medium
```

如果转换质量不够，比如表格错位或 OCR 不稳定，Fact Extractor 应降低可信度或要求人工复核。

## 7. 安全与合规

MarkItDown README 明确提醒：它以当前进程权限执行 I/O，因此需要限制输入。

Li-Shou 中建议：

1. 只转换 `data/`、`uploads/`、`runs/` 等允许目录内的文件。
2. 禁止默认转换远程 URL。
3. 禁止访问用户主目录、云凭证、SSH key、浏览器 profile。
4. 对 ZIP 文件限制解压深度、文件数量和总大小。
5. 对音频、视频、OCR 或云解析标注数据合规风险。
6. MCP server 只允许 localhost，本地受信 agent 使用。
7. 转换失败或产生警告时，不把结果自动送入最终报告。

## 8. 推荐路线

### 8.1 第一阶段：手动/CLI

1. 用 CLI 转换赛题 PDF、样例论文 PDF、数据说明。
2. 人工检查 Markdown 质量。
3. 调整 chunking 策略。

验收：

1. `XH-202619.pdf` 可转换为 Markdown。
2. 至少 3 篇领域论文可转换并进入 Evidence Store。
3. 转换日志和 source hash 可追溯。

### 8.2 第二阶段：Python Ingest Service

1. 封装 `DocumentIngestor`。
2. 支持 PDF/DOCX/XLSX/HTML/CSV/JSON。
3. 生成 Markdown、metadata、conversion log。
4. 与 Literature Miner 和 Fact Extractor 对接。

验收：

1. 上传文件后自动生成 Markdown。
2. Fact Extractor 可按 section/chunk 引用来源。
3. Citation Auditor 能追溯到原始文件。

### 8.3 第三阶段：MCP/OCR 增强

1. 为 Qwen Code/Codex/Minimal Harness 提供 MarkItDown MCP。
2. 对扫描 PDF 或图片丰富文档启用 OCR 插件。
3. 只在必要场景评估 Azure 解析能力。

验收：

1. Agent 可以请求转换指定允许路径文件。
2. OCR 输出有明确标记。
3. 所有远程或云端调用都有人工确认和日志。

## 9. 风险与对策

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| PDF 表格或公式转换失真 | 事实抽取错误 | 对表格/公式 chunk 标记低可信，必要时人工复核 |
| 扫描 PDF 无文本层 | 文献抽取不完整 | 使用 OCR 插件或人工补充关键段落 |
| 远程 URL 被滥用 | 访问内网或敏感资源 | 禁止默认远程转换，使用 allowlist |
| ZIP 包过大或嵌套 | 资源耗尽 | 限制大小、文件数量、嵌套深度 |
| MCP 暴露过宽 | 本机文件泄露 | 仅 localhost，最好容器化并限制挂载目录 |
| 云解析涉及敏感数据 | 合规风险 | 仅处理公开资料，记录审批和数据范围 |

## 10. 参考来源

1. MarkItDown 仓库：<https://github.com/microsoft/markitdown>
2. 本地 README：`external/markitdown/README.md`
3. Python 包 README：`external/markitdown/packages/markitdown/README.md`
4. MCP 包 README：`external/markitdown/packages/markitdown-mcp/README.md`
5. OCR 插件 README：`external/markitdown/packages/markitdown-ocr/README.md`
6. 许可证：`external/markitdown/LICENSE`

