# Paper Source 上游契约

面向读者：开发 AcademicSurvey（SPAR + GEPA）检索侧的人，以及任何想给 paper_source 供料
的模块作者。本文只讲**交接面**——你需要交出什么、能依赖什么、结果怎么读回来。
paper_source 内部怎么取源见[工具说明](paper_source_tool_ch.md)。

## 交接面

你交出一批「已经判定为相关」的论文，每篇至少带一个非标题标识符；paper_source 交还一批
版本固定的正文字节和可直接执行的转换请求。

```text
检索 + 相关性判定  ──→  论文批次（身份 + 线索 + 策略）
                              │
                              ▼
                        paper_source
                              │
                              ▼
              逐篇结果（含转换请求引用）  ──→  paper_markdown
```

检索侧不需要做的事：清洗 id 写法、解析 arXiv 版本、判断链接是不是真的能下、控制限流。
这些都在下游。检索侧**必须**做的只有一件：给出至少一个真标识符。

## 一、身份：唯一的硬性要求

`identity` 接受你能拿到的任何一种标识，多给不冲突，会一起用于交叉校验与补全。

| 字段 | 说明 |
| --- | --- |
| `arxiv_id` | arXiv 标识，任意写法 |
| `doi` | DOI，任意写法 |
| `s2_paper_id` | Semantic Scholar 的 paperId |
| `openalex_id` | OpenAlex work id，如 `W4406604119` |
| `pmid` / `pmc_id` | PubMed / PMC 标识 |
| `title` | **仅用于交叉校验，不作为查询键** |

### 写法不必清洗

下面这些写法都会被归一到同一个裸 id，你直接把检索通道给的原样传过来即可：

- `arXiv:2501.10120v2`、`http://arxiv.org/abs/2501.10120v2`、`oai:arXiv.org:2501.10120`、
  `2501.10120v2.pdf` → `2501.10120`
- `hep-th/9901001v3`（旧式带斜杠 id）→ `hep-th/9901001`
- `https://doi.org/10.1145/X`、`doi:10.1145/X` → `10.1145/x`

版本号会被剥掉——版本由 paper_source 自己向 arXiv 解析，你给的版本不作数（检索通道给的
版本经常是陈旧的）。无法识别的写法会被置空而不是报错，最终表现为「缺少该标识符」。

### 只有标题的条目会被直接拒绝

这是入口处的硬校验，会抛出验证错误使整批请求失败，不是一条诊断。

理由：SPAR 在拿不到任何 id 时会退化为用标题的 md5 作为主键，而标题匹配正是上游元数据出错
的主要来源——同名论文、预印本与正式版标题微调、OCR 出的标题截断，都会让「按标题找论文」
悄悄取回错误的正文。**取错正文比取不到正文糟糕得多**：取不到会报错，取错了会静默进入
RAG 并污染引用。

你的处理方式应该是：在合并检索结果时就把无 id 的条目单独分流，要么补一次 id 查询，要么
标记为「不可取源」并排除出批次，不要指望下游兜底。

### paper_key：你能依赖的稳定性承诺

`paper_key` 是 RAG chunk 主键的命名空间，优先级固定为：

> 期刊 DOI > arXiv id > Semantic Scholar > OpenAlex > PubMed

两条重要性质：

**arXiv 自铸的 `10.48550/` DOI 不参与 DOI 优先级。** 它只是 arXiv id 的另一种写法，而且
只有 2022 年之后的投稿才有。若让它参与优先级，同一批语料会按投稿年份分裂成两个命名空间
（旧论文进 `arxiv:` 空间，新论文进 `doi:` 空间），去重立刻失效。这类 DOI 会被反推成
arXiv id 使用。

**key 只由你给的身份决定，不受下游补全影响。** 例如 `hep-th/9901001` 在取源过程中会补到
出版商 DOI `10.1143/ptp.101.1155`，这个 DOI 会写进返回的 `identity` 和元数据，但
`paper_key` 仍然是 `arxiv:hep-th/9901001`。这意味着：**同样的输入身份永远得到同样的
key**，你可以在发请求之前自己算出 key 来做去重和缓存判断，不必等结果回来。

