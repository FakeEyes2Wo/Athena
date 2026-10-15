# 真机跑测暴露的 loop 失效模式

Status: current
Owner: Athena maintainers
Last verified: 2026-10-15
Source of truth: `src/athena/research/turns/ideator.py`, `src/athena/cli.py`,
`src/athena/research/supervisor/`, `src/athena/research/prediction_contract.py`

## 当前 SEARCH 修复与恢复规则（2026-10-15）

没有历史可信 best 时，Agent 的 `submit` 是执行前的提交意图，不代表脚本已经成功。
平台执行失败时，SEARCH 保留同一个 Plan、工作区与冻结评估器，把失败原因放入下一轮提示；
只有可信评分成功才更新 best。已有可信 best 时，`submit` 直接结算该存档的分数、代码提交与证据，
不会再次执行工作区，也不会把未评分的新改动当成 best；要评估新改动应选择 `continue`。
修复受该假设的 `turn_limit` 约束；GUI 的搜索上限控制假设实验数量，与一个假设内部的修复轮数不同。

每轮提示直接给出可信参考分数、历史最佳分数和对应 commit，明确训练集 OOF/CV 只属于诊断指标。
`continue` 后平台自动执行、评分并提交已评分修订；下一轮源码差异相对最新 commit 检查，
不会反复把之前已经提交的修改计为新实验。反馈中的“无源码变化”不能仅凭旧编辑记录认定为过期错误。
已有历史 best 并不证明当前工作区的所有文件都已评分；应对照 commit、Git 状态与实验日志。

新建 Plan 未指定轮数时默认使用 12 轮上限，自动生成的假设不再因为空限额而无限重试。
`patience=0` 表示停用基于连续无改进轮数的提前停止，不会在首个有效分数后直接结算；
明确提交/放弃和轮数上限仍然生效。正数 patience 继续按连续无改进轮数判断。
明确指定的限额仍有效；人工显式选择无限续跑，以及旧 checkpoint 已保存的限额语义保持。
每轮提示会给出当前假设的隔离工作区。Plan 的 shell `workdir` 必须位于该目录内；
把任务总目录误填为 `workdir` 会返回包含正确路径的纠错提示。依赖仍安装到共享环境，
外部数据仍通过绝对文件路径读取。

| 故障 | 当前处理 | 自动修复的边界 |
|---|---|---|
| manifest 不合法、脚本异常或超时、缺少声明输出 | 同 Plan 反馈后重试 | 反馈包含阶段、命令、退出状态与有界错误尾部；修复依赖代码和已允许的环境能力 |
| 无源码改动、只有输出/文档改动 | 同 Plan 反馈后重试 | 必须实际实现假设，不能以重跑父实验结算成功 |
| 预测缺行、多行、重复 ID、错分区或列契约不符 | 评分前拒绝，反馈后重试 | CSV 按声明 ID 列对齐；兼容旧的首列约定；非表格格式仍由相应评估契约负责 |
| Agent 正常完成后缺少结构化决策，或决策格式不合法 | 有界重试并保留诊断 | 不会因没有结果而无声丢失这轮失败 |
| Agent/执行/存储服务不可用、冻结评估器或冻结输入不可用 | 保留 Plan 并暂停 SEARCH | 需先恢复服务或数据，再显式恢复；候选 Agent 不得改冻结数据/评估器来消除错误 |
| CSV 重复/空表头、行宽不符、空目标或空训练/SEARCH 划分 | PREPARE 预检报错 | 数据所有者修正输入后再启动，不把不可用数据交给 SEARCH 无限尝试 |
| 源 CSV 占用平台保留 ID 列，或预分割数据不符合已配置的目标列 | PREPARE 预检报错 | 源 CSV 不能包含 `__athena_row_id`；已配置目标时，训练表必须有非空目标，SEARCH 特征不能包含目标；已有 SEARCH 平台 ID 必须唯一且非空 |
| 标签文件带元数据列、预测列说明有歧义 | 使用明确列契约 | `metric.json` 的 `label_column` 和 `prediction_column` 优先；无法唯一判断标签时明确报错 |
| 评估器标签只覆盖部分样本或取自错误分区 | 冻结前检查标签 ID 与对应特征 ID 完全一致 | SEARCH/FINAL 分别校验，差异反馈仅给数量；不完整 v2 契约不能退回 legacy 绕过必填项 |

