# Evaluator 契约：按 id 对齐，不按位置

Status: current
Owner: Athena maintainers
Last verified: 2026-08-16
Source of truth: `src/athena/core/agent/prompts/evaluator_agent.md`,
`src/athena/research/supervisor/prepare.py`,
`test/unit/research/supervisor/test_prepare_plan.py`

PREPARE 让一个 Agent 自己写评估脚本，冻结成不可变 bundle，之后 SEARCH 与 VALIDATE 用它
给每个候选打分。**这个 Agent 可以写出一个跑得通、打印得出数字、却什么都没测量的
evaluator**，而整条 loop 不会察觉。本文记录这个失效、它的根因，以及现在拦在哪。

## 一、症状：每个候选恒定得 0.502

2026-08-16 的真机跑测里，SEARCH 跑完全程、给出了自信的判决：一条假设 REFUTED、一条
INCONCLUSIVE。复核时发现 baseline 与被判 REFUTED 的候选：

- `eval.primary` 完全相同：`0.5020316193853428`
- `artifacts.predictions` 引用**逐字节相同**：`sha256:ecf1574f…`
- `artifacts.report` 引用同样相同

同一份预测文件，两种打分方式：

```
按 evaluate.py 的算法        -> AUC = 0.502   （随机）
按 row_id join 真实标签      -> AUC = 0.8668
```

模型一直是好的。是评估把信号毁掉了。

## 二、根因不在代码，在契约

冻结下来的 `evaluate.py` 核心是这三行：

```python
min_len = min(len(y_true), len(y_pred))
y_true = y_true[:min_len]     # 1200 行，留出集，只有一列 label
y_pred = y_pred[:min_len]     # 6000 行，带 row_id
```

它把两边截到较短长度后**按位置**比较。留出集的第 i 个标签，对上的是全量预测的第 i 行
——毫不相干的两个样本。于是任何候选都得到 ≈0.5，SEARCH 在结构上不可能区分出优劣。

但写出这段代码的 Agent 并没有违反任何规定：

- 它自己的 `HANDOFF.md` 示例就是一列裸 `probability`，`index` 列标注为 *optional*；
- 当时的 `evaluator_agent.md`（66 行）只说"hold out a validation split yourself"，
  **从未要求 join key，也从未说明候选该预测哪些行**；
- 候选 Agent 交出 `row_id,probability` 覆盖全部 6000 行，同样没有违反任何规定。

契约定义了格式，没定义**对应关系**。三方各自自洽，合起来是空的。

## 三、现在拦在哪

### 冻结前的结构性检查（必要条件）

`_freeze_evaluator` 在冻结前要求 `labels.csv` 至少两列：

```python
if len(columns) < 2:
    raise ValueError(
        "labels.csv must carry a row-id column named '__athena_row_id' next to "
        "the target so predictions can be joined by id, …"
    )
```

只有一列时 join 在结构上就不可能，所以这一条能静态判掉，且错误直接变成 Agent 的修复
反馈（`ValueError` 走既有的 feedback 重试链）。

**这是必要条件，不是充分条件**：带了 id 列的脚本仍然可以写成按位置对齐。

### Prompt 里的行为要求

`evaluator_agent.md` 新增一节，把三件事写死：

- `labels.csv` 必须带 `__athena_row_id` 列（沿用 `init_agent.md` 早就定下的列名），id
  取原始数据文件里的行序，候选无需猜测即可复现；
- `evaluate.py` 必须按该 id join，**不得依赖行序，不得截到较短长度**；
- id 对不上（标签有、预测没有，或反之）是**错误**，打印 `{"primary": 0.0}` 并向 stderr
  写一行诊断——静默地只给交集打分会把坏候选藏起来；
- `HANDOFF.md` 必须写明 id 列名与候选应当预测的行集合（留出的那批 id，不是整份数据）。

并要求 Agent 在 `submit` 前跑两个探针——**两个都要，缺一不可**：

1. 把预测文件的**行**打乱（每个 id 仍带着自己的值）重新打分，**分数必须不变**。变了
   就说明脚本在读行序。
2. 保持 id 不动，把**预测值**在 id 之间打乱，重新打分，**分数必须变化**。不变就说明
   join 根本没有喂给指标。

