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

并要求 Agent 在 `submit` 前自查一次：**给预测文件洗牌后重新打分，两次分数必须不同。**
相同就说明脚本在按位置对齐。

## 四、还没做：运行期判别性检查

自查写在 prompt 里，靠的是模型自觉。真正牢靠的做法是平台自己验：冻结后用同一份预测跑
两次，第二次把行打乱，分数必须变化。

没有立刻做，是因为它需要平台知道预测文件的列名与格式，而那恰恰是当前契约没有规定死的
部分——先把契约收紧，再谈自动验证。做这一步时应当同时把预测格式固定成
`__athena_row_id,prediction`，与 `init_agent.md` 的既有约定合并。

## 五、为什么这条值得单独立一篇

其他缺陷的失败是响亮的：崩溃、卡死、预算耗尽。这一条**安静地成功**——SEARCH 跑完，
research tree 里躺着状态齐全的实验记录，REFUTED/INCONCLUSIVE 一应俱全，而它们全部没有
信息量。一个自动研究系统里，能产出可信外观的错误结论比崩溃危险得多。

任何"由 Agent 生成、之后被当作事实基准"的产物都该配一个判别性检验：**它对本该不同的
两个输入，给不给出不同的输出。**

## 相关文档

- [Supervisor 设计基线](supervisor_design.md) — 三阶段与 trusted evaluator 的位置
- [真机跑测暴露的 loop 失效模式](loop_failure_modes_ch.md) — 同一轮跑测的其余缺陷
