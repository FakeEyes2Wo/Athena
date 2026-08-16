# paper_rag 真机基准

Status: current
Owner: Athena maintainers
Last verified: 2026-08-16
Source of truth: `src/athena/research/paper_rag/`, 实测数据见本文各节

2026-08-16 在真实语料上测了七个检索算子的**性能**与**质量**。所有数字来自
`~/.athena/artifacts` 里的真实语料，不是构造数据；模型是 qwen3.7-plus，编码器是
qwen3.7-text-embedding（1024 维）。

接口语义见 [paper_rag 工具](paper_rag_tool_ch.md)。

## 语料

| 语料 | 论文 | chunk | 句子 | 用途 |
|---|---|---|---|---|
| A | 44 | 4 928 | 36 920 | 压力测试；同一份内容同时存有 1.0（JSON）与 1.1（float32）两种编码 |
| B | 8 | 729 | 8 666 | 生产尺寸；`--survey-papers 8` 真跑出来的那一份 |

两个语料主题相同：不平衡表格分类与 AUC 优化。**因此本文所有质量数字都只能读作该主题
下的表现，不能外推成领域无关结论。**

## 一、装载：float32 相对 JSON

语料 A 的两种编码内容完全一致，是干净的 A/B。各自在独立进程里测量。

| 阶段 | JSON 1.0 | float32 1.1 | 变化 |
|---|---|---|---|
| 索引解析（不含向量） | 0.118 s | 0.122 s | — |
| 向量装载 | 12.17 s | 0.32 s | **快 38 倍** |
| 峰值常驻内存 | 2 767 MB | 517 MB | **低 5.4 倍** |
| 热重载（已缓存） | 13 µs | 11 µs | — |

峰值差距的来源是 `json.loads` 的中间对象。落盘体积同步从 838 MB 降到 151 MB。

索引解析与向量装载分开计时不是修辞：关键词检索、章节检索、整篇读取都**不**触发向量
装载，只有 `paper_semantic_search` 会传 `vectors=True`。

## 二、逐算子时延

穿过工具边界测量，所以这就是 Ideator 每次调用实际付的钱。

| 算子 | p50（语料 A） | p95（语料 A） | p50（语料 B） |
|---|---|---|---|
| `paper_corpus_overview` | 1.89 ms | 2.25 ms | 26.5 ms（冷） |
| `paper_keyword_search` | 15.28 ms | 19.41 ms | 2.63 ms |
| `paper_semantic_search` | 155.83 ms | 1 320.72 ms | 143.95 ms |
| `paper_section_search` | 11.64 ms | 12.86 ms | 1.52 ms |
| `paper_visual_of` | 0.16 ms | 0.59 ms | — |
| `paper_cites` | 0.03 ms | 0.18 ms | — |
| `paper_chunk_read` | 0.02 ms | 0.10 ms | 0.06 ms |

`paper_semantic_search` 是唯一有可见成本的算子，而且**成本几乎不在我们这边**。拆成两段
测（20 次采样）：

- 编码 API 往返：p50 **136.8 ms**
- 本地打分（36 920 × 1024 float32 矩阵乘 + 分组）：p50 **6.66 ms**
- **API 占单次调用的 96.1%**

p95 的 1.3 秒是进程内第一次调用，它额外付了一次性的向量装载。

放到整轮里看：一次真实 Ideator turn 161.7 秒，5 次 `paper_*` 调用合计 **333 ms，占
0.2%**。检索层不是瓶颈，模型往返才是。

## 三、质量：known-item 检索

12 条查询，每条都是**刻意避开目标论文标题用词**的改写，防止关键词检索靠回抄标题取胜。
金标是 paper id，且**评分前先核验**每个金标确实出现在语料里——这一步当场抓出一个错标：
《When AUC meets DRO》通篇用 KL 散度与 CVaR，从不出现 "Wasserstein"，它不该被列为该题
的答案。

按返回结果里**首个金标论文的名次**（去重后，k=10）：

| 通道 | hit@1 | hit@3 | hit@5 | 未命中 | MRR |
|---|---|---|---|---|---|
| `paper_semantic_search` | **11 / 12** | 11 / 12 | **12 / 12** | **0** | **0.938** |
| `paper_keyword_search` | 3 / 12 | 5 / 12 | 6 / 12 | 6 | 0.336 |

结论直说：**面对改写过的问题，关键词检索不能替代语义通道。** 它在 Agent 已知确切术语时
仍是对的工具，但一半情况下跨不过措辞差异。

> 早先用"片段是否含主题词"打分时两个通道都是 P@5 = 1.00。那个指标对关键词检索近乎同义
> 反复（它本来就是按那些词匹配的），换成 known-item 才有区分度。这里保留这段是为了说明
> 指标选择本身会决定结论。

