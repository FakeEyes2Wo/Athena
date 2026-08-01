# PaperScout Agent

面向读者：复核论文检索链路执行流程的人，以及负责组合根、Scheduler 和离线评测的协作方。

本文说明 PaperScout 在 Athena 里怎么执行、每一步产出什么、与原论文的对应关系和差异，
以及当前的复现结果与边界。

## 一句话定位

PaperScout 把"找论文"当成多轮决策：它维护一个累积的 paper pool，每一步只看池的一个摘要
视图，自己决定这一步该发新的 `search` 还是沿某篇论文 `expand` 一跳引用，直到池不再增长。

它解决的是"用什么检索动作序列把相关论文捞出来"，不负责下载正文、解析或建 RAG。

## 与原论文的对应关系

实现依据 PaperScout（Pan et al., 2026, arXiv:2601.10029）与其开源实现
(https://github.com/pty12345/PaperScout, Apache-2.0)。本仓库只实现推理期的 Agent，不实现
PSPO 训练：PSPO 是训练算法，需要 4×H800、Qwen3-4B 底座与 verl/ray 训练栈；Athena 这一层
对应论文里未经 RL 微调的 `PaperScout-Qwen3-Max` 变体——同一套框架、同一组提示与阈值，
由一个通用大模型直接驱动。

逐字沿用论文口径的部分：

| 项目 | 取值 | 出处 |
| --- | --- | --- |
| 动作空间 | `search(query)`、`expand(arxiv_id)` | Table 1 |
| 观测 | 双列表，至多 10 篇已扩展 + 10 篇未扩展 | §4.1 与 A.2 |
| 入池阈值 τ | 0.01 | A.2 |
| 交付阈值 ρ | 0.5 | Evaluation Protocol |
| 过程奖励 top-k | 3，判定阈值 0.4 | A.2 与开源实现 |
| 重复动作惩罚 η | 0.5 | 式 (2) |
| 终止条件 | 池连续三步不变 | A.2 |
| 策略提示、SELECT_PROMPT | 逐字移植 | 开源实现 `prompts.py` |

过程奖励在推理期不参与任何决策，只作为可审计的过程信号写进 `ScoutAction.reward`。

### 三处必须偏离的地方

1. **检索后端**。论文训练用本地 Milvus 快照（数百万篇元数据 + 预缓存 ar5iv 全文），评测时
   统一改用 serper.dev 的网页搜索。两者本仓库都没有，改接 arXiv Atom 接口与 Semantic
   Scholar relevance search。这会改变召回的绝对水平，因此本文的复现数值与论文数值不是同
   语料对比。
2. **引用扩展**。参考实现抓 ar5iv 全文，用三条正则从参考文献串里抠出标题，再逐条按标题
   反查。这里改用 Semantic Scholar 的 `/references`，直接拿到结构化的被引论文，省掉两层
   易错解析和一次全文下载。
3. **相关性分数**。论文的 ρ(p) 是 pasa-7b-selector 输出 `"True"` token 的概率，需要推理端点
   返回 logprobs。当前端点实测不返回 logprobs，二值判定会让 ρ 只剩两个取值、Recall@k 排序
   和 0.5 阈值同时退化，因此默认改用论文 LLM-score 一节的 0–3 分级评分归一到 [0,1]。
   `TokenProbabilityScorer` 保留了原口径，端点支持 logprobs 时可直接切换。

## 执行流程

入口是 `PaperScoutAgent.run(ctx)`，输入是写进 ArtifactStore 的 `ScoutRequest`
（`ctx.turn.request_ref` 指向它）。

```text
ScoutRequest artifact
        │
        ▼
PaperScoutAgent.run
        │
        ├─ 每步：pool.observation() + 动作历史 → 两条消息的提示 → 策略采样
        │        │
        │        └─ 工具调用 → ToolRegistry.adispatch（并行，至多 max_parallel_calls）
        │                 │
        │                 ├─ paper_scout_search → 各检索后端 → 打分 → τ 过滤 → 入池
        │                 └─ paper_scout_expand → 引用后端 → 打分 → τ 过滤 → 入池
        │
        ▼
ScoutCorpus（retained / pool / actions）+ ScoutStats → PaperScoutResult
```

### 逐步

1. **读取请求。** 从 artifact 解析 `ScoutRequest`，构造本次运行私有的 `ScoutSession`
   （持有 pool、动作历史、后端、scorer）和私有 `ToolRegistry`（只注册两个动作工具）。
2. **渲染观测。** `pool.observation()` 按分数降序取至多 10 篇 `[EXP]` 与 10 篇 `[NEW]`，
   摘要截断到 400 词；动作历史渲染成 `[Search] q` / `[Expand] id` 行。池为空时是一句
   `No papers in the pool.`。
3. **策略采样。** 每步都用刚渲染的观测重新构造两条消息（system + user），不累积对话历史
   ——状态全在池里，上下文长度不随步数增长。模型在 `<analysis>` 里说明判断，然后发出工具
   调用。
4. **并行执行动作。** 取前 `max_parallel_calls` 个调用交给 `adispatch`。两个工具都声明
   `concurrency_safe=True`，因为论文的策略明确要求一步内并行；池的读改写由 `ScoutSession`
   内部的锁保证，而不是靠 Agent loop 的串行屏障。
5. **入池。** 每个动作各自：跨后端合并去重 → 剔除已在池中的 → 打分 → 丢弃低于 τ 的 →
   入池 → 记录本次动作的过程奖励。重复的 query 或重复 `expand` 同一篇不再打后端，直接记
   −0.5。
6. **判定终止。** 该步结束后比较池大小：连续三步没有增长即 `pool_unchanged` 停止；否则受
   `max_steps`、`max_seconds` 和取消信号约束。
7. **产出。** 池里分数 ≥ 0.5 的论文按分数排序成交付集合，连同完整池与逐动作轨迹写成
   `ScoutCorpus`，统计写成 `ScoutStats`，顶层返回 `PaperScoutResult`。

### 复核要点

- **去重是双重的。** 只按标识符去重不够：同一篇论文在 Semantic Scholar 里可能有多条记录、
  各带不同 `paperId`，标识符优先级会让它们取到不同 key 而同时进池，直接抬高交付集合的分母。
  因此规范化标题相同也算同一篇。
- **打分在锁外、入池在锁内。** 打分是慢调用，放锁内会把并行动作串行化；入池是 read-modify-
  write，必须整体在锁内，且入池前要重新检查一次是否已存在。
- **`expand` 只接受池内论文的 arXiv id。** 池外 id、无效 id、重复扩展都走同一条 −0.5 分支，
  不会去打后端。
- **限流按服务分桶。** arXiv 每 3 秒 1 次是服务条款要求；Semantic Scholar 实测即使按 1 秒
  间隔仍会零星返回 429，只能靠退避重试兜住，因此它是"尽力而为"通道。
- **单后端失败不终止动作。** 记进 `errors` 并继续用其他后端，整轮结束时 `status` 变
  `partial`。

## 接入方式

依赖全部由组合根注入，模块不在导入时创建客户端、不读环境变量：

```python
limiter = HostRateLimiter(
    transport=UrllibTransport(timeout=30),
    bucket_intervals={"api.semanticscholar.org": SEMANTIC_SCHOLAR_INTERVAL},
)
semantic_scholar = SemanticScholarBackend(limiter, api_key=...)
agent = PaperScoutAgent(
    artifacts,
    [ArxivSearchBackend(limiter), semantic_scholar],
    semantic_scholar,
    GradedRelevanceScorer(client, model),
    model=model,
    client=client,
)
```

运行期发出三类事件：`paper_scout/started`、`paper_scout/step`（步号、调用数、池大小、
分析摘要）、`paper_scout/completed`（交付篇数、停止原因）。事件只携带小型摘要与 artifact
引用。

## 复现结果

见 `docs/paper_scout_reproduction_ch.md`。

## 当前限制

- 只实现推理期 Agent，不含 PSPO 训练，因此无法复现论文中 RL 微调带来的增益。
- 检索后端与论文不同，数值不是同语料对比。
- Semantic Scholar 无论有没有 key 都会零星 429，是已观察到的真实风险。
- `expand` 只跟出向引用一跳，与论文一致；入向引用和多跳都未实现。
- 相关性判断只看标题和摘要，不读全文。
