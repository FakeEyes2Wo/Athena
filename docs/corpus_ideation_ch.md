# 文献语料接入 Athena loop

Status: current
Owner: Athena maintainers
Last verified: 2026-08-16
Source of truth: `src/athena/research/runtime.py`,
`src/athena/research/agent_turn_runner.py`, `src/athena/research/survey/wiring.py`,
`test/unit/research/test_runtime_survey.py`

Academic Survey 能把一句主题变成可检索的论文语料库；Ideator 需要证据来提假设。本文说明
两者怎么接上，以及三条约束为什么必须成立。

前置阅读：[Academic Survey 全链路](academic_survey_ch.md)、
[paper_rag 工具](paper_rag_tool_ch.md)。

## 一、形态：一次性建库，Ideator 只读

```
athena run --survey [--survey-query …] [--survey-papers N]

  start()
    ├─ Supervisor 照常进 PREPARE ──────────────────────────────▶ SEARCH ─▶ VALIDATE
    └─ asyncio.create_task(_run_survey())
          topic 改写 → scout → fetch → convert → embed → index
          └─ state.corpus_ref = <artifact ref>
                                    │
                          Ideator 每次实例化时读取
                                    ▼
                     paper_* 七个只读算子 + 提示词里的 corpus_ref
```

默认关。一次调研是十几分钟的模型往返，不能由默认值替用户决定花这笔钱。真机实测
`--survey-papers 8` 用时 **13 分 45 秒**（取源、转换、编码、建索引全含），全程与 PREPARE
并行。

### 约束一：SEARCH 绝不为调研停等

`survey_corpus_ref()` 刻意不是 `async`，也不 await 任何东西：

```python
def survey_corpus_ref(self) -> str | None:
    return self.state.corpus_ref
```

语料建好要十几分钟，而 ideation 每一轮都要取一次。在这里等一下，就等于把语料从"可选
增益"变成关键路径。语料没建好时 Ideator 照常只凭 EDA 提假设。

真机上"不停等"这一半被验证过：第 3 次跑测里语料 ready 的事件与 SEARCH 失败几乎同刻发
生，那一轮的 Ideator 一条都没用上语料，loop 本身没有因此多等一秒。

> **另一半曾经写错。** 本文早先的版本在这里写"建好之后的那一轮自动拿到算子"，而这句话
> 从未被验证过——当时的证据只是"SEARCH 没有变慢"，那验证的是**不阻塞**，不是**会生效**。
>
> 第 9 次跑测把它证伪了：语料在 seq 908 就绪，唯一一轮 ideation 在 seq 785 就启动了，
> 早约两分钟；全程 0 次 `paper_*` 调用，6 条假设 0 条引用语料。原因是调度器只在"没有
> 假设可排"时才 `GENERATE`（`scheduler.next_actions`），而第一轮生成的 6 条假设已经
> 填满了 4 个实验额度，此后再没需要生成过。**非阻塞本身没错，错的是它单独并不成立
> ——缺一个"语料就绪 → 补一轮"的触发器。** 见约束四。

### 约束二：只给读的那一组

`corpus_tools()` 用 `build_survey_tools(..., include_survey=False, include_producers=False)`，
交出七个算子：

`paper_corpus_overview` · `paper_keyword_search` · `paper_semantic_search` ·
`paper_section_search` · `paper_cites` · `paper_visual_of` · `paper_chunk_read`

`paper_survey`/`paper_fetch`/`paper_markdown` 会写出**新**语料。摆在 Ideator 面前迟早
会被按下去，而一次全链路是十几分钟起步——建库是编排层的决定，不是 Agent 的。

### 约束三：调研失败不中断 loop

`_run_survey` 把除 `CancelledError` 外的异常转成一条 error 输出，loop 继续。
`aclose()` 里先取消调研任务再停 Supervisor，避免后台任务把进程吊住。

### 约束四：语料就绪必须补一轮 ideation

不阻塞是对的，但它需要一个配套件才成立：**语料落地时得有人再问一次假设。**
`Supervisor._corpus_ideation()` 在 `_fill_slots` 的最前面跑：

```python
if self.state.corpus_ref is None or self.state.corpus_ideation_done:
    return False
self.state.corpus_ideation_done = True     # 先落状态再跑，避免重入补第二轮
await self._persist_state()
hypotheses = await self._run_ideator_turn(self.state.hypotheses_per_ideator)
return len(await self.register_hypotheses(hypotheses)) > 0
```