CSV 探针使用标准 CSV 转义，支持值中的逗号、引号和换行；表头空白按同一规则处理。
VALIDATE 与 SEARCH 都使用声明的预测文件、ID 列与格式，诊断 CSV 不再混入预测行检查。

失败原因在修复期间保持持久化，并经过脱敏和长度限制。长 traceback 保留根因所在尾部，
下轮提示也保留它。明确放弃时也保留 Agent 的脱敏原因，且不再执行该次实验。
预算耗尽时，有历史可信 best 就保留该结果；没有 best 时，自动/跳过
验证模式结算为失败或无法判定，人工模式等待补充预算。不会把失败尝试写成可信分数。
基础设施暂停时，已在运行的其他 Plan 会收集完可信结果；SEARCH 尚有未完成 Plan 时不会
直接进入 VALIDATE 或生成完成状态。停止/取消必须能退出等待，取消后的迟到结果不再结算。
恢复时若该 Plan 的轮数已经用尽，需要先追加它的轮数预算；恢复操作不会绕过预算上限。

PREPARE 也会使用会话保存的单次实验超时。基线重试反馈包含稳定工作区、当前轮次、
剩余预算和脱敏后的错误尾部；重复失败会要求当轮实际修改、执行和验证，而不能只承诺
下一轮处理。轮数耗尽时，最终异常仍保留最后一次有界、脱敏的根因。
manifest 中的命令必须在每次执行时重新生成全部声明输出，包括报告；运行器会先归档旧输出，
只在执行前用文件工具写一份静态报告不能满足新鲜度契约。
来源标题核验失败会同时给出所选标题与权威记录标题，供 Agent 在原有有界
修复次数内纠正；不会放宽来源核验或修改已经封存的基线证据。
权威目录有时仅返回主标题。对 `XGBoost: A Scalable Tree Boosting System` 与 `XGBoost`
这类明确用冒号分隔的情况，允许足够长的主标题规范化后精确匹配；来源标识、年份、来源类型和引用门槛仍按原规则校验。
验证记录保留权威目录返回的标题，不声称已经核验目录中缺失的副标题。

Windows PowerShell 的路径和目录命令返回对象。命令包装会流式转成文本后再退出，
避免 PowerShell 5.1 的自动格式化尚未完成就退出，导致 Agent 误判工作区为空。
原生命令退出码、cmdlet/脚本错误及超时前日志均有回归覆盖；没有将整个输出缓存到变量。
原生程序向 stderr 写入警告、刷新后继续运行时，不会被 PowerShell 5.1 的错误记录误判而中断；
多行警告后的完成输出、非零退出码以及随后真正的 cmdlet 错误分别检查。

GUI 重启会从当前会话的持久化草稿恢复任务描述、任务理解卡片和可用操作。只有澄清草稿
的会话也属于有效会话。新建/切换会话要等后端绑定完成，首条任务不能落到前一个会话；
切换失败时保留可恢复的当前会话。进行中的澄清与工作区切换冲突时返回明确错误。
损坏的澄清草稿会显示恢复提示，重新开始任务前保留原文件备份；取消后返回的模型结果
不能覆盖取消状态。实验超时和容忍度与搜索上限一样按会话保存，重启后继续使用保存值。
恢复旧确认请求会再次检查取消状态和草稿版本，避免重复恢复或覆盖刚完成的取消；设置面板也会忽略旧会话延迟返回的读取结果。
恢复到待确认任务卡片后，同一会话的普通输入作为该草稿的修改指令，保留草稿身份并重新确认；
独立新任务通过“新会话”启动，不会误发新建请求而把旧任务卡片覆盖为失败。
暂停与后续 Agent 调度竞争时，明确的 `runtime paused` 拒绝保持等待状态；尚未启动的
Plan turn 退回轮数，不把这次控制操作误报成研究失败。
实验报告归档也按会话隔离，两个会话的 `exp_baseline` 不再冲突；恢复时从各自研究树
重建报告，默认旧会话仍使用原有归档目录。
PREPARE 的 EDA 协调 Agent 同样收到当前会话的绝对目录，并受 shell 工作目录限制；
不能把项目根下历史 `workspaces/eda` 报告当成新会话的分析结果。失败后恢复会重新汇总当前会话报告。