> 这两条最初写反过一次：文档与 prompt 都要求"洗牌后分数必须不同"。按 id join 的脚本
> 对行序本来就免疫，分数不变才是正确表现——第一版探针会把正确实现判为失败。修正后在
> 真机复核过：行打乱 0.8823 → 0.8823（不变，正确），值打乱 0.8823 → 0.4742（变化，
> 正确）。

两条合起来才说明"分数只取决于哪条预测属于哪一行，且确实取决于它"。

## 四、修复后的真机复核（2026-08-16 第二轮）

改完 prompt 与冻结检查后重跑一次完整 loop，PREPARE → SEARCH → VALIDATE → COMPLETED。
Agent 自己写出来的 evaluator：

- `labels.csv` 头是 `__athena_row_id,label`，1200 行留出集，id 是原始数据的行号；
- `evaluate.py` 用 `pd.merge(labels_df, pred_df, on="__athena_row_id", how="inner")`，
  并对"标签有而预测没有""预测有而标签没有"两个方向都显式报错退出；
- 全文没有 `min(len(...))` 之类的截断。

基线分数 **0.8823**（此前恒定 0.502）。两个探针的实测：

| 探针 | 分数 | 判定 |
|---|---|---|
| 原样 | 0.882326 | — |
| 行打乱（id 与值一起移动） | 0.882326 | 不变 → 确实按 id join |
| 值在 id 之间打乱 | 0.474217 | 变化 → join 确实喂给了指标 |

## 五、契约必须走 content，`context_refs` 到不了 model

严格化之后第一次复跑，基线直接判 0.0——不是评估器的错，是**候选侧根本没看到契约**。

写 `predictions/` 的有三类 Agent：PREPARE 的基线、每个 SEARCH 候选、以及提假设时要参考
格式的 Ideator。此前只有 Ideator 那条路"附了"契约，而且是附进 `context_refs`：

```python
input_text = trigger.content if trigger is not None else ""   # base_runner
```

`base_runner` 只把 trigger 的 **content** 当作 model 的 user prompt。`context_refs` 从
来没有被解析回正文——它只在"未读 mailbox 消息"那条路径上被拼成信封，拼的还是 ref 字符
串而不是内容。**这是一条死信道。**

真机（2026-08-16 第 11 次）证据：

- Ideator 的整条 user prompt 只有 374 字符，末尾写着 "The evaluator contract … is
  attached as context; read it before proposing hypotheses"——契约一个字都不在里面。
  这句空头支票在 main 上已经存在很久。
- PREPARE 的日志前几轮里 `__athena_row_id` / `eval_handoff` 一次都没出现，基线因此交出
  `id,probability` 覆盖全部 6000 行。

反过来说，Ideator 之所以真能用上语料，恰恰因为 `corpus_ref` 那段是拼进 **content** 的。

现在 `handoff_block()` 把契约拼成一段带边界标记的正文，三个入口都走 content。
`context_refs` 仍然照留，供事后审计与重放，只是不再假装它能送达。

> **对测试的教训**：旧用例断言的是"ref 出现在 `context_refs` 里"。它在真机全线失效的整
> 段时间里一直是绿的——因为它验证的是数据被放进了一个没人读的字段。断言要落在**能改变
> model 行为的那个字段**上。

改完之后第 12 次跑测复核：基线自己写出 `__athena_row_id,prediction` 的预测文件，分数
**0.8790**（改之前是 0.0）；五个 SEARCH 候选跑出四个**互不相同**的分数
（0.880641 / 0.871740 / 0.700031，加基线 0.879049），SOTA 从基线推进到 0.880641。
指标终于能区分候选了。

## 六、第三种失效：把 `predictions/` 里的东西全拼起来打分

A/B 的 survey-on 臂看起来"文献让假设变差了"：基线 0.6906、最好候选 0.7161，而 survey-off
臂是 0.8637 → 0.9137。**这个读数是假的。**

自己从 artifact 里把预测重算一遍，才发现两边对不上：

| | evaluator 报的 | 独立重算 |
|---|---|---|
| survey-off 基线 | 0.863697 | 0.863697 ✓ |
| survey-on 基线 | **0.690575** | **0.876182** ✗ |

