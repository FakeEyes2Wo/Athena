# Academic Survey Agent

面向读者：审查论文发现链路的人，以及负责 Scheduler、paper_source、界面、运维和离线评测的协作模块开发者。

本文说明 Academic Survey Agent 解决什么问题、如何保证结果可信、上下游如何与它交换信息，以及当前实现的能力边界。它不展开内部文件和函数，也不把实现细节当成接口承诺。

## 一句话定位

Academic Survey Agent 把一个研究主题和明确约束转成一组**已经完成相关性判断、身份合并、排序和审计留痕**的参考论文，并生成 `paper_source` 可以直接消费的取源请求。

它解决的是“应该参考哪些论文”，不负责“怎样下载和解析这些论文”。

## 在 Athena 中的位置

```text
用户目标 / TaskParser
        │
        ▼
Scheduler 形成 SurveyRequest
        │
        ▼
Academic Survey Agent
  理解主题 → 多源检索 → 去重 → 相关性判断 → 排序
        │
        ├── SurveyCorpus：供审查、复现和离线评测
        │
        └── PaperSourceRequest：供 paper_source 取源
                                      │
                                      ▼
                            paper_markdown → paper_rag
```

模块之间的责任边界如下：

| 模块 | 负责 | 不负责 |
| --- | --- | --- |
| Scheduler / 编排层 | 把任务目标转成请求，选择预算和取源策略，注入运行依赖 | 自行检索、判断相关性 |
| Academic Survey Agent | 发现论文、合并身份、执行约束与证据门槛、排序、生成取源请求 | 下载正文、解析 PDF/TeX、建立 RAG |
| paper_source | 固定版本、选择下载通道、校验字节、生成转换请求 | 判断论文是否相关 |
| paper_markdown / paper_rag | 转换正文、建立可检索语料 | 改写上游的相关性结论 |
| GEPA 离线作业 | 在冻结评测条件下优化提示，并生成晋升报告 | 在生产 Session 中改变预算或安全规则 |

paper_source 的字段要求和反馈码见 [Paper Source 上游契约](paper_source_upstream_contract_ch.md)。

## 输入：编排层需要提供什么

Agent 接收一个已经写入共享 ArtifactStore 的 `SurveyRequest`。Athena Turn 的 `request_ref` 指向该请求，而不是直接放一段自然语言。

| 信息 | 含义 | 协作方注意事项 |
| --- | --- | --- |
| `topic` | 要调查的研究主题 | 必填；应表达研究问题，而不是检索 API 语法 |
| `constraints` | 年份、venue、语言、领域等硬约束 | 只有能确定执行的约束才能放入这里 |
| `mode` | `fast` 或 `diligent` | 决定固定预算；模型不能自行扩张预算 |
| `task_metadata_ref` / `objective_ref` | 原任务上下文 | 只作溯源，不替代 topic |
| `unresolved_constraints` | 尚不能确定执行的用户要求 | 不得静默猜测；结果会标记为 partial 并提醒审查者 |
| `paper_source_policy` | 下游取源策略和最终论文数上限 | 由编排层决定，不由相关性模型生成 |

年份区间必须有效。显式给出的 venue、年份或语言约束遇到元数据缺失时，候选会进入 `constraint_unknown`，不会因为“可能符合”而进入最终集合。

### 两种预算

当前预算版本为 `academic-survey-budget-v2`：

| 上限 | fast | diligent |
| --- | ---: | ---: |
| 检索轮数 | 2 | 4 |
| 初始 query family | 4 | 8 |
| 候选数 | 250 | 1000 |
| LLM 判断数 | 120 | 500 |
| RefChain seed | 5 | 20 |
| 每个 seed 的引用 | 10 | 20 |
| 最终参考论文 | 20 | 50 |
| 墙钟时间 | 180 秒 | 600 秒 |
| 每轮检索 batch | 4 | 8 |

最终 handoff 数还会受到 `paper_source_policy.max_papers` 限制。这个限制只截断交付集合，不会让论文发现提前停止。

## 它怎样完成一次调查