三条约束成立：

- **只补一轮。** `corpus_ideation_done` 落在持久化状态里，续跑不重复补。
- **不动实验预算。** 它只往队列里加候选，跑几个仍由 `search_limit` 决定；新候选按
  `ranker` 的优先级与既有候选竞争，不插队。
- **没有 Ideator 时直接标记完成**，避免每轮重试一个不存在的能力。

## 二、工具必须惰性求值

Ideator 的 agent type 只注册一次，而它的工具可用性是随时间变的——语料要十几分钟才建好，
Kaggle 是否接入也取决于 Supervisor 后来的决定。所以传给 `register_ideator_agent` 的是
**零参 callable**，在 `factory` 里每次创建实例时才求值：

```python
extra_tools=self._ideator_tools          # 不是 self._ideator_tools()

def _ideator_tools(self) -> ToolRegistry | None:
    return _merged(self._runtime.kaggle_tools("ideator"), self._runtime.corpus_tools())
```

这是 main 上已有的房屋惯例（先例是 `extra_tools=lambda: self.kaggle_tools("plan")`）。
传一个已经求好值的 registry，等于把第一轮的可用性冻结到运行结束。

## 三、`corpus_ref` 存在就装配，别看本进程跑没跑过调研

这条是真机跑测揪出来的，值得单独说，因为它把整个特性静默架空了。

`corpus_tools()` 与 `corpus_paper_ids()` 最初的判据是：

```python
if self.survey_corpus_ref() is None or self._survey_stack is None:
    return None
```

而 `_survey_stack` 只在 `_run_survey()` 里通过 `_ensure_survey_stack()` 建起来。于是：

> `_start_survey` 明确规定"已有语料就不再调研"（`if self.state.corpus_ref is not None:
> return`）。这条路径——续跑、崩溃恢复、命中缓存语料——**保证** `_survey_stack` 是
> `None`。而它恰恰是最该拿到算子的场合。

同时提示词注入只看 `survey_corpus_ref()`，所以 Ideator 会收到"请先调
`paper_corpus_overview`"的指示，手里却一个 `paper_*` 都没有。实测结果：0 次检索调用、
0 条 `sources`，而且**引用核验拿到空集合，任何编造的 paper id 都会静默放行**——当时
`rubric_prior` 还给"有引用"加 0.12 分，等于直接奖励幻觉。

修正后判据只有 `corpus_ref`，装配走惰性：

```python
if self.survey_corpus_ref() is None:
    return None
return build_survey_tools(self._ensure_survey_stack(), ...)
```

语料是内容寻址的、`corpus_ref` 是持久化状态，所以"这个进程有没有跑过调研"从来就不是
正确的判据。

`_ensure_survey_stack()` 必须注入本项目的 artifact store：

```python
build_survey_stack(artifacts=self._store, client=self._client)
```

不注入的话 `build_survey_stack` 会按 `ATHENA_ARTIFACT_ROOT` 自建一份，语料写在一个库
里、loop 到另一个库里去取，`corpus_ref` 会一直取不到。

两个回归用例钉住这条：`test_a_corpus_restored_from_state_still_hands_the_ideator_its_operators`
（含 `built[0]["artifacts"] is runtime._store`）与
`test_citations_are_verifiable_against_a_corpus_restored_from_state`。

## 四、引用必须是"读过的"，且不换算成优先级

`_verify_sources` 在假设入图前，把 `sources` 与**本轮真正打开过正文的论文**求交：

```python
opened = rt.corpus_papers_read()          # RetrievalSession._read 折成论文集合
kept = [s for s in hypothesis.sources if s in opened]
```

判据是"读过"而不是"在语料里"。第一版只查 id 存在，第 12 次跑测证明那太松——一篇《数据
增强综述》被引来支持"两两交互特征"，而它确实在语料里、也确实出现在检索结果里，只是从没
被 `paper_chunk_read` 打开（详见第五节）。会话本来就记着已读 chunk，折成论文集合即可。

没有语料时整段跳过：此时 `sources` 按 schema 本就该为空，不该顺手清掉别的来源写进去的
内容。被丢弃的引用会发一条 error 输出说明数量与原因。门禁臂与 baseline 臂都走这一步。