## 四、关键词检索的短语失效（可修）

多词关键词按**字面子串**匹配，因此没有逐字出现过的短语返回空结果。27 条自然多词关键词
里 **7 条（26%）返回 0 命中，而这 7 条全部在拆成单词后有结果**。

| 关键词 | 短语命中 | 拆词命中 |
|---|---|---|
| `Wasserstein ball` | 0 | 5（全在那篇讲它的论文里） |
| `ambiguity set` | 0 | 5 |
| `false positive rate range` | 0 | 5 |
| `self attention` | 0 | 5 |
| `minority class oversampling` | 0 | 5 |
| `label noise robustness` | 0 | 5 |
| `worst case` | 0 | 5 |

失败是静默的，Agent 无法与"语料里确实没有"区分。连字符同样致命：`cross validation` 与
`cross-validation` 各返回 5 条，但**论文集合完全不相交**。

建议修法：短语命中为 0 时回退到词级匹配。

## 五、`paper_corpus_overview` 的摘要字段

`PaperSummary.abstract` 取锚点 chunk 开头 280 字。实测语料 A：

- **44 篇里 22 篇**，这 280 字里真正的正文散文**不足 40 字**——额度花在标题（`title`
  字段已经有了）、作者列表上，有一篇甚至是 LaTeX 导言区
  （`\definecolormydarkbluergb0,0.08,0.45 pdftitle=…`）。
- **44 篇里 20 篇没有 `abstract` kind 的 chunk**，锚点回退到首个 chunk；其中 3 篇回退成
  了**表格**，1 篇回退成插图。
- 语料 B 同样只有 3/8 篇有 `abstract` chunk。

建议修法：跳过前置区块，从摘要正文起算；标题已在 `title` 字段，不必重复。

体积参考：44 篇的 overview 载荷约 30 700 字符（≈7 700 token），8 篇约 1 500 token。

## 六、章节遍历的覆盖率

工具描述建议用 `paper_section_search` 找 Limitations / Ablation 来获取反面证据。同义词
展开之后，这条建议对常见章节成立，对它点名的那两个基本落空：

| 章节 | 语料 A（44 篇中） | 语料 B（8 篇中） |
|---|---|---|
| Related Work | 20 | 5 |
| Experiments | 20 | 5 |
| Conclusion | 20 | 5 |
| Ablation | 9 | **0** |
| Limitations | 3 | 2 |

引用边同样稀疏：语料 B 的 8 篇之间只有 11 条引用边。图文互链正常（406 条）。

"用章节检索和引用反向边找反面证据"这条叙事需要按这些数字重新审视。

## 七、优化清单

按性价比排序，每条都对应上文一个测量。

1. **关键词短语回退到词级匹配** —— 恢复 7/7 条实测死短语（第四节）。
2. **overview 摘要跳过前置区块** —— 修好 22/44 条几乎为空的摘要（第五节）。
3. **向量 artifact 走 mmap** —— 现路径先读 151 MB 字节再解码成 151 MB 数组，工作集
   333 MB；`numpy.load(path, mmap_mode="r")` 0.8 ms 打开、常驻 29.8 MB，首次查询触页后
   稳定在 186 MB。每个语料省约 147 MB，且在内存压力下可被换出。
4. **缓存查询向量** —— 语义检索 96% 的时间是一次 API 调用，跨 lane、跨轮次的重复查询
   每次都重新付费。
5. **重新审视反面证据的取证路径** —— Ablation 在真实调研语料上覆盖 0 篇（第六节）。

## 复现

基准脚本不在仓库内（属于一次性测量），关键参数记录于此以便重跑：

- 语料 A 的两种编码：`schema_version` 1.0 / 1.1，同 44 篇内容，`embedding_format`
  分别为 `json` / `float32`。
- 时延采样：overview 冷 1 次 + 热 5 次；关键词与语义各 12 条查询；章节 5 个标题；
  遍历与整篇读取取自前两者返回的 chunk。
- known-item：12 条改写查询，金标先按"论文正文是否含判别词"核验，再计 hit@k 与 MRR。
- 内存：`K32GetProcessMemoryInfo` 读 `WorkingSetSize` / `PeakWorkingSetSize`，
  每种编码各起独立进程。

## 相关文档

- [paper_rag 工具](paper_rag_tool_ch.md) — 算子接口与索引结构
- [文献语料接入 loop](corpus_ideation_ch.md) — 这些算子在 loop 里怎么被用
- [paper_markdown 质量](paper_markdown_rag_quality_ch.md) — 上游转换的质量门禁
