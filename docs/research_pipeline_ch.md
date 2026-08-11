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
| `ATHENA_RESEARCH_MODEL` | PaperScout 的策略模型 | 回落 `ATHENA_TUI_MODEL` |
| `ATHENA_SCORER_MODEL` | 相关性打分模型 | 沿用策略模型 |
| `ATHENA_TUI_MODEL` | 上面两个的兜底 | 无文本模型，流程无法开始 |
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

### 为什么打分要单独配一个模型

`scout` 占全链路 69% 的墙钟，而它内部的成本结构很不平衡：

| | 每轮次数 | 单次延迟 | 输出 token |
| --- | --- | --- | --- |
| 策略调用（决定下一步搜什么） | 4–6 | 38.9s | 2210 |
| **相关性打分**（四档分类） | **38–56** | 15.8–19.0s | 857–1037 |

延迟由输出 token 决定，不由 prompt 大小决定。策略每步一次、要在 20 篇观测里做判断，值得用强
模型；打分只是读标题加摘要判 0/1/2/3，却是调用次数最多的一环。

实测同一批论文（一半贴题、一半明显不贴题）：

| 模型 | batch=8 | batch=24 | 判分 |
| --- | --- | --- | --- |
| `qwen3.7-plus` | 19.0s | 40.7s | 贴题 1.0 / 不贴题 0.0 |
| `qwen3.6-flash` | **6.1s** | **12.1s** | **完全相同** |

每篇摊到的时间从 2.38 秒降到 0.50 秒。`ATHENA_SCORER_MODEL` 因此独立于策略模型；不设时沿用
策略模型，行为与分开之前一致。

#### 换模型对判分的影响，以及唯一要守的纪律

同一批 150 篇论文，plus 打两遍、flash 打一遍（受控对比——四轮真机的分数分布差异里混着"论文
不同"这个变量，不能用来判断模型）：

| 对照 | 逐篇档位一致率 |
| --- | --- |
| plus vs plus（同会话连打） | **94.0%** ← 噪声底 |
| plus vs flash | 63.3% / 68.7% |

**差异确实超出噪声，但方向是好的。** 分歧集中在 2 分档：plus 判 2 而 flash 判 1 的多是不平衡
数据的**应用**论文（SMOTE 对比、医疗预测、欺诈检测）；flash 判 2 而 plus 判 1 的 7 篇里有 6 篇
是 **AUC 优化方法**论文（代理损失、随机近端算法）。查询问的是"提升 AUC 的正则化技术"，flash
抓的是这个中心词。这是单查询上的人工判断，不是 ground truth。

**唯一的硬约束在门槛上：**

| 门槛 | plus | flash |
| --- | --- | --- |
| `> 0.45`（只要 3 分） | 2 篇 | 2 篇，且是同样那 2 篇 |
| `> 0.2`（含 2 分） | 29 / 37 篇 | **17 篇** |

3 分档跨模型、跨会话完全一致，三次打分零摇摆。2 分档不是——"切题但不完全满足"这个判据本身
模糊，**同一个模型连打两遍就是 27 篇与 35 篇，稳定率 77%**。所以：`retain_threshold` 保持默认
0 或用 0.5 都安全，**用 0.3 时换打分模型必须重测**。

两个副作用，都不影响交付：flash 多判 21 篇为 0 分，被 `τ=0.01` 挡在池外，池子小约 14%——那
21 篇 plus 有 20 篇判 1 分，本来就轮不到交付。反过来，flash 更小更集中的 2 分档让交付集合更多
由打分器决定、更少由 `tie_break` 的随机次序决定。

`GradedRelevanceScorer` 的 `batch_size` 同时从 8 提到 24：三倍的量只多花五成时间，一个 150 篇
的池从 19 个请求降到 7 个。代价是一次解析失败作废的论文更多——失败的批次整批按 0 分处理。

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
     ├─ _scout    → PaperScoutResult.paper_source_request_ref（要 max_papers × 1.3 篇）
     ├─ _fetch    → PaperSourceRecord.conversion_request_ref
     ├─ _convert  → PaperContent（截回 max_papers；并发，受 conversion_concurrency 约束）
     └─ _index    → corpus_ref
     ▼
