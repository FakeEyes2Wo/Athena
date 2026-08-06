# Paper Markdown Tool：PaSa RAG 质量问题与修复方案

本文档是 `paper_markdown` 工具的产物质量契约。它记录 PaSa TeX 回归暴露的问题、
确定性修复决策和验收标准。范围仅限论文 Markdown 工具；Web Search、Athena 运行时
注册和具体模型供应商接入不在本文档范围内。

## 审计基线

审计对象为 `PaSa_2501.10120_source.tar.gz` 经以下正式工具链生成的产物：

```text
ToolRegistry.resolve("paper_markdown").ainvoke(request_ref=...)
  -> PaperMarkdownTool.execute
  -> PaperProcessor.process
  -> LocalArtifactStore
```

基线 `PaperContent` 引用为：

```text
sha256:dd3a498795a7586920d4256ccc08d0c58061e3f7c140f53092d0cd6a2c4cae5b
```

基线已正确恢复 7 位作者、37 条参考文献、20 张表格和 3 张图片，但不满足生产 RAG
质量要求。

## 已确认问题

### 1. 标题与附录结构损坏

- 8 个 `\paragraph{...}` 被转换为空 `#####`，标题文字落入普通段落。
- 6 个 chunk 的 `heading_path` 含空字符串。
- `\appendix` 没有形成结构边界，附录第一张表继承 `References` 路径。

影响：章节过滤失效，附录内容与参考文献混淆，查询可能返回错误上下文。

### 2. Bibliography 与 chunk 边界污染

- 完整 bibliography 被作为一个约 2,304 token 的元素索引。
- overlap 将 8,837 字符的 bibliography 完整复制到附录表格 chunk。
- 污染后的 chunk 长 13,090 字符，并错误携带全部 37 个 citation keys。
- 7 个父章节标题形成 11--40 字符的孤立 chunk。
- 6 个 chunk 合并多个视觉对象，最多合并 5 张表格。

影响：重复召回、citation filter 误命中、上下文槽位浪费和检索粒度过粗。

### 3. 表格 Markdown 与列语义不可靠

- 8/20 张表格在单元格中写入裸换行，后续行脱离 Markdown 表格。
- `multicolumn` 布局信息被丢弃，层级表头出现空列。
- `ablation` 有 4 个空表头；超参数表的真实数值落在无标题列中。

影响：Markdown/AST 切分器无法稳定恢复行列关系，数值与指标可能错误关联。

### 4. Retrieval unit 重复且缺少质量元数据

- 20 张表格的 17,026 字符结构文本同时出现在正文 chunk 和视觉 retrieval unit。
- chunk overlap 与视觉重复合计至少占待索引字符的 26.6%。
- 23 个视觉 retrieval unit 都缺少 `heading_path` 和父 chunk 关系。
- retrieval metadata 不包含解释模型或 `interpretation_status`。
- `best_effort` 下全部视觉解释均为 `deterministic-evidence-only`，但消费者无法直接
  从 retrieval unit 判断其降级状态。

影响：重复结果挤占 top-k，视觉结果不能按章节过滤，降级证据可能被误当成模型解释。

### 5. 质量门禁覆盖不足

基线诊断只包含 `visual_interpretation_unavailable`。空标题、无效表格、跨章节 overlap、
超长 chunk 和重复索引均未被质量状态反映。artifact 完整性不能替代 RAG 质量验证。

## 修复决策

### TeX 结构

1. 为所有 section 类宏声明带星号、可选短标题和必需标题的参数规格。
2. 将 `\appendix` 转换为显式 `## Appendix`，附录内 section 下移一级并保留
   `Appendix` 祖先路径。
3. 将 bibliography 拆为显式 References 标题和逐条 bibliography 元素，使 chunker
   能按目标大小安全分组并保留每条 citation key。
4. 空标题不得进入正文；若仍出现，必须产生稳定诊断。

### 表格

1. 单元格内部换行统一转换为 `<br>`，保证每个 Markdown 表格行只占一行。
2. 保留 `multicolumn` 跨列宽度，用父表头填充跨列，并把两层表头扁平化为
   `父表头 / 子表头`。
3. 所有输出表格必须满足：列宽一致、表头非空、分隔行列数一致。
4. 无法安全恢复时保留 TeX fenced block，并产生诊断；不得输出看似成功的坏表格。

### Chunk

