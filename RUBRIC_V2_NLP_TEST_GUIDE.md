# Rubric V2 真实 NLP 测试指南：SMS Spam Collection

## 状态与目标

本指南是可执行方案，但本次 Codex 环境没有使用真实 API Key 执行，因此当前状态是 `NOT RUN / Pending local NLP validation`。

目标不是预设“SMS Spam 必须用 F1/AUC/Accuracy”，而是观察 Layer 1 是否根据任务、官方说明、类别分布和评价可行性给出有解释的单一 Primary；再观察 Layer 2 是否识别重复、文本重叠、泄漏、截断与成本风险，并实际改变候选优先级。

## 一、下载数据

官方数据页：<https://archive.ics.uci.edu/dataset/228/sms%2Bspam%2Bcollection>

1. 在页面点击 `Download`，保存 ZIP。
2. 在 Athena 根目录新建仅供本地验证的目录，例如 `local_data/sms_spam/`。
3. 解压后应得到无表头的 `SMSSpamCollection` 文件，每行是标签、Tab、短信正文。
4. 数据集和转换产物不要提交到 Rubric V2 PR；`local_data/` 若未被团队忽略，请在 Commit 前取消勾选。

## 二、整理为 CSV

在 Athena 根目录打开 PowerShell，先确认原文件位置，然后运行下面的临时转换脚本。它只整理字段，不选择评价指标：

```powershell
$script = @'
from pathlib import Path
import pandas as pd

source = Path("local_data/sms_spam/SMSSpamCollection")
target = Path("local_data/sms_spam/sms_spam.csv")
frame = pd.read_csv(source, sep="\t", header=None, names=["label", "text"])
assert set(frame["label"]) == {"ham", "spam"}
assert frame["text"].notna().all()
target.parent.mkdir(parents=True, exist_ok=True)
frame.to_csv(target, index=False)
print(frame.shape)
print(frame["label"].value_counts())
print(target.resolve())
'@
$script | uv run python -
```

记录输出中的行数和类别计数。不要删除重复文本来“美化”结果；重复/近重复正是 Rubric 应考虑的风险。若需要做 dedup 对照，另存一个文件并保留原始版本。

推荐目录：

```text
Athena/
  local_data/
    sms_spam/
      SMSSpamCollection
      sms_spam.csv
  .athena/
    sms-rubric-v2/       # 运行时生成，不提交
```

## 三、准备 Task Prompt

下面的 prompt 刻意不指定 Primary，便于测试 AI recommendation 路径：

```text
使用 sms_spam.csv 构建一个可复现的 SMS 垃圾短信二分类研究流程。目标列是 label，文本列是 text。请先检查类别分布、精确重复与近重复、潜在的 train/test 文本重叠和标签泄漏，再建立可信 baseline 并探索可验证的改进。评价政策必须在 baseline 之前确定；如果任务或官方协议没有明确 Primary，请根据科学目标与可实现性选择一个单一 Primary，解释方向、secondary metrics 与 guardrails，但只有 Primary 决定 SOTA。所有切分必须固定随机种子，并防止同一或近重复文本跨 split。
```

不要在 prompt 里加“Primary 必须是 F1/AUC/Accuracy”，否则测试的是 Human explicit precedence，而不是未知指标的 AI 选择。另做 Human 对照时，可以明确写出指标，并验证它是否覆盖 AI 建议。

## 四、配置 API

1. 复制 `.env.example` 为 `.env`。
2. 选择仓库支持的 provider：`deepseek`、`openai` 或 `qwen`。
3. Key 只放 `.env` 或当前 PowerShell 环境变量，不放 prompt、代码、日志、截图或 PR。

示例（只写变量名，值换成你自己的）：

```dotenv
LLM_PROVIDER=openai
OPENAI_API_KEY=你的真实Key
MODEL_NAME=团队批准的模型名
```

若用兼容端点，可按团队配置设置 `BASE_URL`。先运行普通 targeted tests，确认代码与 API 无关部分正常。

## 五、启动真实运行

PowerShell：

```powershell
$data = (Resolve-Path '.\local_data\sms_spam\sms_spam.csv').Path
$task = @'
使用 sms_spam.csv 构建一个可复现的 SMS 垃圾短信二分类研究流程。目标列是 label，文本列是 text。请先检查类别分布、精确重复与近重复、潜在的 train/test 文本重叠和标签泄漏，再建立可信 baseline 并探索可验证的改进。评价政策必须在 baseline 之前确定；如果任务或官方协议没有明确 Primary，请根据科学目标与可实现性选择一个单一 Primary，解释方向、secondary metrics 与 guardrails，但只有 Primary 决定 SOTA。所有切分必须固定随机种子，并防止同一或近重复文本跨 split。
'@
uv run python scripts/run_headless.py --project .athena/sms-rubric-v2 --data $data --task $task --search-limit 2
```

首次真实验证建议 `--search-limit 2` 控制时间/费用。成功后再按团队预算增加。

终端出现 `TERMINAL: ...` 才表示流程到达终态；出现 `RUN FAILED` 要保留错误和前后日志。