SurveyReport（逐篇成本与质量事实）
```

### 取源垫底

取不到源只有试过才知道。`require_retrievable_source` 能挡掉"上游没给任何线索"的论文，但挡不住
线索本身失效——真机上三种都遇到过：`doi.org` 重定向回 0 字节（IEEE）、ACM 与 MDPI 对非浏览器
请求返回 403，其中 MDPI 那篇确实是开放获取，纯粹被反爬拦下。

名额少的时候这件事被放大：50 篇丢 6 篇是 12%，10 篇丢 3 篇就是 30%。所以 `_scout` 按
`ceil(max_papers × source_overshoot)`（默认 1.3，且至少多两篇）交付，`_convert` 再按相关性
截回 `max_papers`，多出来的记 `conversion_status="surplus"`。

截断放在转换之前而不是取源之前：取源不调模型，是整条链路里最便宜的一段，转换才是花钱的。
`surplus` 与 `failed` 严格分开——垫底篇数是策略决定的，混进转换失败率会让那个数字随
`source_overshoot` 浮动。`--source-overshoot 1` 关掉垫底；`--papers` 显式点名的论文不做垫底，
也不被 `max_papers` 截掉。

### `status` 只描述本次运行自己的产物

| 值 | 含义 |
| --- | --- |
| `empty` | 一篇都没转换成功 |
| `partial` | 转换出了东西，但要的产物缺了一件（目前只有：要求建索引却没拿到 `corpus_ref`） |
| `complete` | 其余情况 |

**它不再沿用 `PaperScoutResult.status`。** 那个字段的口径是"检索期间有没有后端报过错"，于是
Semantic Scholar 零星 429 就能把整轮标成 `partial`——真机上出现过语料建好、7 篇全进索引、报告
却写着 `partial` 的情况，看报告的人只会以为语料没建成。scout 的自评保留在 `scout_status`，
原因保留在 `warnings`。

逐篇的取源与转换失败同样不降级：那是尽力而为流水线的正常产出，篇数、失败率和逐篇原因都已经
在报告里，用一个总状态去概括只会丢信息。

### 每一段的失败都不终止流程

这是本模块的核心约束，也是被真机连续打破两次的地方：

| 位置 | 失败时 | 为什么在这一层 |
| --- | --- | --- |
| `HostRateLimiter._get_locked` | 传输失败按指数退避重试，与 429 同一条路径 | 超时是瞬时故障，重试比降级便宜得多 |
| `paper_source._fetch_url` | 记 warning，继续试下一个候选 | 线索 URL 来自检索后端，域名不可控 |
| `paper_source._fetch_one` | 这一篇标记 failed | 取源是唯一按篇计费的阶段 |
| `paper_source._resolve_versions` | 退回空解析 | 它在逐篇取源之前，异常逃出去等于整批拿不到 |
| `pipeline._convert_one` | 这一篇记失败原因 | 转换失败率要能算出来 |
| `pipeline._index` | `corpus_ref=None`，由 `final_status` 判成 `partial` | 语料可以事后重建，取源和转换的钱补不回来 |

三次真机事故都属于这一类：一条 `doi.org` 线索 TLS 握手超时，异常一路逃到 `run_survey` 打断
50 篇的全程；修好之后又在建索引时撞穿编码配额，把已经完成的取源与转换一起丢掉。

第三次暴露的是**退回空解析还不够**。`export.arxiv.org` 的批量版本解析超时一次，
`_resolve_versions` 按设计退回空解析、流程没有中断——但版本解析是整批一次请求，钉不到版本的
arXiv 论文全部按 `version_unresolved` 跳过，13 篇里当场丢掉 9 篇，剩下 4 篇还全部退化成开放获取
PDF 通道（`tex_sources: 0`）。

根因不在 `_resolve_versions`，在 `HostRateLimiter`：`UrllibTransport` 把超时与 DNS/TLS 故障包成
`HttpTransportError` 并注明"按可重试处理"，而 `_get_locked` 此前只对 429/5xx 重试，异常直接穿了
出去——注释与行为对不上。现在两者走同一条退避路径。

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

主要开关：`--max-papers`（默认 10）、`--scorer-model`、`--retain-threshold`（默认 0）、
`--source-overshoot`（默认 1.3）、`--allow-unfetchable`、`--strict-quality`、`--concurrency`
（默认 4）、`--max-seconds`（默认 600）、`--no-index`。

**`--max-papers` 省不到检索。** `paper_scout` 会给整个候选池打分（实测池 240 篇、
`scored_papers` 也是 240），`max_papers` 只在 `_finish` 里做最后一次截断。把它从 50 调到 10，
省的是取源、转换和索引这三段。

**要压检索时间，先换打分模型，其次才是 `--max-seconds`。** 三轮实测里有两轮是撞 600 秒墙钟停在
第 4 步的（`stop_reason: max_seconds`），`--max-steps` 根本没成为绑定约束——同样的工作量，每步
耗时在 97 到 178 秒之间浮动，取决于 provider 当时的延迟。`--max-seconds` 是唯一确定的时间上界，
代价是池更小。

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

## 真机基线（2026-08-10，`--max-papers 10`）

同一个查询，默认 10 篇。这一轮跑在取源垫底与 `--concurrency 3` 之前，所以取源只要了 10 篇、
转换按并发 2 跑：

| 项目 | 数值 | 对照 50 篇 |
| --- | --- | --- |
| 候选池 / 交付 | 241 / 10 | 240 / 50 |
| 无源剔除 | 80 篇 | 82 篇 |
| 取源成功 | **7 / 10** | 44 / 50 |
| 转换成功 | 7 / 7，硬失败率 0% | 44 / 44 |
| 语料 | 7 篇 / 404 检索单元 / 4106 句向量 | 44 篇 / 36920 句向量 |
| 耗时 | scout 581s，source 68s，markdown 141s，index 29s，**合计 820s** | 2375s |
| 调用 | http 74，vision 93，embed 257 批 | http 152，vision 1347，embed 2308 批 |

墙钟 13 分 44 秒，比 50 篇省 65%。scout 占 71%——它只随 `max_steps` 变，不随 `max_papers` 变，
两轮的池大小（241 vs 240）和无源剔除数（80 vs 82）几乎一致，说明检索段本身高度可复现。

**交付集合与上一轮几乎不重叠**，10 篇里只有 2 篇相同：策略与打分都走 LLM，同一查询两次跑出的
池成分不同。所以"按上一轮报告的前 N 篇推算"只能给量级，给不了名单。

**这一轮暴露的两件事已经修掉：** 取源只成功 7/10（IEEE 返回 0 字节、ACM 与 MDPI 403），于是有了
取源垫底；报告写着 `partial` 而语料其实建好了，于是有了 `final_status`。

## 已知限制

- `LocatorCache` 默认只在进程内，重跑会重新下载；转换没有缓存，重跑要重付视觉调用；
- `_index` 一次性编码全部句子，篇数上去后是配额压力最大的一段；
- 同分论文之间没有相关性信号。打分只有四档，交付名额几乎总在某一档内部被截断——实测一轮里
  8 篇满分直接入选，剩下 5 个名额由 35 篇同为 0.45 的论文争夺。`tie_break()` 现在用稳定散列
  取代了字典序，消除了系统性偏置（详见 [PaperScout 文档](paper_scout_agent_ch.md)），但它只
  保证"不系统性排除"，单轮的构成仍然是小样本抽取。要真正区分同档论文得让打分器给更细的分数。