1. 章节变化对任意元素生效，不只依赖 heading 元素。
2. overlap 只能发生在同一 heading path 内，且只复用 paragraph/list/abstract；
   bibliography、table、figure、equation 和 code 不参与 overlap。
3. 连续父子 heading 与其首个内容合并，禁止生成只有标题的微小 chunk。
4. bibliography 按完整条目分组；不得在条目中间截断。
5. table/figure 各自形成独立 chunk；章节标题可与紧随其后的首个视觉元素合并。
6. 每个 chunk 的 citation keys、visual IDs 和 heading path 只能来自实际包含的元素。

### Retrieval 与质量状态

1. `PaperVisual` 持久化 `element_id`、`heading_path`、父 `chunk_ids`、解释模型和解释状态。
2. `best_effort` 且解释不可用的表格不再生成重复视觉 retrieval unit；正文表格 chunk
   是其唯一文本检索单元。
3. 有新增解释的视觉单元继续独立索引，并携带父 chunk 与章节元数据。
4. `PaperContent` 暴露 `quality_status` 和 `quality_codes`。任何 warning/error 都产生
   `degraded`，旧 schema 产物读取时为 `unknown`。
5. `best_effort` 可以生成降级产物，但不得被标记为通过；`required` 仍要求视觉解释器。

## 验收标准

单元与工具级测试必须证明：

- 不存在空 Markdown 标题或空 heading path 分量；
- PaSa 包含显式 Appendix，附录首表路径为 `Appendix`，不携带 bibliography citations；
- bibliography 不跨章节 overlap，单个 citation key 只归属实际包含该条目的 chunk；
- 没有仅含标题且小于 100 字符的 chunk；
- 每个 chunk 最多包含一个 visual ID；
- 20 张 PaSa 表格均为列宽一致、无裸单元格换行的有效 Markdown；
- 层级表头无空列，`ablation` 能明确区分两个数据集的指标；
- best-effort PaSa 不重复创建 20 个原始表格视觉 retrieval unit；
- 视觉单元具有 heading path、父 chunk、模型和解释状态；
- `PaperContent.quality_status == "degraded"`，且质量代码明确包含视觉解释降级；
- 7 位作者、37 条参考文献、3 张图片预览及所有源码定位保持不变；
- 全量测试、Ruff、编译和 `git diff --check` 通过。

## 二次审计：结构通过不等于可直接索引

首次修复产物的 `PaperContent` 引用为：

```text
sha256:a2ce56608bc7a8a07b1e7291fe3ed4dd44fdefa051b6a384c4b064f57b335450
```

该产物通过了前述结构验收，但按“retrieval unit 能否直接进入多论文索引并提供可靠
证据定位”复核后，仍确认以下问题。

### 6. 元素标识不唯一，字符区间不可完全回放

- 204 个解析元素只有 150 个唯一 `element_id`；20 组重复 ID 覆盖 74 个元素。
- 62 个 chunk 中有 4 个不能用 `markdown[char_start:char_end]` 回放实际 chunk 正文。
- 根因是同一个 TeX 节点 buffer 拆成多个段落后仍使用相同源码和位置生成 ID；标题和
  作者也复用了同一节点。`full_markdown` 以 ID 保存 span 时，较早元素被较晚元素覆盖。

影响：高亮、证据引用和按字符定位回原文不可靠，依赖 `element_id` 的后续关联存在覆盖
风险。

### 7. Retrieval unit 缺少文档身份和结构元数据

- 62 个正文 retrieval unit 的 metadata 只有 `visual_ids`。
- `paper_id`、标题、作者、来源、质量状态、citation keys 和 labels 没有传入统一检索单元。
- 3 个视觉 retrieval unit 同样缺少文档身份和质量状态。

影响：多论文索引难以归属、过滤和引用命中结果；虽然信息仍在 `PaperContent`，但
`load_retrieval_units()` 的“可直接索引”契约没有成立。

### 8. 不可用视觉解释仍产生重复索引

- 23 个视觉对象均为 `interpretation_status=unavailable`。
- 3 个 figure retrieval unit 与对应 figure chunk 的五词片段包含率为 100%，额外重复
  1,104 字符，且没有提供 caption 之外的新信息。
- 训练曲线和搜索树中的趋势、节点与路径没有进入检索文本。

影响：重复结果挤占 top-k；缺少视觉模型时，caption fallback 不能被误当成独立视觉理解。

### 9. Bibliography 元素在索引层再次被粗粒度合并