这些规则覆盖已验证的故障类型，不保证任意新数据集都可自动修好。模型即使收到准确 traceback，
也可能只描述修复而未实际执行，最终耗尽预算；新建但未接入执行入口的源文件也不能证明假设已实现。
源码差异门禁只拒绝明显的无变化/仅文档输出变化，不是对任意程序语义的证明。数据本身错误、
未声明的任务格式、权限或服务配置问题仍需要对应所有者处理。下面各节是 2026-08 的
历史跑测记录；当时的指标不代表本轮数据集的结果。

2026-08-16 用一个 6000×22、正例 5.98% 的不平衡二分类任务跑了 8 次完整 loop，模型是
qwen3.7-plus。本文记录其中四类失效：三个代码缺陷与两个环境前提。它们的共同点是
**单元测试全绿、真机一步都走不动**。

结构化输出那两个缺陷单独成篇，见
[结构化输出与工具调用](agent_structured_output_ch.md)。

## 一、成功的 Ideator lane 会把 SEARCH 打崩

`_run_ideator_lane` 的返回标注是 `-> HypothesisBatch`，但它的每一条 `return` 给的都是
`list`（门禁保留下来的假设，或空列表）。调用方 `run_ideator_turn` 按 batch 取值：

```python
hypotheses.extend(result.hypotheses)
request = (result.eda_request or "").strip()
```

于是**任何一条成功的 lane** 都会撞上：

```
supervisor> research failed: 'list' object has no attribute 'hypotheses'
```

失败的 lane 反而没事——它走 `isinstance(result, BaseException)` 分支被记为错误。所以这
个缺陷在"全部 lane 都失败"时是隐形的，恰好也是既有用例覆盖到的形态：
`test_gate_retry.py` 只调 `_run_ideator_lane` 本身，从不把返回值送回调用方。

顺带还有一个静默损失：门禁模式下 `eda_request` 根本传不出来，动态 EDA 是死代码。

### 修法

引入 `IdeatorLaneResult`，把两样东西一起带出 lane：

```python
@dataclass(frozen=True, slots=True)
class IdeatorLaneResult:
    hypotheses: list[Hypothesis]
    eda_request: str = ""
```

`eda_request` 只存在于 baseline 契约 `HypothesisBatch` 上，`IdeatorHypothesisBatch`
没有这个字段，因此取值走 `getattr(batch, "eda_request", None) or ""`——门禁模式不请求
补充 EDA 是事实，不是遗漏。

回归用例 `test_a_lane_that_succeeds_survives_the_trip_back_to_run_ideator_turn` 直接跑
`run_ideator_turn`，把 lane 与调用方接起来；去掉修复后它复现同一句
`AttributeError: 'list' object has no attribute 'hypotheses'`。

## 二、一个 ✅ 让已经 FAILED 的运行挂到超时

CLI 的终态判定原本排在渲染之后：

```python
def receive(kind, payload):
    renderer.render(kind, payload)      # ← 这里抛异常
    if kind != "state": return
    ...
    terminal.set()                      # ← 于是永远到不了
```

Agent 正文里出现一个 `✅`，Windows 上重定向后的 stdout 是 GBK，`print` 抛
`UnicodeEncodeError`。事件总线按设计吞掉订阅者异常（一个订阅者故障不该拖垮运行时），
所以进程既不崩也不退——实测在 `status=FAILED` 之后又活了 25 分钟，直到 `--timeout`。