原因在 `predictions/` 目录里躺着两个文件：

```
pred_test.csv    n=1200  mean=0.5172   AUC=0.5214   <- 基线自测时留下的草稿
predictions.csv  n=1200  mean=0.0800   AUC=0.8944   <- 真正的预测
concat 两者      n=2400               AUC=0.6931   <- evaluator 实际打的分
```

评估器把 `predictions/` 下每个 `*.csv` 都 `pd.concat` 起来。两个文件用的是同一批 id，
于是每个 id 出现两次，**打的是"真模型与一个随机文件各半"的混合分**。而这个草稿文件躺在
基线的 commit 里，**每个候选都继承了它**——整条臂的每个分数都被同样地压低。

真实情况恰恰相反：survey-on 臂的基线预测本身是 **0.894417**，比 survey-off 臂的 0.8637
**更好**。所谓"加了 survey 假设更差"完全是这个混合造成的假象。

修法在契约层：

- evaluator 必须**读契约点名的那一个文件**，不再 glob-concat；`__athena_row_id` 重复出现
  即报错退出——一个 id 两条预测本来就是有歧义的。
- `HANDOFF.md` 必须写明**唯一的预测文件名**，让候选无从猜、也无从多写。
- PREPARE 与候选的提示词都明说：提交前删掉自测草稿，留在真预测旁边会被一起打分，并且会
  被后续每个候选继承。

> 这三条都还是 prompt 级别的。平台侧无法直接查重复 id（它不知道预测文件的格式），真正的
> 兜底仍是下一节那个判别性检查。

## 七、还没做：把判别性检查搬进平台

上节两个探针目前写在 prompt 里，靠的是模型自觉。真正牢靠的做法是冻结后由平台自己跑一
遍。没有立刻做，是因为它需要平台知道预测文件的列名与格式，而那恰恰是当前契约没有规定
死的部分——先把契约收紧，再谈自动验证。做这一步时应当同时把预测格式固定成
`__athena_row_id,prediction`，与 `init_agent.md` 的既有约定合并。

## 八、为什么这条值得单独立一篇

其他缺陷的失败是响亮的：崩溃、卡死、预算耗尽。这一条**安静地成功**——SEARCH 跑完，
research tree 里躺着状态齐全的实验记录，REFUTED/INCONCLUSIVE 一应俱全，而它们全部没有
信息量。一个自动研究系统里，能产出可信外观的错误结论比崩溃危险得多。

任何"由 Agent 生成、之后被当作事实基准"的产物都该配一个判别性检验：**它对本该不同的
两个输入，给不给出不同的输出。**

## 九、分数还得带上它的不确定度

`evaluate.py` 现在必须打印 `test_se` 与 `test_n`，而不只是 `primary`：

```json
{"primary": 0.8907, "test_se": 0.0231, "test_n": 7259}
```

这两个字段的通路早就是通的：`TrustedEvaluator` 读它们（`evaluation.py`），
经 `PlanBest.std_error/n` 到 `plan_lifecycle._settle_plan`，那里
**只有在 `std_error` 非空时**才走 `settle_statistically`——Bonferroni 校正的正态
置信区间；否则退回比较两个点估计。

也就是说：机制一直在，只是没有任何一处要求评估器把数吐出来，于是它从来没被启用过。
留出集一小或一不平衡，纯噪声就会被记成一次 WIN，而搜索会顺着这个噪声继续走下去，
后续每一条假设都建立在一个不存在的改进之上。

`test_se` 用 bootstrap 估：对被打分的记录有放回重采样 ≥1000 次（固定种子），
每次重算指标，取这些值的标准差。指标若是"逐条得分求平均"这种形式，
`std / sqrt(n)` 等价且更省。

指标确实没有可估计的抽样分布时，可以在 `HANDOFF.md` 里说明并有意省略——
但默认省略就是让搜索去优化噪声。

## 相关文档

- [Supervisor 设计基线](supervisor_design.md) — 三阶段与 trusted evaluator 的位置
- [真机跑测暴露的 loop 失效模式](loop_failure_modes_ch.md) — 同一轮跑测的其余缺陷
- [接真实评测任务时暴露的七个缺陷](task_readiness_ch.md) — 数据划分、超时、provider 与失败反馈