虽然 parser 已拆出 37 个 bibliography 元素，chunker 仍按 6,000 字符目标将其组合为
两个 chunk，分别包含 25 和 12 条引用。查询单篇参考文献时会携带大量无关文献。

### 10. 展示公式保留了失效的 eqnarray 片段

PaSa 的 8 个展示公式中，5 个在裸 `$$` 内保留 `&`、`\\` 或 `\nonumber` 等
`eqnarray` 对齐命令，6 个仍把 `\label` 写入公式正文。不同 Markdown/KaTeX 渲染器
可能报错，公式检索文本也包含非语义噪声。

### 11. 二次质量门禁缺口

首次门禁没有检测 element ID 唯一性、字符区间回放、retrieval metadata 完整性、
不可用视觉重复、bibliography 检索粒度和公式环境合法性，因此不能仅凭原
`run_manifest.validation` 判定可投产。

## 二次修复决策

### 标识与证据区间

1. 同一源码 buffer 拆出的每个段落必须使用稳定的 part 序号参与 `element_id`；由同一
   节点派生的标题和作者必须使用不同身份后缀。
2. chunk 持久化前必须验证 element ID 全局唯一。
3. 去掉人为添加的 `> Section: ...` 前缀后，每个非 overlap chunk 必须能由全文声明的
   `[char_start:char_end]` 精确回放；失败时产生稳定质量诊断。

### Retrieval metadata

1. 所有 retrieval unit 都携带 `paper_id`、`title`、`authors`、`source_kind`、
   `source_ref`、`quality_status` 和 `quality_codes`。
2. 正文单元额外携带 `citation_keys`、`labels` 和 `visual_ids`；视觉单元保留 element、
   父 chunk、模型和解释状态。
3. metadata 继续使用轻量字符串值，不在每个单元复制全文或大型结构对象。

### 视觉去重

1. 任何 `interpretation_status=unavailable` 的视觉对象都不创建独立视觉 retrieval unit；
   对应 figure/table chunk 保留 caption、结构文本和 artifact 关联。
2. 只有实际产生新语义的 `interpreted` 视觉对象才创建独立视觉 retrieval unit。
3. 缺少视觉解释仍保持 `degraded`，不得因去重而隐藏质量状态。

### Bibliography 粒度

1. 每条 bibliography 元素形成独立 retrieval chunk，不与相邻条目或附录合并。
2. 每个 bibliography chunk 只携带该条目的 citation key；首条可与 References 标题合并。

### 公式 Markdown

1. `equation` 保留为普通 `$$...$$`；`align`、`aligned`、`eqnarray` 等多行环境统一转换为
   `$$\begin{aligned}...\end{aligned}$$`。
2. `eqnarray` 的 `&=&` 规范为单个关系符对齐 `&=`，行首 `&&` 规范为 `&`。
3. 从公式正文移除 `\label{...}` 和 `\nonumber`，但 label 继续保存在 chunk metadata。

## 二次验收标准

- 所有解析元素的 `element_id` 唯一；PaSa 重跑不再出现重复 ID；
- 所有 PaSa chunk 的字符区间均能精确回放正文；
- 所有 retrieval unit 都具有文档身份、来源和质量 metadata；
- 正文 retrieval unit 保留 citation keys、labels 和 visual IDs；
- best-effort 且解释不可用时不生成任何独立视觉 retrieval unit；
- 37 条 PaSa bibliography 形成 37 个独立 chunk；
- 展示公式不含裸 eqnarray 对齐符、`\label` 或 `\nonumber`；
- 新质量门禁可检测人工构造的重复 element ID 和错误字符区间；
- 首次修复已通过的标题、附录、表格、chunk 边界和 artifact 完整性不得回退；
- 全量测试、Ruff、编译和 `git diff --check` 通过。

## 三次审计：多论文主键与引用图谱

二次修复后的 PaSa 产物引用为：

```text
sha256:15ab8d625ea2fc3f58981c35e99452d6172919cab2238869a44541340f85e447
```

该产物的 97 个论文内 retrieval unit 均唯一，37 条 bibliography 已独立分块，20 张
表格可稳定检索，element ID、字符区间、公式和 artifact 完整性也均通过。但是，按
“多个 `PaperContent` 可直接写入同一生产索引，并能建立完整引用图谱”复核后，仍有
以下问题。

### 12. Retrieval unit ID 缺少论文命名空间

