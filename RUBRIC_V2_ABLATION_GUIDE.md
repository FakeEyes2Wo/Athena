# Rubric V2 消融实验指南

## 当前状态

方案已提供，但本次没有真实执行，状态为 `NOT RUN`。任何 PR 都不应在取得日志前声称 V2 优于 V1。

## 研究问题

### Ranking

```text
V1：原 deterministic rubric_prior
vs
V2：Gate 后 LLM Hypothesis Ranking Rubric + deterministic aggregation
```

比较的不是“LLM 分数是否看起来更聪明”，而是：同一批合格候选的排序是否更有证据、是否更少选择不可验证/泄漏风险高/成本不合适的实验，以及最终研究效率是否改善。

### Evaluation

比较 unknown metric 的旧式默认/人工预设控制与 V2 pre-baseline Policy，观察 V2 是否减少错误 Primary、方向错误和 Evaluator/Policy 不一致，同时保持只有 Primary 决定 SOTA。

## 实验前固定项

- 使用同一 commit、Python/依赖、provider、模型版本、任务 prompt、数据 SHA256、split、随机种子、search-limit、concurrency 和预算。
- 保存所有输入候选、Gate 结果、Rubric artifacts、Selector 顺序、Plan/Experiment 结果和 token/费用。
- 不因为看到 test score 后再修改 Primary 或 prompt。
- 至少 3 个独立 seed；模型有随机性时，建议每个条件 3 次并报告均值与原始值。
- V1/V2 状态目录必须分开，不能共享 research tree 或 resume state。

## A. Ranking-only 配对消融（首选，成本低）

该设计保证两种方法看到完全相同的 Gate PASS 候选，最能隔离 Ranking 本身。

### 1. 采集同一批候选

正常运行 V2 到第一次 `AI Hypothesis Rubric scored N candidates`，保存：

- Layer 1 Policy
- Gate PASS 后的候选 JSON（ID、statement、intervention、expected_effect、cost、sources、evidence refs）
- baseline/SOTA/history/EDA 上下文
- 每条 Rubric review 和聚合 score

### 2. 计算 V1 排名

对保存的同一 Hypothesis 列表，把 `rubric_score` / `rubric_ref` 视为 `None`，用当前保留的 `rubric_prior` 和同一个 Selector config 计算顺序。不要重新调用 Ideator，这样候选不会变化。

### 3. 计算 V2 排名

使用已保存的 `rubric_score`，用同一个 Selector config 计算顺序。Selector 不应产生新的模型调用。

### 4. 比较

记录：

- Top-1 是否变化
- Top-k Kendall/Spearman 排序差异
- 选中项的 verifiability、EDA evidence、feasibility、cost/leakage risk
- explanation 是否引用了真实 evidence
- V2 是否发现 exact/near duplicate、跨 split overlap、target/prompt leakage、tokenization/truncation、LLM judge 稳定性等适用风险
- 额外 LLM 调用、token、延迟和费用

### 5. 可选：执行两边 Top-k

如果预算允许，从同一个 baseline 分别执行 V1 与 V2 的前 2–3 个 hypothesis。每个 Plan 必须使用独立 workspace，但 evaluator、split、预算和随机种子一致。

记录执行成功率、无效/超预算次数、相对 baseline 的 Primary 改善、产生新 SOTA 所需实验数。不要只挑最好的一次报告。

## B. End-to-end Ranking 消融

本项目没有为消融新增长期 production flag。可以用本地临时 wrapper 禁用 Rubric callback；该文件不要提交：

```python
# local_ablation_v1.py（仅本地实验，不提交）
from athena.research.agent_turn_runner import AgentTurnRunner
from scripts.run_headless import main


async def _v1_ranking(self, hypotheses):
    return hypotheses


AgentTurnRunner.run_hypothesis_rubric = _v1_ranking
raise SystemExit(main())
```

V1：

```powershell
uv run python local_ablation_v1.py --project .athena/sms-ablation-v1-seed1 --data $data --task $task --search-limit 3
```

V2：

```powershell
uv run python scripts/run_headless.py --project .athena/sms-ablation-v2-seed1 --data $data --task $task --search-limit 3
```

V1 中 hypothesis 不会获得 AI score/ref，Selector 因此走原 `rubric_prior`；V2 正常生成 review。wrapper 只是实验时 monkeypatch，不修改仓库源码。

注意：两次端到端运行的 Ideator 输出可能不同，因此它衡量“完整策略表现”，不能取代 A 的同候选配对比较。

## C. Research Evaluation Rubric 简单对照

### C1. Policy-only 离线对照

对每个任务保存任务理解上下文，比较：

- Legacy reference：若没有明确 Primary，记录旧行为会得到 `accuracy/maximize`；只作为离线历史参照，不把这个默认重新写回 production。
- V2：Human > Official > Protocol > AI，经过 capability/direction validation 的 Policy。

人工盲评（不知道后续 baseline 分数）以下项目：

- Primary 是否与研究目标一致
- direction 是否正确
- 官方/协议是否被 Human 正确覆盖或被正确尊重
- secondary/guardrails 是否有用但没有影响 SOTA
- explanation 是否说明取舍
- unsupported/empty output 是否安全失败

建议至少覆盖：明确 Human Primary、明确 Official metric、明确 Protocol metric、unknown 分类、minimize 回归、一个 unsupported metric。

### C2. 真实运行对照

对 unknown-metric NLP task：

- V2 arm：使用不指定 Primary 的 prompt，让 Evaluation Rubric 选择并冻结。
- Explicit-control arm：研究负责人在运行前明确指定一个 Primary/direction；系统应锁定 Human 指标。该指标必须在看到实验结果前确定。

这不是为了证明某个指标必然正确，而是比较：Evaluator 生成成功率、Policy/metric.json 一致率、方向错误数、SOTA 决策可解释性和最终结论敏感性。

若团队确实要重跑旧版本，必须使用独立 checkout/容器，不要在 V2 branch 中恢复 unsafe unknown→accuracy 代码。

## 记录模板

| 字段 | V1 / Control | V2 |
|---|---:|---:|
| commit / seed / model | | |
| Gate PASS candidate IDs | | |
| Top-1 / Top-k order | | |
| 执行成功数 / 总数 | | |
| 产生新 SOTA 的实验数 | | |
| best Primary 相对 baseline 改善 | | |
| leakage/duplicate 风险命中 | | |
| 无证据或编造 evidence 数 | | |
| LLM 调用 / token / 费用 | | |
| wall-clock 时间 | | |
| fallback / retry 次数 | | |
| Policy Primary / direction / source | | |
| Policy-Evaluator mismatch | | |

## 判定规则

实验开始前写下判定规则。例如：

- V2 不能出现未知 evidence ref、缺/重/额外 ID 或 Selector-time LLM call。
- V2 的 evaluator mismatch 必须为 0；unknown + LLM failure 必须安全停止。
- 只有 Primary 用于 SOTA，两个 arm 均检查这一点。
- 效果报告同时包含质量、成功率、风险发现、成本与延迟，不能只报告 best score。
- 如果 V2 成本上升但没有稳定质量/风险收益，应结论为“不足以证明有效”，而不是选择性展示成功 seed。

## 完成后

把原始记录、汇总表、失败样例和费用加入 PR 或独立实验报告。只有真实执行后，才把 `RUBRIC_V2_PR_TEMPLATE.md` 中的 Ablation 从 `NOT RUN` 更新为带命令和证据的结果。