反过来说，如果你在两次运行中对同一篇论文给出了不同的身份组合（第一次只有 arXiv id，
第二次补上了期刊 DOI），key 会变。跨批次去重要以你自己合并后的身份为准，不要中途改主意。

## 二、每篇论文能带的其他信息

一篇论文的完整输入除 `identity` 外还有四个字段。**它们的消费程度差别很大**，下表是准确
的现状，请按此预期：

| 字段 | 是否被消费 | 实际用途 |
| --- | --- | --- |
| `upstream_metadata` | 是 | 复制进转换请求的元数据，最终进入 RAG；部分键会被解析结果覆盖 |
| `hints` | 部分 | 只有 `oa_pdf` / `publisher_pdf` 两类的 URL 会进下载候选队列 |
| `retrieval_channels` | 是 | 逗号拼接后写入元数据 `retrieval_channels`，供溯源 |
| `matched_queries` | **否** | 仅随请求存档，取源逻辑不读取 |

批次级还有一个 `corpus_ref`，指向上游语料 artifact，同样**仅作溯源存档**，不参与取源。

### 元数据的键名冲突

`upstream_metadata` 的键会原样带下去，但下面这些键属于 paper_source，你写了也会被覆盖：

`paper_key`、`retrieval_channels`、`source_locator`、`source_channel` 总是被覆盖；
arXiv 解析命中时，`title`、`authors`、`arxiv_id`、`categories`、`published`、`updated`
也会被解析结果覆盖，`doi` 与 `license` 在解析结果里存在时覆盖。

其余自定义键（如 `venue`、`year`、`relevance_score`、`citation_count`）原样保留并一路传到
`PaperContent.metadata`，RAG 侧可以读到。想把检索阶段的判定依据带进索引，就放在这里。

### 线索（hints）的语义

线索是「检索时顺带看到的下载链接」，用于**减少解析请求**，不是权威来源：

- `is_open_access` 与 `license` 记录但**不被采信**——是否真能下取决于实际响应，许可以
  arXiv 官方记录为准；
- URL 必须是 `http`/`https`，其余协议会被拒绝并记一条警告（不是静默丢弃，就是要让你看到）；
- 下载到的字节仍要过魔数判定，指向 HTML 落地页的「PDF 链接」会被判为无法识别；
- 上游线索与 OpenAlex 反查到的链接**合起来最多尝试 3 个**，多给无害但不会被访问。

有 arXiv id 时，线索基本不会被用到——arXiv 通道优先且几乎总能成功。线索的真正价值在
非 arXiv 论文上。

## 三、批次与顺序

`papers` 是有序的，**顺序即优先级**。`max_papers` 截断从尾部发生，被丢弃的部分记一条批级
警告。所以把你最想要的论文排在前面。

### 批次大小怎么定

请求数是 **⌈N/60⌉ + N**（默认策略、全为 arXiv），节奏是每 3 秒一次请求。几个实际数字：

| 批次 | 请求数 | 耗时约 |
| --- | --- | --- |
| 20 篇 | 21 | 1 分钟 |
| 50 篇（默认上限） | 51 | 2.5 分钟 |
| 60 篇 | 61 | 3 分钟 |
| 200 篇 | 204 | 10 分钟 |

元数据端点一次最多 60 个 id，所以**按 60 篇切批**能让批量查询满载（默认上限是 50，要
用满需要显式把 `max_papers` 调到 60）。开启许可获取会变成 ⌈N/60⌉ + 2N，因为 OAI-PMH
每次只能取一篇——只在确实需要逐篇许可信息时才打开。

重跑同一批语料时命中本地缓存的论文完全不发下载请求，所以迭代调试的代价远低于首次。

### 不要并行调用