一次运行由固定阶段组成。模型负责理解和判断，阶段顺序、预算、过滤和停止条件由确定性控制流负责。

1. **理解研究问题。** 将主题拆成多个互补的 query family，并形成可逐项核验的相关性标准。
2. **覆盖优先检索。** 在重复搜索同一视角前，优先执行尚未覆盖的 query family 和尚未探测的通道。
3. **深入结果页。** 四个正式通道都支持真实分页；仍有下一页的查询会保留到后续轮次。
4. **合并真实身份。** 只使用 DOI、arXiv、Semantic Scholar、OpenAlex、PMID 或 PMC 等真实标识符合并；标题相似本身不会触发合并。
5. **执行硬约束。** 明确不符合或缺少必要约束元数据的候选不会进入相关性判断后的交付集合。
6. **逐项判断相关性。** 每个结论必须引用标题或摘要中的原文证据；无法提供有效证据时不能判为 relevant。
7. **扩展一跳引用。** 对新发现的高相关论文做一次受限 RefChain；引用论文仍要经过相同的身份、约束和证据门槛。
8. **补充检索视角。** Query Evolution 可以追加方法、应用、比较或局限等查询，但不会替换尚未执行或仍有下一页的查询。
9. **确定性排序与交付。** 只在 relevant 候选中综合多列表命中、判据覆盖、影响力和时效性排序，再按 handoff 上限截断。

### 为什么 V2 更重视召回

旧调度会先把多个通道都分给第一个 query。若一轮只有四次检索，而第一个 query 恰好配置四个通道，其他 query family 可能一次都没有执行。这会得到很高的 precision，却系统性漏掉不同术语、方法或应用方向的论文。

V2 从四个方面处理这个问题：

- **先覆盖 query family，再利用高收益通道。** 同等预算下优先扩大主题视角，而不是反复确认同一视角。
- **使用真实 cursor 分页。** 后续轮次可以进入结果长尾，不再重复第一页。
- **保留活动查询。** 尚未执行、仍有下一页或新演化出的查询共同参与后续调度。
- **分离 discovery 与 handoff。** 找够最终论文数不代表搜索已经充分；Agent 会继续运行到轮数、候选、判断、时间、饱和或无活动查询等边界。

通道收益仍会参与后续分配，但只能在覆盖要求之后起作用。这样保留了 SPAR 对有效通道的利用能力，同时降低早期视角偏差。

## 正式检索通道

当前只使用四个元数据通道：

| 通道 | 主要价值 | 分页方式 | 常见运行问题 |
| --- | --- | --- | --- |
| arXiv | 预印本、摘要、arXiv/DOI 身份 | `start` | 精确领域查询可能为零结果 |
| OpenAlex | 跨领域元数据、引用数、开放获取线索 | cursor | 元数据字段可能缺失 |
| Semantic Scholar | 外部 ID、引用、开放获取线索 | `offset/next` | 未配置 key 时容易触发 429 |
| PubMed | 生物医学主题、PMID/PMC、结构化摘要 | `retstart/count` | 查询语法需要通道改写 |

通道原始响应和规范化结果都通过 ArtifactStore 留痕。分页 cursor 是缓存键的一部分，因此第一页、第二页不会互相误命中；离线 exact replay 遇到缺页会明确失败，而不是把缺失响应伪装成零结果。

本模块不依赖、不调用 `asta-paper-finder`，也没有隐藏 fallback。

## 哪些论文有资格进入最终集合

最终参考论文同时满足以下条件：

1. 至少有一个真实、可供下游解析的论文标识符；
2. 不违反年份、venue、语言等硬约束；
3. 所有 required relevance criteria 都有有效证据支持；
4. 跨通道合并后身份没有已知冲突；
5. 排名后位于本次 handoff 上限内。

三个容易混淆的概念应分开看：

| 概念 | 含义 |
| --- | --- |
| observation | 某个 query 在某个通道看到的一条原始结果 |
| candidate | 多个 observation 按真实身份合并后的一篇论文 |
| reference paper | 通过约束和相关性门槛、最终被选中交付的一篇论文 |