`PaperChunk.chunk_id` 只由 Markdown 字符区间与 chunk 正文生成，
`load_retrieval_units()` 又直接把局部 `chunk_id` 或 `visual_id` 用作全局 `unit_id`。
两份来源指纹不同但正文相同的 TeX 会产生相同的 retrieval unit ID。

影响：以 `unit_id` 为主键的向量库可能用后写入论文覆盖先写入论文；metadata 中虽然
存在 `paper_id`，但主键写入发生在 metadata 过滤之前，无法阻止覆盖。

### 13. 活动源码 label 与持久化引用图谱不一致

PaSa 活动源码含 43 个有效 label，产物含 42 个，但集合并不等价：

- `selector_detail` 和 `exp_rst_appx` 两个有效 section label 丢失；
- 注释行中的 `ablation_study` 被错误收录；
- 两处指向 `selector_detail` 的正文引用没有可连接目标。

根因是元素 metadata 从包含 `LatexCommentNode` 的原始 buffer 直接扫描 label；同时，
紧随 section 且不产生可见 Markdown 的独立 `\label` 节点没有归属于标题元素。

影响：按 label 构建的章节跳转、引用图谱和 graph RAG 出现断边，注释内容还会污染
过滤条件。

### 14. 完整多模态质量仍由调用方能力约束

本次产物未注入视觉解释器，23 个视觉对象均明确标记为
`interpretation_status=unavailable`。20 张表格仍有确定性 Markdown 结构文本，但 3 张
图片只有 caption，无法提供曲线数值、趋势和图内关系等额外语义。

这不是下载或模型供应商问题的工具内替代实现点。工具必须继续保留视觉解释注入接口：
`required` 在解释不可用时拒绝产出，`best_effort` 允许生成明确的 degraded 产物；不得
把 caption fallback 伪装为视觉理解。生产多模态验收必须由调用方注入解释器后重跑。

### 15. 论文原文存在低强度重复

未发现五词片段包含率不低于 0.80 的近重复；两个 prompt 表格存在约 0.70 的包含关系，
属于论文原文的有意复用。工具继续保真，不自动删除或合并。若索引器需要控制相似结果
占用 top-k，应在索引策略层建立 canonical/duplicate group，而不是损坏论文内容。

## 三次修复决策

### Retrieval unit 命名空间

1. `PaperChunk.chunk_id`、`PaperVisual.visual_id` 和父子关系继续使用稳定的论文内局部 ID，
   避免破坏已持久化的 artifact 关联。
2. `RetrievalUnit.unit_id` 使用 `paper_id` 作为首选命名空间；上游未提供 `paper_id` 时，
   使用 `provenance.source_fingerprint`。
3. 正文与视觉 retrieval metadata 分别保留原始 `chunk_id` 或 `visual_id`，使消费者可以
   回连 `PaperContent`，并暴露实际采用的 `retrieval_namespace`。

### TeX label 与引用完整性

1. citation/label metadata 扫描前屏蔽行注释和 `comment` 环境，不能从不可见源码生成
   检索过滤值。
2. 紧随 section 类标题、且中间只有空白或注释的独立 `\label` 归属于标题元素，并合并
   label 自身的源码定位。
3. TeX parser 从活动 document AST 独立收集 source labels 和 cross-reference targets，
   供质量门禁与元素 metadata 交叉验证。
4. 活动 label 集与元素持久化 label 集不一致时产生稳定诊断；任何 cross-reference target
   无法在持久化 label 集中解析时产生独立诊断并把产物标记为 degraded。

## 三次验收标准

- 两份正文相同但来源指纹不同的论文不得产生相同 retrieval unit ID；
- 两个不同 `paper_id` 的论文不得产生相同 retrieval unit ID；
- 每个 retrieval unit 都可通过 metadata 中的局部 ID 回连原 chunk 或 visual；
- 注释行和 `comment` 环境中的 citation/label 不进入元素 metadata；
- section 后置 label 即使后面没有正文，也必须归属于对应 heading；
- PaSa 的 43 个活动源码 label 与持久化元素 label 集完全一致；
- PaSa 的所有活动 cross-reference target 均能在持久化 label 集中解析；
- 无视觉解释器的 PaSa 继续保持 `degraded`，且不产生伪视觉 retrieval unit；
- 二次验收已经通过的文本、表格、bibliography、公式和 artifact 完整性不得回退；
- 全量相关测试、Ruff、编译和 `git diff --check` 通过。

## 四次审计：浮动体语义路径与引用边

三次修复后的 PaSa 产物引用为：