工具声明为非并发安全。限流器是实例级的，同时跑多个实例只会让两边都排队，而且会违反
arXiv「同一时刻只保持一个连接」的要求。需要提速就增大批次，不要增加并发。

## 四、策略归谁定

策略是批次级的，由**发起取源的编排层**设置，不是检索模块的输出。检索模块通常不需要碰它，
除非你在做实验：

| 字段 | 默认 | 什么时候需要改 |
| --- | --- | --- |
| `prefer` | `tex` | 要图像回退时用 `both`（TeX 之外额外抓一份 PDF） |
| `allow_unpinned_version` | `false` | 几乎不该改；改了正文会随时间漂移 |
| `allow_non_arxiv_channels` | `true` | 想把语料严格限定在 arXiv 时关掉 |
| `allow_paid_content_api` | `false` | 有 OpenAlex API key 且愿意计费时 |
| `fetch_license` | `false` | 需要逐篇许可信息时；代价是请求数翻倍 |
| `max_papers` | `50` | 按上表调整批次 |
| `visual_policy` / `chunking` | 继承默认 | 原样透传给 paper_markdown |

`visual_policy` 值得注意：默认是 `required`，若运行时没有配置视觉解释器，paper_markdown
会直接报错而不是降级。做无视觉环境的批量取源时应设为 `best_effort`。

## 五、结果怎么读回来

`records` 与你的 `papers` **一一对应且同序**，每条带 `ref_index` 指回输入下标。截断掉的
部分不会出现在 records 里，靠 `stats.requested` 与 `stats.accepted` 的差值发现。

对齐上游条目用 `ref_index`；跨批次对齐用 `paper_key`。

三种状态的处理建议：

- `fetched` —— 拿到字节，`conversion_request_ref` 可直接交给 paper_markdown；
- `failed` —— 试过但所有通道都失败，**数据源问题**，可以重试或降级为只用摘要；
- `skipped` —— 被策略拒绝，根本没发请求，**配置问题**，重试不会有任何变化。

把这两者混为一谈是最常见的误用：对 `skipped` 重试只会浪费时间。

### 三条稳定性承诺

1. 单篇失败不会让整批失败。唯一会让整批失败的是入口校验（如只有标题的条目）。
2. `records` 的顺序与输入一致，不会因取源快慢重排。
3. 同样的输入身份永远得到同样的 `paper_key`。

### 三个不要假设

1. **返回的 `identity` 不等于你传入的 `identity`**——会被补全（多出 DOI 等）。
2. **`metadata` 里你写的键不一定还在**——见上文的覆盖规则。
3. **有 `conversion_request_ref` 不代表转换一定成功**——那由 paper_markdown 决定，取源
   只保证字节是一个已识别的格式。

## 六、反馈回路：哪些诊断是你该修的

诊断分散在批级 `diagnostics` 和每篇的 `record.diagnostics` 里。下面这几条直接指向上游
的问题，值得在检索侧建立监控：

| 诊断码 | 说明什么 | 检索侧该做什么 |
| --- | --- | --- |
| `paper_source.title_mismatch` | 你给的标题与解析出的真实标题相似度低于 0.75 | 高频出现说明跨通道合并把不同论文合成了一条，检查合并逻辑 |
| `paper_source.url_scheme_rejected` | 线索 URL 不是 http(s) | 链接提取有 bug，可能混进了本地路径或相对 URL |
| `paper_source.http_error`（线索通道） | 你给的下载链接返回非 2xx | 链接已失效，或需要登录；考虑不再输出该来源 |
| `paper_source.payload_not_recognized` | 下载到的既不是 TeX 也不是 PDF | 多半指向 HTML 落地页而非直链 PDF |
| `paper_source.no_channel_available` | 一个通道都没得试 | 身份信息不足，检索侧应补 id 或提前排除 |
| `paper_source.max_papers_truncated` | 批次超限被截断 | 批次切分逻辑与 `max_papers` 不一致 |

其余诊断（arXiv 无源码、OpenAlex 无记录、退避重试等）是数据源的客观事实，检索侧无须处理。