**引用不再加分。** `rubric_prior` 早先给非空 `sources` 折算 +0.12 最终得分，意图是让有
据可依的假设先跑；实测它买到的是装饰。引用本身仍有价值（可追溯、可复核），只是不该换算
成优先级——真要让文献影响排序，得先有"这条引用确实支持这个主张"的判据，而那还不存在。

## 五、真机结果

三次跑测各自证明了不同的一段，合起来才是完整链路。

**第 8 次**（语料预置，跳过 13 分钟调研）—— 证明"语料 → Ideator"这一段：

```
hypotheses=5  experiments=3  sota=exp_baseline
cited papers: arxiv:2304.02858, doi:10.69987/aimlr.2026.70205
```

4 条 Ideator 假设里 3 条带可核验引用。当时的实验判决（REFUTED/INCONCLUSIVE）后来查明
全部无效——那一轮的 evaluator 按位置对齐，每个候选恒定 0.502，见
[Evaluator 契约](evaluator_contract_ch.md)。**引用是真的，判决不是。**

**第 9 次**（`--survey --survey-papers 8`，不预置语料）—— 证明"调研 → 语料"能在同一
进程内跑完：调研 seq 1 起、seq 908 就绪，PREPARE → SEARCH → VALIDATE → COMPLETED 全程
走通，基线 0.8823（evaluator 已修好）。但**语料一次都没被读到**：唯一一轮 ideation 在
seq 785 就启动了，0 次 `paper_*` 调用、6 条假设 0 条引用。约束四正是为此加的。

**第 11、12 次**（加了约束四之后）—— 触发器两次都按预期生效，假设 6 → 12，其中
**6 条带可核验引用**（此前 0 条），`corpus_ideation_done` 只置一次。

第 12 次把实验预算提到 6，因此带引用的假设第一次真的跑上了：

| 假设 | 引用语料 | 分数 | 结果 |
|---|---|---|---|
| baseline | — | 0.879049 | — |
| `0373fd55be93` | 否 | **0.880641** | 成为新 SOTA |
| `ce8bb6989abd` | 是 | 0.871740 | 低于基线 |
| `afd5f68a98f0` | 是 | 0.700031 | 明显低于基线 |
| 另三条带引用的 | 是 | — | FAILED |

**要直说：这一轮里带语料引用的假设没有一条赢过基线，赢的那条恰恰没引用。**

查下来问题不在"文献接地没用"，而在**接地从来没发生过**。带引用与不带引用的假设提的是
同一批干预（f00–f04 两两交互特征、优化集成权重），赢的那条和排第二的那条其实是同一件事；
引用是事后贴的标签，而且经常张冠李戴：

| 引用 | 论文实际是什么 | 用来支持什么 |
|---|---|---|
| `tkde.2025.3622600` ×3 | 《数据增强综述》 | "两两交互**特征工程**" |
| `arxiv:2010.06479` ×2 | 《信用卡欺诈检测**综述**》 | target encoding 与 SMOTE |
| `arxiv:2206.13152` | 《重采样方法评估》 | target encoding |

而语料里三篇真正讲 AUC 的论文一次都没被引用——任务主指标就是 ROC-AUC。这个激励是本设计
自己造的：`rubric_prior` 给非空 `sources` 折算 +0.12，而校验只查 id 存在。

### 三条修正

1. **核验判据从"在语料里"换成"本轮真的用 `paper_chunk_read` 打开过"**
   （`RetrievalSession.read_papers`）。上面那些论文都在检索结果里出现过，只是从没被打开。
2. **`rubric_prior` 不再为"有引用"加分。** 奖励"有没有引用"就是在为贴标签付钱。引用仍
   有价值（可追溯、可复核），只是不该换算成优先级。
3. **语料目录直接摆进 lane 的 prompt**，不再指望 Agent 自己调 `paper_corpus_overview`
   ——真机三次跑测它一次都没调过。

## 六、A/B：语料到底有没有用？（结论：这个实验设计答不了）

同一任务、同一数据，开/关 `--survey` 各跑一次，`--max-search-experiments 6`。
两臂的留出集**完全相同**（同 1200 个 id、同 72 个正例），所以分数可比。

| | 关调研 | 开调研 |
|---|---|---|
| 基线 | **0.863697** | **0.690575** |
| 最好候选 | **0.913656** | 0.716103 |
| 相对自身基线 | **+0.0500** | +0.0255 |
| 假设数 / 带引用 | 8 / 0 | 11 / 5 |
| 实验失败数 | 1 | 3 |
| 终态 | COMPLETED | VALIDATE/FAILED |