```text
sha256:1a9dbb8093bed1684de3f034d7de0a01aa742df2f985df7844cbdc3875771881
```

该产物已通过 artifact 完整性、全局 retrieval ID、label 集和引用目标完整性验收；
97 个 retrieval unit 均可安全进入多论文索引。但是，章节过滤和关系扩展仍没有达到
生产 RAG 要求。

### 16. TeX 浮动体的源码路径不等于语义路径

table/figure 按 TeX 声明位置继承 `heading_path`，但论文常把浮动体声明放在实际讨论
章节之前。PaSa 中至少有以下高置信错配：

- `main_results`、`main_results_real` 被归到 `Experimental Setting / Paper Management`，
  实际由 `Main results` 讨论；
- `ablation` 被归到 `Main results`，实际由 `Ablation study` 讨论；
- `gen_search_query`、`crawler_prompt` 被归到 `Quality Evaluation`，实际由 Crawler 的
  imitation learning 小节讨论；
- `main_results_100`、`ablation_cost` 和 `tab:AutoScholarQuery_prompt` 均继承了相邻但
  语义错误的源码章节。

影响：按 `Experiments / Main results` 过滤时会返回 `ablation`，却排除真正的
`main_results` 和 `main_results_real`。纯 BM25 尚能依靠 caption 召回，但 metadata
过滤、分层检索和 rerank 会产生系统性误差。

### 17. Cross-reference 只有目标完整性，没有结构化边

TeX parser 已收集整篇 `source_reference_keys` 并验证 63 次引用没有断链，但
`ParsedElement`、`PaperChunk` 和 retrieval metadata 均未保存每个正文单元实际引用的
label。消费者只能重新解析 `[label]` 文本，无法可靠区分普通方括号、引用边和公式编号。

影响：不能直接执行“召回正文后展开其表格、公式或附录目标”的关系型 RAG，也无法
对引用边做结构化过滤和图遍历。

### 18. Multicolumn 扁平化仍产生重复语义

为了保持 Markdown 矩形，当前实现把跨列值复制到每个被覆盖的列。PaSa 超参数表因此
出现 `Name | Name | Value`，以及 `learning rate | learning rate | 1e-6`。数值没有错位，
但重复 token 会改变词法权重并给表格问答增加无意义字段。

### 19. 多模态限制保持不变

23 个视觉对象仍为 `unavailable`。这次修复继续只完善工具的确定性结构和持久化契约，
不实现 Athena 运行时或模型供应商；生产多模态产物仍必须由调用方注入视觉解释器。

## 四次修复决策

### 双章节路径

1. 不物理移动浮动体，不改变 Markdown 源顺序、字符区间或源码 locator。
2. TeX 元素保留声明时的 `heading_path` 作为 source path，并为 table/figure 计算独立的
   `semantic_heading_path`。
3. 语义路径只使用确定性引用上下文推断：在同一顶层章节内，选择距离浮动体最近的、
   明确引用其 label 的正文元素路径；没有同顶层候选时保留 source path。
4. 最近候选等距但指向不同路径时不得猜测，保留 source path 并产生稳定的 ambiguity
   诊断。成功推断记录 info 诊断，便于审计但不降低质量状态。
5. `PaperChunk.heading_path` 继续记录 source path 以保持旧产物兼容；新增
   `semantic_heading_path`。`RetrievalUnit.heading_path` 优先使用 semantic path，metadata
   同时暴露 `source_heading_path`。
6. `PaperVisual` 同时持久化 source/semantic 路径，模型解释和视觉检索使用 semantic path。
7. 新产物 schema 提升为 `1.2`；旧 `1.0/1.1` 产物继续以空 semantic/reference 字段读取。

### 结构化引用边

1. 在屏蔽注释后的 TeX 片段中提取 `ref`、`eqref`、`autoref`、`cref` 和 `Cref` 目标，
   按首次出现去重并存入 `ParsedElement.reference_keys`。
2. chunk 只聚合其实际元素包含的 reference keys；overlap 不得引入其他章节的引用边。
3. `PaperChunk.reference_keys` 持久化局部边；正文 retrieval metadata 暴露同名字段。
4. 元素 reference keys 的全集必须与活动 document AST 的 reference targets 一致，否则
   产生稳定诊断并把产物标记为 degraded。

### Multicolumn 去重

1. 解析时记录 colspan continuation，而不是永久复制跨列文本。
2. 两层分组表头仍展开为 `父表头 / 子表头`；没有子表头时，续列使用明确的
   `(continued)` 表头，保证表头非空且不伪造新字段名。
