# Rubric V2 SUPPORT2 修复后全流程真实运行记录

日期：2026-08-28  
数据：公开 SUPPORT2 全量数据，9,105 行、16 个输入特征和 `mortality_180d` 标签列  
任务：180 天死亡率二分类预测  
运行配置：DeepSeek `deepseek-v4-flash` + `deepseek-v4-pro` reasoning，`search-limit=3`，`ideator-count=3`，`hypotheses-per-ideator=5`，`concurrency=1`

## 1. 结论摘要

本次是修复后的真实付费 API 端到端运行，不是 mock。运行最终以退出码 `0` 到达：

- `phase=COMPLETED`
- `status=COMPLETED`
- SEARCH 三个实验预算全部结算
- VALIDATE 完成
- 最终预测和标签 ID 完整对齐

本次最重要的结论是：此前影响较大任务的结构化输出、断点恢复、SEARCH 完成完整性和最终 VALIDATE 闭环问题，在该次全量 SUPPORT2 运行中没有再次阻断流程。

本次结果适合证明“修复后的 Rubric V2 工作流可以在真实公开数据、真实 DeepSeek API 和 `search-limit=3` 下完成闭环”。由于本次与此前运行采用的基线模型和数据切分不同，本报告不把指标差异表述为跨版本性能提升。

## 2. 数据完整性与固定任务

- 数据文件：`local_data/support2/prepared/support2_180d.csv`
- 数据规模：9,105 行，17 列（16 个输入列和 1 个目标列）
- 目标列：`mortality_180d`
- 类别计数：0 类 4,840 行，1 类 4,265 行
- 数据 SHA-256：`DF8F44EAA79CAD65A4CF562B08ADF4609292DAB4CAACFCD672F8E4BF9CB4B563`
- 抽样：无；使用全部 9,105 行
- 主指标：AUROC/ROC-AUC
- 优化方向：`maximize`

任务理解状态为 `READY`。流程继续遵守泄漏字段禁用、缺失值必须在训练管线内处理、实验失败不得伪造分数等约束。

## 3. Layer 1：评价策略冻结

本次运行沿用唯一、冻结的主评价策略：

- primary metric：`roc_auc`
- direction：`maximize`

主指标在 PREPARE、SEARCH、Selector 和 VALIDATE 之间没有被静默改写。SEARCH 候选仍以可信 evaluator 输出的 AUROC 进行比较；辅助 AUPRC 和 Brier 只用于解释，不替代主指标。

## 4. PREPARE、EDA 与可信基线

PREPARE 最终完成，并建立了可复现基线：

- 模型：`RandomForestClassifier`
- 参数摘要：800 trees，`min_samples_leaf=2`，`random_state=42`
- 预处理：数值列中位数插补和缩放；分类列众数插补和 one-hot encoding
- 附加特征：每行数值变量缺失数量 `_missing`
- SEARCH 训练行：7,284
- SEARCH 验证行：1,821

基线 SEARCH 指标：

- AUROC：`0.7516216464980187`
- AUPRC：约 `0.7352`
- Brier：约 `0.2015`

部分 EDA worker 曾以 `max turns exhausted without structured output` 结束，但 PREPARE 能够保留已有产物、继续完成数据分析和基线构建，没有因此把整个流程标记为失败。这说明 EDA 子任务失败已能被隔离，但它仍是值得后续减少的运行成本。

## 5. Ideator 与 Layer 2 Rubric

三个 Ideator 使用 Pro reasoning 进行候选生成。候选经过可证伪性检查、重复/有效性处理后，Layer 2 对 12 个有效候选完成一次批量 Rubric 评分：

`[rubric_status=success] 假设优先级 Rubric 已批量评分 12 个候选。`

本轮没有进入 Layer 2 deterministic fallback，因此评分可报告为完整 Rubric 成功，而不是降级成功。Selector 消费已保存的分数，没有在选择阶段另行调用 LLM 重新评分。

## 6. SEARCH 三个实验预算

本次 `search-limit=3` 已全部结算：

| 实验 | 假设 | 状态 | SEARCH AUROC | 结论 |
|---|---|---:|---:|---|
| 1 | 使用原生缺失值的 HistGradientBoosting | FAILED | 无可信分数 | `settled without a trusted result`，假设记为 INCONCLUSIVE |
| 2 | 为重缺失实验室变量增加逐列缺失指示 | SUCCEEDED | `0.7502349510236114` | 低于基线，假设被反驳 |
| 3 | 对树模型增加 Platt/Isotonic 校准 | SUCCEEDED | `0.7516216464980187` | AUROC 与基线持平；Platt 的 Brier 略有改善 |

按冻结主指标 `roc_auc/maximize`，没有候选严格超过基线，因此 `sota_id` 保持为 `exp_baseline`。这里的“基线最优”仅指本次 SEARCH 验证比较，不代表最终测试分数等于 SEARCH 基线，也不代表相对旧运行获得了确定性能提升。

## 7. VALIDATE 与最终闭环

SEARCH 三个预算结算后，流程正常进入 VALIDATE，并最终输出：

- SEARCH 参考分数：`0.7516216464980187`
- final evaluator AUROC：`0.7641140166451901`
- final AUPRC：`0.7419588808109052`
- final Brier：`0.19833761474331962`
- generalization gap：`-0.012492370147171417`
- generalization warning：`false`
- 最终预测行数：1,821
- 重复 ID：0
- 缺失 ID：0
- 额外 ID：0
- 非法/越界分数：0