**修正确实生效了**：这一轮两臂提的东西不再一样了。关调研那臂全是特征工程（target
encoding、多项式与交互项），开调研那臂被语料带向了 SMOTE 与 stacking。而且严格核验之后
只剩**一篇**论文（`arxiv:2304.02858`）撑起 5 条引用——说明 Ideator 这次真的只打开了那一
篇，不再是散贴 6 篇。

### 但上表右列是假的

自己从 artifact 里把预测重算一遍，survey-on 臂对不上：evaluator 报 0.690575，独立重算是
**0.876182**。原因是它的 `predictions/` 里躺着一个自测草稿 `pred_test.csv`（AUC 0.5214），
而评估器把目录下每个 `*.csv` 都 concat 起来打分——两个文件同一批 id，打的是"真模型与随机
文件各半"的混合分。草稿躺在基线的 commit 里，**每个候选都继承了它**。详见
[Evaluator 契约](evaluator_contract_ch.md) 第六节。

**按真实数字，survey-on 臂的基线（0.8944）反而比 survey-off 臂（0.8637）更好。** 所谓
"加了调研假设更差"是这个混合造成的假象，不是文献接地的效果。

### 这个 A/B 仍然答不了原问题

即便修掉上面那条，设计本身也不够：两臂各自的基线由 PREPARE 独立生成，同一任务、同一数据、
同一留出集，写出来的强弱就能差 0.03 以上，而 SEARCH 改进也就 +0.05 量级——**基线方差与待
测效应同量级**。N=1 的两臂测不出文献接地的效果，不管它是正是负。

要真答这个问题，得让两臂**共用同一个冻结 evaluator 与同一个基线**，只让 ideation 不同；
或者每臂重复多次把基线方差平均掉。前者更省钱，也更干净。

> **方法论上的教训**：这一轮里 evaluator 报的分数错了两次（0.502 那次、0.691 这次），两次
> 都是"看起来合理的数字"。凡是要拿来下结论的分数，都该独立重算一遍再信——重算成本是几行
> 代码，而错误结论的成本是整条实验线。

单独跑一次 Ideator turn 观察算子使用（真实模型、真实语料，161.7 秒）：

| # | 算子 | 参数 | 耗时 | 命中 |
|---|---|---|---|---|
| 1 | `paper_semantic_search` | "class imbalance gradient boosting ROC-AUC" | 329.4 ms | 5 |
| 2 | `paper_chunk_read` | 2 篇论文的 3 个 chunk | 0.1 ms | 3 |
| 3 | `paper_keyword_search` | SMOTE · oversampling · class weight | 3.4 ms | 5 |
| 4 | `paper_chunk_read` | 2 个 chunk | 0.1 ms | 2 |
| 5 | `paper_chunk_read` | 2 个 chunk | 0.1 ms | 2 |

**检索合计 333 ms，占整个 turn 的 0.2%。** 引用与内容是对得上的：class-balanced loss
那条引 GBDT 不平衡研究，SMOTE 那条引 oversampling×ensemble 析因研究。

一个提示词依从性缺口：注入的指示要求**先**调 `paper_corpus_overview`，它没有，直接上了
语义检索。结果可用，但该算子存在的定位作用被跳过了。

## 七、CLI 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--survey` | 关 | 开启后台调研；不开则 `corpus_ref` 恒为 `None` |
| `--survey-query` | 空 | 调研主题；留空时由 `SURVEY_QUERY_PROMPT` 从 `--task` 改写 |
| `--survey-papers` | `DEFAULT_SURVEY_PAPERS`（10） | 转换并建索引的论文数 |

主题改写实测（原任务是一句含数据集路径的自然语言）：

> Survey of methods for handling severe class imbalance in tabular binary
> classification with a focus on optimizing ROC-AUC and ranking performance

按该主题取回的 8 篇全部切题。

## 相关文档

- [Academic Survey 全链路](academic_survey_ch.md) — 调研四段流水线与成本账
- [paper_rag 工具](paper_rag_tool_ch.md) — 七个算子的接口语义
- [paper_rag 真机基准](paper_rag_benchmarks_ch.md) — 检索质量与性能实测
- [真机跑测暴露的 loop 失效模式](loop_failure_modes_ch.md) — 同一轮跑测的其余缺陷