3. 数据行 colspan 只在首列保留文本，其余续列输出空单元格，不重复内容。

## 四次验收标准

- PaSa 的 `main_results`、`main_results_real` retrieval heading path 为
  `Experiments / Main results`；`ablation` 为 `Experiments / Ablation study`；
- 前述 8 个高置信浮动表格均获得与其最近同顶层引用上下文一致的 semantic path；
- Markdown 顺序、chunk 字符区间和 source heading path 均保持可回放；
- 所有正文 retrieval unit 暴露 `reference_keys` 和 `source_heading_path`；
- PaSa 元素 reference key 全集与活动 AST 目标集一致，所有目标继续可解析；
- 超参数表不再包含 `Name | Name` 或 `learning rate | learning rate`；
- 20 张表格继续满足矩形、表头非空和行内无裸换行；
- 无视觉解释器时仍保持明确 degraded，且不产生伪视觉 retrieval unit；
- 前三次验收已经通过的 ID、label、bibliography、公式和 artifact 完整性不得回退；
- 全量测试、Ruff、编译、确定性重跑和 `git diff --check` 通过。

## 五次审计：检索文本与语义章节不一致

四次修复后的 PaSa 产物引用为：

```text
sha256:7cff483d64109170525b2554d4f8a125e4b0f8a56eeedfbe5548cf7d5e7bb62f
```

该产物的 artifact、全局 ID、label/reference/citation 图谱、表格和 semantic heading
metadata 均通过验收，但 `RetrievalUnit.text` 尚未同步使用 semantic heading，不能直接
判定为生产级 RAG 通过。

### 20. Metadata 正确但实际检索文本仍含源码章节

`load_retrieval_units()` 已优先把 `semantic_heading_path` 暴露为顶层 `heading_path`，却仍
直接读取 `chunk.content_ref`。11 个发生语义迁移的 PaSa 浮动体因此全部存在矛盾：

- `main_results`、`main_results_real` 的 metadata 为 `Experiments / Main results`，文本
  前缀仍为 `Experimental Setting / Selector / Paper Management`；
- `ablation` 的 metadata 为 `Experiments / Ablation study`，文本仍以
  `### Main results` 开头；
- `main_results_100`、`ablation_cost` 等附录表也仍携带相邻源码章节。

实际 BM25 探针中，查询 `paper management` 时 `main_results_real` 和 `main_results`
分别错误排到第 2、3 位；查询 `main results` 时 `ablation` 错误排到第 1 位。错误标题
同样会进入向量 embedding，因此不能依靠 metadata 过滤掩盖这个问题。

### 21. 章节质量门禁只检查 metadata

四次验收验证了 semantic/source 双路径和 metadata，但没有验证真正送入 lexical/vector
索引的文本。只要 `RetrievalUnit.heading_path` 正确，旧门禁就无法发现文本中的旧
`> Section:` 前缀或被合并进浮动体 chunk 的 Markdown heading。

## 五次修复决策

### 原始证据与检索副本分离

1. `content_ref` 继续保存源码顺序的 chunk Markdown，不移动浮动体、不修改全文或字符区间。
2. `PaperChunk` 新增可选 `retrieval_text_ref`，保存只面向 embedding、BM25 和 rerank 的
   章节规范化文本。
3. source path 与 semantic path 相同时，检索文本保持原样；两者不同时，检索文本统一以
   `> Section: <semantic path>` 开头，并移除 chunk 开头被合并的源码 Markdown headings。
4. `load_retrieval_units()` 优先读取 `retrieval_text_ref`；读取旧产物时回退到
   `content_ref`，不破坏 schema 1.0--1.2。
5. 新产物 schema 提升为 `1.3`。检索文本是确定性派生制品，不调用 LLM，也不改变
   chunk ID、locator、label、reference 或 citation 图谱。

### 检索章节质量门禁

1. 对 semantic/source 不同的 chunk，检索文本第一条非空上下文必须是完整 semantic path。
2. 检索文本不得继续包含其他 `> Section:` 前缀，也不得包含来源路径中的旧 Markdown heading。
3. 违反约束时产生稳定的 `rag_retrieval_heading_mismatch` warning，使产物进入
   `degraded`，而不是把矛盾文本静默送入索引。

## 五次验收标准

