# Paper Markdown RAG 产物说明

本文档面向 Athena 下游 RAG 摄取、索引、检索和回答模块。它说明
`paper_markdown` 的持久化产物应如何消费，哪些字段用于索引与关系展开，以及在
`degraded` 状态下哪些证据仍可安全使用。

工具转换流程和模型接口见 [Paper Markdown Tool 协作契约](paper_markdown_tool_ch.md)。
已知解析缺陷、验收规则和实际修复记录见
[RAG 质量文档](paper_markdown_rag_quality_ch.md)。

## 消费入口

下游接收的是 `PaperContent` artifact ref，而不是 TeX、PDF、本地路径或导出的 Markdown
文件。使用同一个 `ArtifactStore` 读取 `PaperContent`，再调用
`load_retrieval_units()` 得到唯一允许直接进入检索索引的标准单元。

```python
from athena.research.paper_markdown.schemas import PaperContent

content = PaperContent.model_validate_json(await store.get_text(result_ref))
units = await content.load_retrieval_units(store)
```

不得自行把 `markdown_ref`、`content_ref`、视觉 caption 或 `search_text_ref` 全部拼接后
索引。这样会绕过质量策略，并在视觉解释不可用时制造重复文本。

## 产物关系

```text
PaperContent
  |- markdown_ref / abstract_ref / bibliography_ref     完整证据副本
  |- chunks[]                                           正文、表格、公式、参考文献的原子证据
  |    |- content_ref                                   源码顺序文本，用于回放和展示
  |    `- retrieval_text_ref                            已加入正确检索章节语境的索引文本
  |- visuals[]                                          图片、表格、公式或低文本页面的证据资产
  |    |- asset_ref / preview_ref                        供证据回溯或视觉解释使用
  |    `- interpretation_ref / search_text_ref           仅成功解释后形成独立视觉检索单元
  `- diagnostics_ref / quality_status / quality_codes    摄取决策依据
        |
        `-> load_retrieval_units()
              |- chunk retrieval units                  总是由可接受的正文 chunk 产生
              `- visual:* retrieval units               仅视觉解释成功时产生
```

`content_ref` 与 `retrieval_text_ref` 的职责不同：前者保留文档源码顺序，后者是 embedding、
BM25 和 reranker 应使用的文本。两者之间的区别通常来自 float 的语义归位，例如表格在 PDF
版面中出现在附录页，但正文明确在实验章节引用它；此时检索单元采用实验章节，证据定位仍
保留 PDF 原页 bbox。

## 检索单元契约

每个 `RetrievalUnit` 都可独立 upsert 到任意向量库或倒排索引：

| 字段 | 下游用途 |
| --- | --- |
| `unit_id` | 跨论文全局主键。形式为 `paper_id:local_id`；用于幂等 upsert 和去重。 |
| `kind` | `paragraph`、`table`、`figure`、`equation`、`bibliography` 或 `visual:*`；用于检索过滤和展示策略。 |
| `text` | 唯一的待索引文本；同时送入 embedding、BM25 和 rerank。 |
| `heading_path` | 检索语义章节；用于章节过滤、结果分组和回答上下文。 |
| `locators` | TeX 文件/行号或 PDF 页码/bbox；用于证据定位和原文跳转。 |
| `metadata` | 小型字符串 metadata；包含论文身份、质量状态、引用边、label 和视觉关联。 |

`metadata` 的稳定字段如下：

| 字段 | 语义 |
| --- | --- |
| `paper_id` / `retrieval_namespace` | 论文身份与 unit ID 命名空间。 |
| `title` / `authors` / `source_kind` / `source_ref` | 文献归属与来源筛选。 |
| `quality_status` / `quality_codes` | 摄取时的可用性与降级原因。 |
| `chunk_id` | 正文证据的局部 ID；视觉单元使用 `visual_id`。 |
| `source_heading_path` | 原始版面位置的章节路径。与 `heading_path` 不同时，后者是检索路径。 |
| `citation_keys` | 当前证据引用的文献键。 |
| `reference_keys` | 当前证据指向的内部 label，例如 `Table 4`。 |
| `labels` | 当前证据定义的内部 label；可由 `reference_keys` 反向解析。 |
| `visual_ids` / `chunk_ids` | 正文、视觉证据之间的父子关系。 |

当前 metadata 的多值字段是字符串：`authors` 使用 `; ` 分隔，其余列表字段使用 `,` 分隔。
下游应保留原始字符串用于过滤，同时在写入关系索引时按该约定拆分；空字符串表示没有值。

## 摄取策略

推荐将一篇论文分成三个互补索引，而不是只保存向量：

| 索引 | 键 | 内容 | 用途 |
| --- | --- | --- | --- |
| 检索索引 | `unit_id` | `text`、向量、BM25 字段、`kind`、章节、质量 metadata | query 召回与 rerank。 |
| 关系索引 | `paper_id + label` | label 定义 unit、`reference_keys` 指向 unit、`visual_ids` | 从正文讨论跳转到表格、图片、公式或附录。 |
| 证据索引 | `unit_id` | `locators`、`content_ref`、视觉 asset/preview refs | 回答引用、PDF 跳转和人工核验。 |

最小摄取流程：

```python
async def ingest_paper(store, result_ref, search_index, relation_index):
    content = PaperContent.model_validate_json(await store.get_text(result_ref))
    decide_quality_policy(content.quality_status, content.quality_codes)

    for unit in await content.load_retrieval_units(store):
        metadata = unit.metadata
        await search_index.upsert(
            key=unit.unit_id,
            text=unit.text,
            metadata=metadata | {
                "kind": unit.kind,
                "heading_path": " / ".join(unit.heading_path),
            },
        )
        for label in split_metadata_values(metadata.get("labels", "")):
            await relation_index.define(metadata["paper_id"], label, unit.unit_id)
        for target in split_metadata_values(metadata.get("reference_keys", "")):
            await relation_index.reference(unit.unit_id, target)
        for visual_id in split_metadata_values(metadata.get("visual_ids", "")):
            await relation_index.attach_visual(unit.unit_id, visual_id)
