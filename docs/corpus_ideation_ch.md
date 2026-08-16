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
0 条 `sources`，而且**引用核验拿到空集合，任何编造的 paper id 都会静默放行**——这比没有
语料更糟，因为 `ranker.rubric_prior` 给"有引用"加 0.12 分。

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

## 四、引用必须可核验

`ranker.rubric_prior` 里 `evidence = 1.0 if hypothesis.sources else 0.0`，按
`0.4 + 0.3*evidence + 0.3*specific` 与 `prior_weight=0.4` 折算，**非空 `sources` 值
+0.12 的最终得分**。不校验就是在直接奖励幻觉。

`_verify_sources` 在假设入图前把 `sources` 与语料实际 paper id 求交：

```python
known = await rt.corpus_paper_ids()
if not known:
    return hypotheses          # 没有语料就没有比对基准，清空只会误伤别的来源
kept = [s for s in hypothesis.sources if s in known]
```

被丢弃的引用会发一条 error 输出说明数量与原因。门禁臂与 baseline 臂都走这一步——排序
对两条臂是同一个。

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
seq 785 就启动了，0 次 `paper_*` 调用、6 条假设 0 条引用。约束四正是为此加的，加完之后
这条路径尚未复跑验证。

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

## 六、CLI 参数

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