- PaSa 11 个 semantic/source 不同的 chunk 均具有独立 `retrieval_text_ref`；
- 11 个检索文本均以各自 semantic path 开头，且不含旧 source section 前缀或合并标题；
- `main_results`、`main_results_real` 不再因旧前缀响应 `paper management`；
- `ablation` 不再因旧 `### Main results` 标题压过真正的 main-results 正文；
- `content_ref`、全文 Markdown、字符区间、chunk ID 和 locator 保持源码证据语义；
- 人工构造的章节矛盾能触发 `rag_retrieval_heading_mismatch`；
- schema 1.0--1.2 产物仍可读取并回退到 `content_ref`；
- artifact、ID、label/reference/citation、bibliography、表格、公式和视觉降级行为不得回退；
- 全量测试、Ruff、编译、确定性重跑、产物哈希审计和检索探针通过。

## PDF fallback PaSa 验收（2026-07-24）

PDF-only 回归入口为 `../papers/PaSa_2501.10120_paper_markdown_tool_pdf/reproduce_pdf.py`，
同样经过 `ToolRegistry → PaperMarkdownTool → PaperProcessor`。最终 result ref 为：

```text
sha256:f14fb87c5c5147ea69f3234e76a17995b58898397497463029788c8749dba2e8
```

本次验收覆盖 138 个正文 retrieval unit、25 个视觉对象（含 2 个单行复杂 PDF 公式）、385 个
导出 artifact 引用；两次
相同请求的 result ref 一致，导出文件哈希全部匹配。PDF 解析器额外保证相邻 caption 不会
复用前一视觉区域，并在 caption 明确包含章节名时记录 source/semantic 双路径，避免跨栏
页面把 Table 1/2 错分到 `3.2 RealScholarQuery`。独立公式现在以 `equation` 视觉任务进入
同一 VLM 协议，并按原子元素切分，避免多个公式共享一个 RAG chunk。未注入视觉解释器时
结果仍明确为 `degraded`，质量代码为 `visual_interpretation_unavailable`，且不生成伪视觉
retrieval unit；这不是多模态 RAG 通过的替代品。

## 非目标

- 本轮不实现 arXiv/Web Search 下载。
- 本轮不实现 Athena 模型供应商或 VLM 客户端。
- 本轮不通过 LLM 重写正常正文，也不让 LLM 掩盖可确定性修复的结构错误。

## PDF fallback 二次审计：非 VLM RAG 缺陷（2026-07-24）

审计对象为 PaSa PDF 通过正式 `paper_markdown` 路径生成的
`sha256:f14fb87c5c5147ea69f3234e76a17995b58898397497463029788c8749dba2e8`。
视觉解释缺失不计入本轮结论；以下问题均来自确定性 PDF 文本和结构处理。

### 22. Caption 关键词造成错误语义章节

当前 PDF parser 只要 caption 含有历史章节名，就把视觉对象迁移到该章节。该启发式把
Table 4/5 的实验结果迁移到 Datasets，把 Table 6 迁移到 Selector，把 Table 8 迁移到
Crawler，并错误改写 Table 14/16 等附录表的 retrieval heading。错误路径已进入实际
retrieval text，不只是 metadata 展示问题。

### 23. 公式版面碎片和控制字符进入索引

独立单行复杂公式已经具有 VLM 入口，但多行公式的 PDF 字形块仍被当作普通段落合并。
PaSa 的 PPO 公式 chunk 因此包含 `U+0010`--`U+0015` 控制字符、孤立的 `min`、`clip`、
`πold` 和公式编号碎片。该 chunk 会响应 `PPO policy loss` 查询，直接污染 lexical/vector
索引。正文行内公式仍应直接提取，但不可因此接受控制字符或纯版面碎片。

### 24. Bibliography 条目被按版面块切碎

PDF 结果把 37 条参考文献拆成 75 个 bibliography chunk，出现仅 `36.`、独立 arXiv ID、
作者半行与标题半行分离等情况。最短 retrieval unit 只有 26 字符，无法作为独立参考文献
证据使用。

### 25. 附录标题、作者和引用图谱不完整

- `B.1/B.2/D.1/D.2/G.1/G.2/H.1/H.2` 未成为 Markdown heading；
- 7 位作者被合并为两个带脚注标记的 metadata 字符串；
- 正文可见的 Figure/Table 引用没有写入 `reference_keys`；
- PDF 只恢复 10 个唯一 citation key，而 TeX 基准为 37 个；
- 存在 `Paper Search`、拼接失败的脚注 URL 和跨 block 断词。