```

同一个查询命中正文和其关联表格/图像时，应按 `paper_id`、`chunk_id` 与 `visual_ids` 分组。
这可避免把同一 caption、表格解释和正文讨论作为三条独立结论返回。回答层应把命中的
`locators` 与 `content_ref` 作为可验证引用，不应只展示生成后的摘要。

## 质量状态

| 状态或代码 | 默认摄取策略 |
| --- | --- |
| `pass` | 正常索引。 |
| `degraded`，且仅包含业务允许的代码 | 索引文本单元，同时保留质量 metadata 并在回答需要该能力时降级。 |
| `visual_interpretation_unavailable` | 保留正文、caption 和确定性表格 chunk；不期待 `visual:*` 单元，也不把图片趋势当成已理解事实。 |
| `pdf_formula_layout_fragment_omitted` | 正常检索其余文本；涉及被省略 PDF 公式的精确推导时回到公式 visual asset 或要求视觉解释。 |
| `rag_*`、来源损坏、无法接受的业务代码 | 隔离或重新处理，不进入面向用户的默认索引。 |
| `unknown` | 按保守策略隔离，直到重新处理或完成审计。 |

质量代码是机器可读决策输入，不是仅供日志展示的文本。策略应由下游配置决定，但不能仅因
`degraded` 就静默当作 `pass`，也不能因视觉解释失败而丢弃全部可靠正文。

## PaSa 实例

以下来自正式 PDF fallback 产物：

```text
papers/PaSa_2501.10120_paper_markdown_tool_pdf/
  run_manifest.json
  paper_content.json
  pasa.md
  chunks/
  visuals/
```

其 result ref 为：

```text
sha256:bf5d754b9dea077fabbde9b8f81f68bca72316f7383a67ac97bde1b3f70cf9b2
```

产物为 `schema_version=1.3`，有 103 个 retrieval units、25 个视觉对象（20 表、3 图、
2 公式）。当前没有注入视觉解释器，因此 `quality_status=degraded`，质量代码为：

```text
pdf_formula_layout_fragment_omitted
visual_interpretation_unavailable
```

### 正文到表格的引用边

正文单元 `arxiv:2501.10120:chunk-6f855fe51ac8e10e`：

```text
kind: paragraph
heading_path: 5 Experiments / 5.3 Main results
text: As shown in Table 4, PaSa-7b outperforms all baselines ...
metadata.reference_keys: Table 4
locator: PDF page 8, bbox [70.87, 719.35, 153.70, 730.26]
```

下游先将这条正文命中作为主论断，再以 `(paper_id, "Table 4")` 查询关系索引，取得
label 为 `Table 4` 的 table unit。这样“主结果”查询既能命中文本解释，也能展开其对应
表格证据，而无需依赖字符串模糊匹配。

### 语义章节不同于源码章节的表格

Table 14 单元 `arxiv:2501.10120:chunk-adac5a001d05c6eb`：

```text
kind: table
heading_path: G Additional Experimental Results /
              G.1 Results on 100-sample subset of AutoScholarQuery
metadata.source_heading_path: G Additional Experimental Results / G.2 Action cost
metadata.labels: Table 14
metadata.visual_ids: table-bc1449f4db68
locator: PDF page 15, bbox [101.24, 241.92, 494.04, 367.53]
```

PDF 版面把 Table 14 放在相邻的小节位置，但正文明确在 `G.1` 讨论它。检索时使用
`heading_path`，展示“原文位置”或打开 PDF 时使用 `source_heading_path` 与 locator。两者
都不能被下游覆盖或混为一谈。

### 原子参考文献单元

```text
unit_id: arxiv:2501.10120:chunk-49c1ea83a9e33fe7
kind: bibliography
heading_path: References
text: OpenAI. 2023. Gpt-4 technical report. arXiv preprint arXiv:2303.08774.
locator: PDF page 11, two source bboxes
```

该单元是一条完整参考文献，而不是作者半行、页码或单独 arXiv ID。可单独参与“技术报告”
或“被引用工作”的检索，也可在回答中作为可回溯书目证据。

### 公式与视觉降级

```text
unit_id: arxiv:2501.10120:chunk-84e24bca79da547c
kind: equation
heading_path: 4 Methodology / 4.2 Crawler
visual_ids: equation-476cc6494d59
text: ˆA(st, at) = ˆRt −ˆVϕ(st). (4)
locator: PDF page 6, bbox [360.08, 397.40, 525.01, 409.51]
```

该公式单元仍可用于章节或符号关键词检索；其 `Visual source` 链接只用于取回证据图片，
不是视觉理解结果。由于本次 `visual_interpretation_unavailable`，`load_retrieval_units()`
没有生成任何 `visual:equation`、`visual:figure` 或 `visual:table` 单元。下游不得从该事实
推出图片中的数值、坐标轴趋势或完整公式布局。

## 回答与审计

面向用户的回答至少应保存并可返回：论文 `paper_id`、`unit_id`、标题、章节、locator 和
质量状态。需要展示原文时，按以下顺序回溯：

```text
retrieval hit -> unit_id -> chunk/visual metadata -> content_ref or asset_ref -> locator
```

不要把模型生成答案本身写回 `PaperContent`。答案、用户反馈和检索排名属于下游 RAG 的
独立状态；`PaperContent` 只保存可复现的论文转换证据。