只有标题、没有真实 ID 的结果会先进入 quarantine，并可通过精确标题查询补一次身份；补不到就不会交给 paper_source。可能重复但没有身份桥接证据的论文只记录为 `possible_duplicate`，不会凭标题自动合并。

## 输出：审查者和下游能得到什么

Agent 的顶层结果是 `AcademicSurveyResult`。它只包含少量状态和 artifact 引用：

| 输出 | 用途 |
| --- | --- |
| `survey_corpus_ref` | 指向完整的审计入口 `SurveyCorpus` |
| `paper_source_request_ref` | 非空时可直接交给 paper_source；零命中时为 `None` |
| `stats_ref` | 本次覆盖、成本、停止原因和错误摘要 |
| `prompt_bundle_version` | 说明本次判断使用了哪一版提示 |
| `status` / `warnings` | 区分正常受限完成和覆盖受损 |

`SurveyCorpus` 内联最终参考论文的轻量索引，并引用以下审计材料：

- 原始请求和预算；
- QueryPlan；
- 完整 candidate ledger；
- 完整 judgment ledger；
- 每篇论文的相关性证据和排名组成；
- PromptBundle 与版本；
- 本次统计和停止原因。

### `complete` 不等于“找全了所有文献”

`complete` 表示 Agent 在选定预算内正常运行到了一个预期停止条件，例如最大轮数、候选上限、判断上限、饱和或无活动查询。它不声明对开放世界文献实现了绝对召回。

`partial` 表示覆盖受到异常影响，例如某个选定通道失败、存在无法执行的硬约束或墙钟时间在阶段中途耗尽。只要其他通道仍有有效结果，partial 仍可包含可用的参考论文和取源请求。

所有已配置检索通道都失败且没有任何相关论文时，Agent 会失败，而不是生成一个看似正常的空语料。相反，通道正常但确实零命中时，会生成 `complete` 空语料，并把 `paper_source_request_ref` 设为 `None`。

## 各协作模块怎样接入

### Scheduler / ThreadRuntime

编排层需要完成五件事：

1. 将任务目标映射成 `SurveyRequest`，不能把未解析约束擅自转成硬过滤；
2. 把请求写入与 Agent 相同的 ArtifactStore，并把引用放入 Athena Turn；
3. 在组合根创建 `AcademicSurveyAgent`，注入模型、四通道、缓存和可选 checkpointer；
4. 通过现有 `agent_runner` / ThreadRuntime 契约调度它；
5. 读取 `AcademicSurveyResult`，决定是否将非空取源请求交给 `paper_fetch`。

当前类和默认通道工厂已经导出，但应用组合根仍需显式实例化并注册 Agent；模块不会在导入时创建全局模型、读取环境密钥或自行注册后台任务。

### paper_source

`paper_source_request_ref` 非空时可以直接消费，不需要重新判断相关性或重新排序。输入论文的顺序就是优先级，`corpus_ref` 指回本次 SurveyCorpus。

下游失败不应反向改写“论文是否相关”。下载失败、版本无法固定和正文格式不可识别是取源事实，应由 Scheduler 决定重试、跳过还是补位。下游反馈中最值得回传给检索侧的是：

- `paper_source.title_mismatch`：可能发生了错误身份合并；
- `paper_source.no_channel_available`：身份或线索不足；
- `paper_source.max_papers_truncated`：上下游批次上限不一致；
- 线索 URL 被拒绝或返回非论文内容：通道映射需要修正。

### 界面和人工审查

运行过程中会发出以下生命周期事件：

| 事件 | 适合展示的内容 |
| --- | --- |
| `academic_survey/started` | run、mode、提示版本 |
| `academic_survey/query_plan_ready` | 初始 query 数和计划引用 |
| `academic_survey/retrieval_round` | 轮次、pull 数、新 observation 数 |
| `academic_survey/judgment_progress` | 已判断及 relevant/uncertain/irrelevant 数 |
| `academic_survey/refchain_complete` | seed 和引用候选数 |
| `academic_survey/saturation` | 最终停止原因和 stats 引用 |
| `academic_survey/completed` | result、corpus 和论文数 |

