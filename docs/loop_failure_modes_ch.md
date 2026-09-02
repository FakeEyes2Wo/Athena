# 真机跑测暴露的 loop 失效模式

Status: current
Owner: Athena maintainers
Last verified: 2026-08-16
Source of truth: `src/athena/research/turns/ideator.py`, `src/athena/cli.py`,
`src/athena/research/supervisor/experiment.py`

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