### 修法

两条，都必要：

1. **先判终态，再渲染**，渲染包在 `try` 里。控制流不能挂在"打印成功"这个前提上；渲染
   失败只该少一行日志。
2. `main()` 里把 stdout/stderr 切成 `encoding="utf-8", errors="replace"`，从源头让这类
   字符可打印。

回归用例 `test_a_run_that_fails_still_exits_when_its_output_cannot_be_printed` 用一个
会对非 GBK 字符抛异常的 stdout 替身，并且**断言 stderr 里没有 `timed out`**——超时兜底
和正常终态都返回 1，只断言退出码的话这个用例在修复前也会"通过"。

## 三、报不出字段名的修复反馈 = 必然耗尽预算

`experiment.json` 的 `ExperimentManifest` 是 `extra="forbid"`。agent 多写了一个
`metrics` 块，收到的反馈是：

```
<field>: Extra inputs are not permitted
```

`_manifest_validation_summary` 会把不属于 `_MANIFEST_FIELDS` 的路径段一律遮成
`<field>`，用意是不让 manifest 内容漏进反馈。但对 `extra_forbidden` 来说，出问题的键
**按定义**就不在 schema 里，所以它永远被遮掉——agent 连删哪个键都不知道，原样重交三次
直到 PREPARE 轮次预算耗尽。它甚至已经把基线跑出来了（ROC-AUC 0.8660，5 折分层 CV）。

### 修法

只对 `extra_forbidden` 放行键名，且先消毒：

```python
def _safe_field_name(part: object) -> str:
    printable = "".join(char for char in str(part) if char.isprintable())
    return printable[:_MAX_FIELD_NAME_CHARS] or "<field>"
```

键名是 agent 自己写的、长度有界，回给它不构成泄露；manifest 的**取值**仍由
`include_input=False` 挡在外面，其余错误类型的路径照旧遮蔽。消毒去掉不可打印字符并截到
40 字，避免超长键或换行伪造出新的一行反馈。

三个用例分别钉住：多余键必须被点名、恶意键名必须被截断、其余错误仍然遮蔽。

### 同族问题（未修）

VALIDATE 在同一次跑测里以同样的方式耗尽预算：16 次修复尝试，每次都是

```
Validation policy rejected the repair: validation diff is binary or cannot be reviewed safely
```

反馈没说**哪个文件**是二进制。修法方向相同，但需要先弄清策略判定口径，未在本轮处理。

> **通用规则**：任何进入"反馈 → 重试"闭环的错误消息，必须包含 agent 据以采取不同行动
> 的信息。说不出"改哪里"的反馈，重试预算越大只是把失败拖得越久。

## 四、两个环境前提，值得在启动时自检

都不是代码缺陷，但各自吃掉了一次完整跑测。

### `uv` 必须在 `PATH` 上

`_freeze_evaluator` 通过子进程调 `uv lock`。`uv` 不在 `PATH` 时报 `[WinError 2]`，
agent 无从修复，连试 11 次后轮次预算耗尽。注意 agent 自己 `pip install uv` 也没用——它
装到了另一个解释器的 user site，父进程的 `shutil.which("uv")` 仍然找不到。

### 项目路径必须留够 `MAX_PATH` 余量

evaluator 工作区里的隔离 venv 会长出这样的路径：

```
<project>/workspaces/evaluator/.venv/Lib/site-packages/sklearn/metrics/
_pairwise_distances_reduction/_middle_term_computer.cp313-win_amd64.pyd
```

从项目根往下就是 **133 个字符**，也就是说项目根只剩 127 字符的额度。一旦超出，这个
`.pyd` 就越过 Windows 的 `MAX_PATH`（260），文件在磁盘上存在但加载器打不开，表现为一个
极具误导性的错误：

```
ModuleNotFoundError: No module named
  'sklearn.metrics._pairwise_distances_reduction._middle_term_computer'
```