事件只携带小型摘要与 artifact ref。界面不应要求事件内联摘要、原始 API 响应或完整 judgment ledger。

人工审查建议从最终论文列表开始，抽查其 `judgment_ref`、`rank_breakdown_ref` 和 matched queries；需要解释漏检时，再查看 query coverage、candidate ledger 和 query/channel batch 轨迹。

### 运维和凭据

组合根可以向默认通道工厂注入联系邮箱、Semantic Scholar API key 和 PubMed API key。凭据不能进入请求、checkpoint、事件或 artifact。

建议生产环境提供持久化 cache 和 LangGraph checkpointer。默认内存 cache 适合单进程运行和测试，但进程退出后不能复用；checkpoint 的命名空间由 thread 与 turn 稳定导出，便于显式恢复。

应持续监控：

- 各通道成功率、429/5xx 和重试次数；
- query coverage 与分页 pull 数；
- candidate/relevant/uncertain 数和饱和曲线；
- cache 命中、LLM 调用、token 和墙钟成本；
- partial 比例及其 error 分类；
- paper_source 的 title mismatch 和无可用通道反馈。

## GEPA：只在离线优化提示

GEPA 调用同一条生产调查流程，但不进入生产 Session。它只能优化 Query Understanding、通道查询改写、相关性判断和 Query Evolution 的提示文本；预算、硬约束、身份合并、证据门槛、排序公式和停止条件不能由提示变异。

离线评测把两个阶段分开：

- **candidate recall**：gold 论文是否进入完整候选集合，用于识别 retrieval miss；
- **handoff precision / recall / F1**：gold 论文是否通过判断并进入最终交付，用于识别 judgment 或 selection miss。

一个候选 PromptBundle 只有同时满足以下条件才具有晋升资格：

1. held-out promotion split 上的 macro handoff F1 严格提高；
2. macro handoff precision 不低于 incumbent；
3. macro candidate recall 不低于 incumbent；
4. 使用同一个明确版本的冻结快照；
5. 获得显式批准。

这组门槛防止通过放宽相关性标准换取表面 recall。exact replay 只适合不改变查询集合的判断提示；会改变查询的优化必须使用版本化 frozen snapshot，或先补采再冻结的 materialized offline 流程。

## 失败与处理建议

| 情况 | 结果 | 协作方处理 |
| --- | --- | --- |
| 单通道失败 | 继续其他通道，结果通常为 partial | 检查凭据、限流和数据源状态；已有结果仍可审查 |
| 所有通道失败且无结果 | Agent 失败 | 不要当作“没有相关论文”；修复数据源后重试 |
| 通道正常但零命中 | complete 空 corpus | 可调整主题、约束或使用 diligent 模式 |
| 未解析硬约束 | partial + warning | 由人工或 TaskParser 明确约束语义 |
| 结构化模型输出连续无效 | 当前节点失败 | 检查模型兼容性和提示版本 |
| 证据引用不存在 | 该 criterion 变为 unknown | 不应绕过 evidence gate |
| 达到 judgment/candidate/time 上限 | 停止并记录原因 | 结合覆盖统计决定是否扩大预算 |
| 外部取消 | 传播取消 | 已写 artifact 可保留，不能伪造 completed |
| paper_source 下载失败 | 不改相关性结论 | 下游重试、跳过或由 Scheduler 补位 |

## 审查清单

人工审查一次实现或集成时，建议依次确认：

- 请求来自共享 ArtifactStore，topic、约束、mode 和取源策略归属清楚；
- 生产组合根显式注入模型、通道、凭据、cache 和 checkpointer；
- 首轮 batch 覆盖多个 query family，而不是全部落在第一个 query；
- cursor 被纳入 cache/replay 身份，未耗尽查询不会被 evolution 丢弃；
- handoff cap 只限制最终输出，不作为 discovery 停止条件；
- 无真实 ID、约束未知或证据无效的候选不会进入 handoff；
- `paper_source_request_ref` 可被现有 paper_source 直接解析，顺序与 corpus 一致；
- partial、空结果和失败三种情况没有混淆；
- 在线路径没有导入或调用 GEPA，也没有读取未显式注入的密钥；
- 日志、事件和 artifacts 中不含 API key；
- promotion report 同时检查 candidate recall、handoff precision 和 handoff F1；
- 新模块只依赖公开契约，不读取 LangGraph checkpoint 内部字段。