`title_mismatch` 特别值得盯：它是**唯一能发现「取错论文」的信号**。相似度低于 0.75 时
paper_source 仍会继续取源（因为查询用的是 id，id 通常比标题可靠），但如果这条诊断在某个
检索通道上集中出现，那个通道的元数据合并大概率有问题。

## 七、SPAR 各通道该映射到哪个字段

SPAR 从 Google / arXiv / OpenAlex / Semantic Scholar / PubMed 多源聚合，合并后每条论文
通常已经带有若干标识符。映射建议：

| 检索通道 | 首选字段 | 备注 |
| --- | --- | --- |
| arXiv | `arxiv_id` | 直接传原始写法，不要自己剥版本 |
| OpenAlex | `openalex_id` + `doi` | 两个都给；OpenAlex 记录里的 DOI 常是期刊 DOI，优先级更高 |
| Semantic Scholar | `s2_paper_id` + `doi` + `arxiv_id` | S2 的 `externalIds` 里三者常同时存在，全部传下去 |
| PubMed | `pmid` / `pmc_id` + `doi` | 生物医学论文很少有 arXiv 版本，DOI 是主键 |
| Google / 网页 | 从落地页解析出的 `doi` 或 `arxiv_id` | 解析不出 id 的条目应排除，不要只传标题 |

合并后的开放获取链接放进 `hints`，类型标 `oa_pdf`；出版方直链标 `publisher_pdf`；
arXiv 摘要页链接可以不传（有 arXiv id 就够了，摘要页不参与下载）。

**md5(title) 这类兜底键绝对不要放进任何 id 字段。** 它不是标识符，放进去只会让 paper_source
拿一个假 id 去查询，得到「查不到」而不是「身份不足」，掩盖真正的问题。

## 八、一个完整的最小请求

```json
{
  "papers": [
    {
      "identity": {
        "arxiv_id": "arXiv:2501.10120v2",
        "title": "PaSa: An LLM Agent for Comprehensive Academic Paper Search"
      },
      "upstream_metadata": {
        "year": "2025",
        "venue": "arXiv",
        "relevance_score": "0.91"
      },
      "retrieval_channels": ["arxiv", "semantic_scholar"],
      "matched_queries": ["LLM agents for academic paper search"]
    },
    {
      "identity": {
        "doi": "https://doi.org/10.1145/3477495.3531937",
        "openalex_id": "W4285719821"
      },
      "hints": [
        {
          "url": "https://dl.acm.org/doi/pdf/10.1145/3477495.3531937",
          "kind": "publisher_pdf",
          "channel": "openalex"
        }
      ],
      "retrieval_channels": ["openalex"]
    }
  ],
  "policy": {
    "prefer": "tex",
    "max_papers": 60,
    "visual_policy": "best_effort"
  }
}
```

第一篇会走 arXiv 源码通道拿到 TeX，`paper_key` 为 `arxiv:2501.10120`；第二篇没有 arXiv
版本，会先查 OpenAlex 再尝试出版方直链，`paper_key` 为 `doi:10.1145/3477495.3531937`。

## 九、常见错误清单

| 错误 | 后果 |
| --- | --- |
| 只传标题 | 整批请求在入口被拒 |
| 把 md5(title) 兜底键当 id 传 | 查询失败，且掩盖了「身份不足」这个真实原因 |
| 自己剥版本或拼 URL | 白做；写法归一和版本解析都在下游 |
| 把 `arxiv_abs` 链接当下载线索 | 不会被使用（有 id 就够了） |
| 并行调用多个取源实例 | 违反 arXiv 单连接约束，且不会更快 |
| 对 `skipped` 状态重试 | 配置问题，重试结果完全相同 |
| 用 `metadata["title"]` 传自定义标题 | arXiv 命中时会被解析出的真实标题覆盖 |
| 依赖返回的 `identity` 与输入相同 | 会被补全，跨批次去重应以 `paper_key` 为准 |