实测路径长 264 字符、`LongPathsEnabled = 0`。把项目根换到 37 字符的路径后同一份代码一
次通过。agent 自己的诊断是对的（"this is an environment issue, not a baseline issue"），
但它没有能力绕开。

建议在 `run` 启动时就检查：`shutil.which("uv")` 非空；`len(project_root) <= 127`
或 `LongPathsEnabled = 1`。两项都是毫秒级，而失败的代价是几十分钟的模型时间。

## 五、一个字都没改的候选也算实验

评估修好之后（见 [Evaluator 契约](evaluator_contract_ch.md)），第 9 次跑测立刻照出一条
此前被恒定 0.502 盖住的问题：

```
baseline commit f2cc5597fbea  primary=0.882326
80bf18bfa3c2    f2cc5597fbea   no   0.882326   REFUTED
3342c5a12da2    f2cc5597fbea   no   0.882326   REFUTED
0a86e624f87f    f2cc5597fbea   no   0.882326   REFUTED
aa77d5008be3    f2cc5597fbea   no        -     FAILED
```

4 个候选的 commit **全部等于 baseline**，predictions artifact 逐字节相同，分数一模一样。
看 Agent 日志就清楚了：它读了继承来的 `solution/train_model.py`、读了
`predictions/metrics.json`、**原样重跑了一遍基线脚本**，然后提交——

> "The hypothesis has produced a working solution with ensemble AUC of 0.8823"

它压根没实现自己那条 one-hot encoding 改动，只是看见数字不错就交了。三条因此被判
REFUTED：又一次"实验从没发生却给出自信判决"，只是这回坏的不是评估，是候选。

`PlanRunner` 现在在打分之后、提交之前判这一刀：SEARCH 计划拿到空 diff 就返回
`kind="no_change"`，错误文本直接说"你没有改动任何文件，因此重跑的是父实验"，走既有的
同 Plan 反馈重试链。只约束 SEARCH——PREPARE 基线本来就没有"相对谁的改动"。

## 六、修完之后的完整跑测

第 9 次跑测首次走到 `COMPLETED`：PREPARE → SEARCH → VALIDATE → COMPLETED，基线
ROC-AUC **0.8823**（此前恒定 0.502），调研在同一进程内建好语料。

第 12 次是第一次 SEARCH 真的在做实验：

```
hypothesis        commit   primary    cited  status
baseline          -        0.879049   -      SUCCEEDED
0373fd55be93      YES      0.880641   False  SUCCEEDED   <- SOTA
1cbe2524cae1      YES      -          True   FAILED
377c8699e96f      YES      -          True   FAILED
ce8bb6989abd      YES      0.871740   True   SUCCEEDED
afd5f68a98f0      YES      0.700031   True   SUCCEEDED
abfef4a2930c      YES      -          True   FAILED

distinct scores: 4 / 4      sota: exp_hyp_0373fd55be93
```

- **每个候选都产生了新 commit**，分数四个各不相同（0.879049 / 0.880641 / 0.871740 /
  0.700031）。SOTA 从基线推进到 0.880641——这是所有跑测里第一次出现真实的搜索进展。
- **`no_change` 一次都没触发**。这不是它失效，是它没机会：候选真的在改文件了。所以这条
  判据目前只有单元回归背书，真机上尚未被触发过一次。
- 三条带引用的假设 FAILED（`settled without a trusted result`），另两条跑出了分数但都低
  于基线。见 [文献语料接入 loop](corpus_ideation_ch.md) 第五节。

VALIDATE 仍以第三节末尾那个同族问题告终（`validation diff is binary`），未处理。

## 相关文档

- [结构化输出与工具调用](agent_structured_output_ch.md) — 同一轮跑测里的另外两个缺陷
- [Evaluator 契约](evaluator_contract_ch.md) — 那个"安静地成功"的失效
- [文献语料接入 loop](corpus_ideation_ch.md) — 带引用的假设从哪来
- [Supervisor 设计基线](supervisor_design.md) — 三阶段与门禁的目标契约
