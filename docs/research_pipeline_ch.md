# Academic Survey 全链路：组合根与流水线

`paper_scout` → `paper_source` → `paper_markdown` → `paper_rag` 四个模块的交接契约本来就是
闭合的，缺的只是两样东西：一个把依赖装配起来的地方，和一个把四段依次推进的驱动。本文档描述
这两样——`athena/research/wiring.py` 与 `athena/research/pipeline.py`。

## 为什么需要它们

四个模块都遵守同一条纪律：不在导入时创建客户端、不读环境变量、依赖一律由调用方注入。这条
纪律的代价是**必须恰好有一个装配点**，否则模块就没有任何办法被真正用起来。

在补上组合根之前，症状很具体：TUI 里问模型有哪些工具，列表里没有 `paper_markdown`、
`paper_fetch` 这些——不是 TUI 的 bug，是没人注册它们。`build_corpus_index` 只被测试调用过，
`paper_markdown` 与 `paper_rag` 从未在测试之外运行过。

## 组合根 `wiring.py`

### 环境变量

| 变量 | 用途 | 缺失时 |
| --- | --- | --- |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | OpenAI 兼容端点 | 客户端不可用 |
| `ATHENA_RESEARCH_MODEL` | 策略与打分模型 | 回落 `ATHENA_TUI_MODEL` |
| `ATHENA_TUI_MODEL` | 同上的兜底 | 无文本模型，流程无法开始 |
| `ATHENA_EMBEDDING_MODEL` | 句向量 | 语义检索关闭，关键词检索照常 |
| `ATHENA_VISION_MODEL` | 图表解读 | 图表退回仅证据文本 |
| `ATHENA_ARTIFACT_ROOT` | artifact 根目录 | 默认 `~/.athena/artifacts` |
| `ATHENA_CONTACT_EMAIL` | arXiv/OpenAlex 礼貌池 | 不进礼貌池 |
| `SEMANTIC_SCHOLAR_API_KEY` | S2 检索 | 几乎必然 429，见下 |
| `OPENALEX_API_KEY` | OpenAlex 免费额度 | 匿名额度（0.10 美元/天） |
| `ATHENA_GHOSTSCRIPT` | EPS/PS 光栅化 | 按 PATH 自动发现 |

能力缺失一律降级而不是报错：链路在降级形态下仍然完整可跑，把它做成硬错误只会让首次接入寸步
难行。`python -m athena.research --check` 只装配并报告能力，不发起任何检索。

**`SEMANTIC_SCHOLAR_API_KEY` 的影响比看上去大。** 实测同一查询：无 key 时 19 次 search 有
15 次 429，候选池 148 篇；配上 key 后 28 次动作只剩 4 次失败，池 240 篇。

### 两个此前没有生产实现的协议

`TextEmbedder`（paper_rag）与 `VisualInterpreter`（paper_markdown）此前只有测试替身。

**`OpenAIEmbedder`** 有两处不显眼但必须的设计：

1. **响应按 `index` 重排。** 批量编码的响应顺序由服务端决定，而 `build_corpus_index` 依赖
   向量与句子严格一一对应。顺序错位不会报错，只会让之后每一次语义检索都返回错的句子。
2. **并发有上限、限流会重试。** 一次 50 篇的调研要编码约 39000 条句子，按 16 条一批就是两千
   多个请求。无上限地 `gather` 会把它们同时打出去，真机上直接撞出 `insufficient_quota`——
   而且是在取源与转换都已完成之后，最贵的部分已经付了钱。默认并发 4，遇 429 指数退避重试 5 次。

**`VisionInterpreter`** 把图片以 data URI 内联，不走外链：`ArtifactStore` 是内容寻址的本地
存储，没有可供模型访问的 URL，而把图片临时上传到公网只为了让模型看一眼，既多一份凭据又多
一处泄漏面。

### Ghostscript 发现

`find_ghostscript()` 先看 `ATHENA_GHOSTSCRIPT`，否则按 PATH 依次找
`gs`/`gswin64c`/`gswin32c`/`mgs`/`rungs`。后两个是 MiKTeX 自带的那份——装了 TeX 发行版的机器
通常已经有了，不必另装。结果注入 `PaperProcessor`，`paper_markdown` 自己不查 PATH。

## 流水线 `pipeline.py`

刻意写成确定性流程而不是 Agent：四段之间没有需要多轮推理的决策——读多少篇、失败怎么办、降级
的要不要进索引，全部是可以提前定下的策略参数。交给模型只会让成本不可预测。