## 六、检查 Layer 1

按时间顺序检查终端和 `.athena/sms-rubric-v2` 下的状态/artifacts：

1. 任务理解中没有把 unknown 静默写成 Accuracy。
2. 出现 `Research Evaluation Rubric frozen`，且发生在 Evaluator freeze/baseline 之前。
3. Policy 有且只有一个 `primary_metric`，方向与 capability 一致。
4. `metric_source` 与场景相符：没有 Human/Official/Protocol 时应为 `ai`。
5. secondary、guardrails、confidence、explanation 存在且不是空套话。
6. evidence refs 都能在上下文/artifact store 找到；没有模型编造引用。
7. Evaluator 的 `metric.json` 与 Policy 的 Primary/direction 完全一致。
8. 后续 SOTA 日志只按 Primary 比较；secondary、guardrail、weights 没有改变 winner。

建议把 Policy JSON 脱敏复制到实验记录中，并写下“为什么这个指标适合任务”，不要只记名称。

## 七、检查 Layer 2

每个 Gate PASS 批次检查：

1. Ranking 发生在 Gate 后、graph registration/Selector 前。
2. 每个输入 hypothesis ID 恰有一个 review，没有缺失、重复或额外 ID。
3. 六个维度都在 `[0,1]`，confidence 在 `[0,1]`，explanation 非空。
4. `eda_evidence` 是否引用类别分布、文本长度、重复/近重复等真实发现。
5. `leakage_risk` 是否在适用时讨论 exact/near duplicate、跨 split overlap、target/prompt leakage；不适用时不应机械扣分。
6. `feasibility` 与 cost 是否符合 `search-limit 2` 的环境。
7. review artifact 可读，Hypothesis 上只存 score/ref。
8. Selector 使用 AI `rubric_score`；手动模拟 Rubric 失败时，不应留下伪造 score，并应回退 `rubric_prior`。
9. 未选中 hypothesis 仍为 `PROPOSED`。

## 八、实验与结果记录

至少记录两轮独立运行；若预算允许，用同一模型/参数、不同全新状态目录重复三次，观察 Policy 与选择顺序稳定性。

每轮填写：

| 字段 | 记录 |
|---|---|
| 日期、commit、模型/provider | |
| 数据文件 SHA256 | |
| Task Prompt 原文 | |
| search-limit / 随机种子 | |
| Layer 1 Primary / direction / source | |
| Secondary / guardrails | |
| Policy explanation / confidence | |
| Evaluator metric.json 是否匹配 | |
| Gate PASS 候选数 | |
| 每个候选六维分数、总分、解释 | |
| Selector 实际顺序 | |
| baseline Primary | |
| best Primary / 提升 | |
| leakage/duplicate 风险是否被发现 | |
| LLM 调用数、token/费用（若 provider 提供） | |
| 失败、重试、fallback | |

不要用 test set 反复挑模型。应预先固定 split/protocol；如果发现 duplicate overlap，记录修复前后协议差异，不能把污染结果当 SOTA。

## 九、截图与日志

建议截图：

- task 与配置（遮住 Key、用户名和本机敏感路径）
- Layer 1 frozen 消息与 Policy 摘要
- Evaluator metric.json 的 Primary/direction
- Gate PASS 与 Layer 2 batch 完成消息
- 候选总分/解释与 Selector 顺序
- baseline、best result 与终态

建议保留：终端完整文本、Policy artifact、Hypothesis review artifacts、research tree、Evaluator handoff/metric.json、运行 commit、数据 SHA256。

上传前搜索并删除 API Key、`.env`、Authorization header、本机个人路径和原始敏感文本。SMS 数据虽为公开数据，也不需要随 PR 上传。

## 十、问题排查

- `Missing LLM API key`：检查 `.env` 在 Athena 根目录、变量名与 provider 匹配；不要把 Key 写进源码。
- 401/403：Key、端点或账户权限错误。
- 429：限流/额度，降低并发或稍后重试并记录。
- Evaluation Policy 无法生成：查看是否 primary 为空、不支持、方向冲突或模型输出 schema 错误；没有显式 Primary 时安全停止是正确行为，不能手改成 Accuracy 冒充成功。
- Evaluator 一直修复：检查 `metric.json` 是否声明与 Policy 完全一致的 Primary/direction，`eval_script` 和 labels 是否存在。
- Layer 2 fallback：查看 batch 是否缺 ID、重复 ID、越界、未知 evidence ref 或超时。Fallback 不应写 AI score/explanation。
- 结果异常高：优先查 exact/near duplicates、跨 split overlap、label token、文件名/目录泄漏、预处理先看全量数据等。
- 结果不稳定：固定 split/random seed，记录模型温度/版本，比较 explanation 与候选顺序，而不是只看 best score。

## 完成判定

只有真实运行完成、证据保存、Policy/Evaluator 一致、两层时序正确、SOTA 只按 Primary、无泄漏且结果可复现，才能把 PR 中 `Pending local NLP validation` 改成实际结果。仅运行 unit tests 不能写“真实 NLP Passed”。