现有质量门禁没有报告这些事实；若只补上 VLM，产物仍可能被错误接纳。

## PDF fallback 二次修复决策

1. 删除 caption 章节名匹配；从正文显式 `Figure N`/`Table N` 引用建立 reference edge，
   使用最近且唯一的引用上下文推断视觉 semantic path。没有可靠证据时保留 source path。
2. 扩展 PDF appendix 小节识别，并把显示引用写入 chunk `reference_keys`；质量门禁检查
   活动显示引用、持久化边和视觉 label 是否闭合。
3. 删除不可索引控制字符和确定性页边噪声；多行公式碎片不得以普通 prose 形态进入
   retrieval text。单行复杂公式继续使用 equation VLM task，正文行内公式继续直接提取。
4. 在 PDF parser 内按参考文献结束标记合并连续版面块，修复跨块断词，丢弃孤立页码；
   每个完成条目形成一个 bibliography 元素和一个原子 chunk。
5. 按 PDF 作者脚注分隔符恢复独立作者，并保留原始 front matter 正文证据。
6. 新增实际质量诊断：控制字符、短 bibliography、未闭合显示引用和 appendix 标题丢失
   必须使产物 degraded，不能只依赖测试脚本发现。

## PDF fallback 二次验收标准

- PaSa Table 1--20 与 Figure 1--3 的 persisted labels 和正文显示引用形成闭合图谱；
- Table 4/5/6/8/14/16 不再使用 caption 关键词错误迁移；
- 公式 retrieval text 不含 C0 控制字符，PPO 公式碎片不以普通 paragraph 进入索引；
- bibliography 恢复为 37 个非空独立条目，无纯页码、独立 arXiv continuation 或短残片；
- 8 个 appendix 小节均形成 heading path；作者恢复为 7 个独立名字；
- artifact 哈希、字符区间、ID 命名空间、视觉父子关系和确定性重跑继续通过；
- 非视觉 lexical probes 能召回 dataset、main results、ablation、reference 和 prompt 证据；
- 全量测试、Ruff、编译和 `git diff --check` 通过。

## PDF fallback 二次修复实施结果（2026-07-24）

已按上述决策完成 `pdf_parser.py` 和 RAG 质量门禁实现，并通过正式
`ArtifactStore → ToolRegistry → PaperMarkdownTool → PaperProcessor` 重新处理
PaSa PDF。确定性文本和结构问题的修复结果如下：

- PDF 正文括号引用恢复为 37 个作者年份键；`Figure/Table` 显示引用支持组写法，
  PaSa 的 23 个显示引用与 23 个持久化视觉 label 完全闭合；视觉 semantic path 只
  使用最近且唯一的正文引用上下文，不再从 caption 关键词迁移。
- 37 个 bibliography 版面块组恢复为 37 条独立条目；跨块连字符和孤立页码被确定性
  修复或合并，最短条目不再是单独的页码/URL 片段。
- `B.1/B.2/D.1/D.2/G.1/G.2/H.1/H.2` 均生成 Markdown heading path；首页作者脚注恢复
  为 7 个独立姓名；首页图内的 `Paper Search` 和孤立脚注 URL 不再进入正文。
- PDF 文本提取会删除 C0、私用区字形；编号展示公式周围无法可靠排序的纯字形块被从
  prose 中省略并记录 `pdf_formula_layout_fragment_omitted`，因此不会污染 lexical/vector
  索引。独立单行公式仍按既有 `equation` 视觉协议处理，行内公式不产生视觉任务。
- 质量门禁新增 `rag_control_character`、`rag_bibliography_fragment`、
  `rag_appendix_heading_unparsed` 检查；这些问题即使绕过 PDF parser 直接进入文档，也会
  使结果保持 degraded。视觉解释器未注入仍会记录 `visual_interpretation_unavailable`，
  本轮不把它误判为 VLM 通过。

PaSa 正式产物当前为 103 个 retrieval unit、25 个视觉对象（20 表、3 图、2 公式），
7 位作者，37 条参考文献；双次执行 result ref 相同，所有导出 artifact 哈希和 chunk
字符区间回放通过，103 个 unit ID 均带 `arxiv:2501.10120:` namespace。非视觉 lexical
probe 可以召回数据集、主结果、附录表格、prompt 和参考文献证据；公式 VLM 未注入时只
保留两个可确定提取的独立公式和降级诊断，不以伪造文本替代视觉解释。