## 验证证据

截至 2026-07-28，V2 已通过固定 A/B、真实外部服务运行和完整回归。

### 固定同预算 A/B

同一个 benchmark、同一组 3 篇 gold、同一轮 4 个检索 batch：

| 指标 | V1 | V2 |
| --- | ---: | ---: |
| query coverage | 0.3333 | 1.0000 |
| candidate recall | 0.3333 | 1.0000 |
| handoff recall | 0.3333 | 1.0000 |
| handoff precision | 1.0000 | 1.0000 |

V1 的四个 batch 全部分给 q1；V2 的四个 batch 覆盖 q1、q2、q3 和四个不同通道。这个对比固定了输入、预算和 gold，用来证明调度变化带来的召回提升。

### 真实外部服务运行

复用同一请求、同一模型 revision hash 和同一验证预算：

| 指标 | V1 | V2 |
| --- | ---: | ---: |
| 初始 query coverage | 1/3 | 3/3 |
| 运行轮数 | 1 | 2 |
| observations | 25 | 35 |
| unique candidates | 24 | 28 |
| relevant candidates | 5 | 7 |
| evidence-gated handoff | 5 | 5 |

两次运行的 Semantic Scholar 都因 HTTP 429 降级，所以都属于 partial；V2 仍通过其他通道完成了两轮搜索。在线 API 和模型输出会随时间变化，因此真实运行用于验证外部可用性，严格因果结论以固定 A/B 为准。

V2 live corpus、stats 和 validation report 的内容引用分别为：

- `sha256:1aa585533d2604b37dad5067179c89f864790a4fdecc128d713101fcd2fa30d7`
- `sha256:3db76b8eb6fd990877cba11821d8bf9e6cbac6e3281d4632f8afe07df76cb625`
- `sha256:bc79632c8c290a0302ab4308481434656dc4616168c2cd7f4c3f6109e82c2f2e`

这些引用需要在执行验证时使用的 ArtifactStore 中解析。123 个 live artifacts 全部通过内容哈希校验，API key 匹配数为 0。

### 自动回归

完整测试结果为 362 passed、25 subtests passed；同时通过源码编译、Black、依赖一致性和 diff whitespace 检查。测试中的论文服务全部使用离线 fixture，只有单独标记的真实验收流程会访问外部服务。

## 当前限制与后续协作点

- `complete` 是预算内完成，不是 systematic review 意义上的绝对召回保证；更强结论需要版本化、领域覆盖充分的 gold benchmark。
- 目前依赖开放论文 API，没有本地全量 SPLADE/SPECTER/ColBERT 混合索引；API 排名和可用性会限制长尾召回。
- RefChain 只有一跳，尚未实现 forward citation、co-citation 或自适应第二跳。
- 相关性证据只来自标题和摘要，不读取全文；全文级纳入标准需要在 paper_source/paper_markdown 之后增加独立阶段。
- 默认 cache 是进程内存；生产跨进程恢复需要部署层注入持久化实现。
- Semantic Scholar 无 key 时的 429 是已观察到的真实风险，部署时应提供 key 或接受 partial 覆盖。
- GEPA 的质量取决于反馈、Pareto 和 promotion 三组互斥数据，以及可信的 frozen snapshot；没有这些输入时不应宣称提示得到优化。
- Scheduler 的任务映射、应用组合根注册、界面展示和下游失败补位属于协作模块，当前 Agent 不在导入时替它们做决定。

扩展这些能力时，应优先保持现有 `SurveyRequest`、`AcademicSurveyResult`、`SurveyCorpus` 和 `PaperSourceRequest` 边界稳定。新的检索器、停止估计或图扩展可以替换内部策略，但不应迫使 Scheduler、paper_source 或审查界面读取内部 checkpoint。