日志终点为：

```text
[supervisor/text] VALIDATE completed · final test score 0.7641 · generalization gap -0.0125
[state] phase=COMPLETED status=COMPLETED
TERMINAL: phase=COMPLETED status=COMPLETED
```

后台启动脚本写入的退出码为 `0`，结束时间为北京时间 `2026-08-28T15:18:29.7532998+08:00`。

## 8. 与此前 SUPPORT2 结果的口径区别

此前运行和本次运行不能直接作性能增幅比较：

| 运行 | 基线模型 | SEARCH 切分 | SEARCH 基线 AUROC | final AUROC |
|---|---|---|---:|---:|
| 此前 search-limit=3 运行 | LightGBM | 6,373 train / 1,366 validation / 1,366 test | `0.7433582988980716` | `0.748583849862259` |
| 本次修复后运行 | RandomForest + missing count | 7,284 train / 1,821 validation；另有 1,821 final labels | `0.7516216464980187` | `0.7641140166451901` |

两次运行不仅模型不同，评估集合大小和具体 ID 也不同。因此准确表述是：

> 本次独立运行的 final evaluator AUROC 为 0.7641；该数字是本次流程结果，不是从旧基线 0.7434 直接提升到 0.7641 的对照证据。

此外，本次 SEARCH 和 final 的 label ID 集合彼此无重叠，但生成的基线脚本在每个阶段使用“排除当前 evaluator IDs 后的其他全部行”训练。若要把 final 分数用于严格科研结论，后续应预先冻结全局 train/validation/test 三分切分，并确保 final 子集在整个模型选择阶段都不参与训练。本次结果主要作为工作流冒烟和闭环证据。

## 9. 本次验证到的修复效果

结合离线测试和本次真实运行，可确认：

1. DeepSeek Pro reasoning 的 Ideator 输出成功进入本地解析、候选检查和 Rubric 批量评分，没有再次出现三路候选全部为空而终止 SEARCH。
2. Layer 2 在完整评分成功时写出稳定的 `rubric_status=success`；本轮无需 fallback。
3. SEARCH 的三个实验预算均被结算；单个实验没有可信结果时不会把未完成 SEARCH 伪装为成功，也不会阻止安全地转向后续候选。
4. VALIDATE 的结构化结果成功通过协议校验，流程到达真正的 `COMPLETED/COMPLETED`。
5. 重定向后的主要 stdout 中文和 Unicode 输出可读，未再次触发 GBK `UnicodeEncodeError`。
6. 最终预测合同通过 exact-ID 检查。

实现阶段已有的离线证据包括：

- focused regression selection：27 passed
- affected-file suite：121 passed（两次）
- adjacent frozen-contract suite：106 passed
- fallback workflow：4 passed
- Black、`compileall`、`git diff --check`：passed

## 10. 已知问题与边界

1. 一个 SEARCH 实验没有形成可信结果，已明确以 FAILED 结算，不能为其补写或推测指标。
2. 部分 EDA worker 耗尽 turn budget；主流程已降级继续，但仍有可优化的 API/时间成本。
3. 后台 PowerShell 的 stderr 留有 `#< CLIXML` 和被错误编码的 progress record；它没有触发 Python Unicode 崩溃，也未影响 stdout、状态文件或最终结果，但仍是低优先级日志整洁问题。
4. 本次最终分数不构成跨运行的严格性能提升证明；需要冻结同一模型、同一标签 ID 和同一切分才能进行这种比较。
5. 本报告验证的是当前 Rubric V2 分支在该真实任务上的工作流行为，不宣称仓库全部功能或所有模型供应商均已验证。

## 11. 证据位置

本次运行项目目录：

`C:\Users\15055\Documents\AthenaRuns\support2-180d-search3-hardened-20260828-143411`

建议团队复核：

- `.athena/state.json`：最终阶段、状态和 VALIDATE 指标
- `.athena/research_tree.json`：12 个候选、Rubric 评分、三个 SEARCH 实验和 SOTA 指针
- `.athena/logs/agents/`：Ideator、Rubric、实验和 VALIDATE 原始日志
- `console.out.log`：完整 headless stdout
- `console.err.log`：后台 PowerShell stderr/CLIXML 记录
- `exit-code.txt`、`finished-at.txt`：进程退出码和结束时间
- `workspaces/eda/REPORT.md`：本次基线报告
- `workspaces/athena-f15e686a5dd046f0a8f8a58ac657238e/REPORT.md`：最终测试报告

运行目录包含完整数据路径和模型交互证据，适合团队内部复核；不要提交 `.env`、API key、运行目录或原始数据到 GitHub。

## 12. 最终结论

本次全量 SUPPORT2 真实运行证明，修复后的任务理解、评价策略冻结、多 Ideator 候选生成、Layer 2 批量 Rubric、确定性 Selector、三次 SEARCH 预算结算、实验失败隔离、预测合同检查和最终 VALIDATE 可以共同完成真实闭环。

最终状态为 `COMPLETED/COMPLETED`，final evaluator AUROC 为 `0.7641`。该分数应作为本次运行的结果和流程成功证据，不应被包装为相对旧运行的严格性能增幅。