```text
SurveyRequest
     │
     ├─ _scout    → PaperScoutResult.paper_source_request_ref
     ├─ _fetch    → PaperSourceRecord.conversion_request_ref
     ├─ _convert  → PaperContent（并发，受 conversion_concurrency 约束）
     └─ _index    → corpus_ref
     ▼
SurveyReport（逐篇成本与质量事实）
```

### 每一段的失败都不终止流程

这是本模块的核心约束，也是被真机连续打破两次的地方：

| 位置 | 失败时 | 为什么在这一层 |
| --- | --- | --- |
| `paper_source._fetch_url` | 记 warning，继续试下一个候选 | 线索 URL 来自检索后端，域名不可控 |
| `paper_source._fetch_one` | 这一篇标记 failed | 取源是唯一按篇计费的阶段 |
| `paper_source._resolve_versions` | 退回空解析 | 它在逐篇取源之前，异常逃出去等于整批拿不到 |
| `pipeline._convert_one` | 这一篇记失败原因 | 转换失败率要能算出来 |
| `pipeline._index` | `status=partial` + `corpus_ref=None` | 语料可以事后重建，取源和转换的钱补不回来 |

两次真机事故都属于这一类：一条 `doi.org` 线索 TLS 握手超时，异常一路逃到 `run_survey`
打断 50 篇的全程；修好之后又在建索引时撞穿编码配额，把已经完成的取源与转换一起丢掉。

### 跨段的身份合并

`paper_scout` 的 `paper_key_for()` 是 arXiv 优先（`expand` 沿 arXiv id 工作），
`PaperIdentity.paper_key()` 是期刊 DOI 优先（arXiv 自铸 DOI 会让语料按投稿年份分裂）。同一篇
论文因此会在交接处换 key，两边各自都对。

真机上表现为 `arxiv:2303.02505` 在报告里出现两次：一次 skipped，一次带着新 DOI 且相关性为 0。
`_by_identifier` / `_canonical_key` / `_conversion_keys` 三张表负责把它们并回一条。

### 静默内容丢失

`MIN_PLAUSIBLE_MARKDOWN` 是流水线层的兜底：转换报成功、正文却近乎为空时标 `suspect_empty`
并拒绝进语料。它不替代 `paper_markdown` 自己的质量门禁，只识别"几乎什么都没有"——因为
"成功但空"会带着 `pass` 一路进语料，让检索以为这篇论文已经覆盖。

根因（PDF-wrapper 投稿）已在 `paper_markdown` 里修掉，这条兜底保留：它防的是下一个还没被
发现的同类问题。

### 语料门禁

`_indexable` 默认只有 `suspect_empty` 一票否决，`degraded` 放行。理由与实测数据见
[RAG 质量文档](paper_markdown_rag_quality_ch.md)第六次审计。`strict_quality=True` 恢复
"只要 pass / pass_with_notes"的严格口径，供需要干净语料的评测使用。

## 命令行

```bash
python -m athena.research --check                       # 只装配自检
python -m athena.research --query "..."                 # 全链路
python -m athena.research --papers 1706.03762,1512.03385 # 跳过检索，单独量下游
```

`--papers` 存在的理由：检索是全链路里最慢也最贵的一段，而测量取源与转换的健壮性并不需要它，
把两者绑在一起只会让下游的样本量受制于检索门槛。

主要开关：`--max-papers`（默认 50）、`--retain-threshold`（默认 0）、`--allow-unfetchable`、
`--strict-quality`、`--concurrency`、`--no-index`。

## 真机基线（2026-08-04）

查询 `regularization techniques that improve AUC on imbalanced tabular data`：

| 项目 | 数值 |
| --- | --- |
| 候选池 / 交付 | 240 / 50 |
| 无源剔除 | 82 篇（未占交付名额） |
| 取源成功 | 44 / 50（arXiv 32/32，DOI 12/18） |
| 转换成功 | 44 / 44，硬失败率 **0%** |
| 静默丢失 | 0 篇 |
| 语料 | 44 篇 / 4928 chunk / 36920 句向量 |
| 耗时 | scout 640s，source 341s，markdown 1181s，index 213s，合计 2375s |
| 调用 | http 152，vision 1347，embed 2308 批 / 36920 条 |

那 12 篇纯 DOI 论文靠上游的开放获取链接才取到源，只按 arXiv id 过滤会全部丢失。

## 已知限制

- `LocatorCache` 默认只在进程内，重跑会重新下载；转换没有缓存，重跑要重付视觉调用；
- `_index` 一次性编码全部句子，篇数上去后是配额压力最大的一段；
- 交付集合的同分排序按 `paper_key` 字典序，等价于"arXiv 优先且编号小的优先"，即偏向老论文。
  这是为排序稳定加的，不是相关性设计，在 `retain_threshold=0` 之后影响被放大了。
