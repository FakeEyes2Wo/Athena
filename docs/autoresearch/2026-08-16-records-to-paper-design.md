# 已有实验记录 → 论文：全自动科研工作流设计（v3 极简版）

日期：2026-08-16
状态：design
上游：`docs/autoresearch/2026-08-15-autoresearch-protocols-and-paper-engine.md`、`2026-08-15-autoresearch-generalization-and-minimalism.md`、`2026-08-15-autoresearch-figures-and-experiment-design.md`、`docs/paper_rag_tool_ch.md`、`new_kaggle_test/.athena` 实际记录、ARIS（arXiv 2605.03042）审计链、data-to-paper 反向可追溯、Spark-to-Paper（arXiv 2608.11924）

---

## 2026-08-20 控制平面集成

本链路继续作为 `ml.records-to-paper` 的第二入口，并由精简 `AutoResearchService`
直接执行现有 RUNBOOK，不扩展通用 Stage/Provider/Gate 框架。

进入 CLAIMS 前增加一次任务专用 rubric 生成与独立审查，用于证据判级、claim admission
和写作门禁。因为实验已经完成，该 rubric 必须标记 `post-hoc`：

- 可以判断现有证据支持多强的主张；
- 可以要求补充材料或把结论降级为 exploratory；
- 不得声称它是实验前预注册标准；
- 不得用它删除不利实验或重定义已经观察到的主要指标。

其余 evidence 抽取、handoff 写作、`verify-trace` 和三审查流程保持不变。控制平面
基线见
[`2026-08-20-autoresearch-ml-control-plane-design.md`](2026-08-20-autoresearch-ml-control-plane-design.md)。

## 0. 目标与边界

- **输入**：Athena 实验产物目录（`.athena/state.json`、`.athena/research_tree.json`、`.athena/artifacts`），典型样例 `new_kaggle_test`。
- **输出**：对应论文。始终生成 Markdown draft；LaTeX/PDF 走既有 Paper Engine 路径。
- **语言**：英文。
- **自动化**：全自动默认；`INTAKE` 后、`OUTLINE` 后各一个可选人工 checkpoint。
- **强制可追溯**：论文中每个数字必须能由确定性脚本回溯到实验记录。

## 1. 设计原则：只把“溯源”程序化

- **程序化只做溯源**：这是唯一不能交给 LLM 的部分——数字来自哪、论文有没有引用它、哈希是否对得上。
- **其余全部 Handoff 文档**：怎么写、怎么判断声明强弱、怎么审稿、怎么打包，都写成 Markdown 指令，由 agent 执行。ARIS 已证明这条路可行（“ARIS is a methodology, not a platform”），Athena 也已有 `kaggle_handoff_agent.md` 同类先例。
- **不扩展 `@athena/autoresearch` 的 Stage/Provider/Gate 框架**：不新增 RunSpec、Provider、Gate。流程由 RUNBOOK 驱动，程序只提供两个确定性脚本。
- **与 Spark-to-Paper 同路线**：论文生成作为**可组合的 Handoff/Skill 文档**，每个模块有明确输入/输出、可独立运行、可替换；单篇目标成本控制在 ~$10 API 量级（01/05 零 LLM 成本，02/03/04 为主要 LLM 成本）。
- **与既有 AutoResearch design 对齐**：论文图/消融/编译/证据规则沿用 `2026-08-15-autoresearch-figures-and-experiment-design.md` 与 `2026-08-15-autoresearch-protocols-and-paper-engine.md`，不重复发明。

## 2. 一句话架构

```text
一个脚本 extract：任意来源实验记录 → evidence_chain.json（通用证据链，当前 adapter=athena）
一组 Handoff：  agent 照 RUNBOOK 完成 intake / claims / writing / review
一个脚本 verify：  论文 × evidence_chain.json → trace_audit.json（PASS/WARN/FAIL）
```

## 3. 文档组织

| 文档 | 内容 |
|---|---|
| 本文件 | 设计总览、架构、决策记录 |
| [records-paper-handoff/00-RUNBOOK.md](records-paper-handoff/00-RUNBOOK.md) | 总控：阶段顺序、脚本调用点、人工闸、终止与恢复 |
| [records-paper-handoff/01-intake-and-evidence.md](records-paper-handoff/01-intake-and-evidence.md) | 通用证据链抽取（adapter=athena）、digest 骨架 |
| [records-paper-handoff/02-claims-and-outline.md](records-paper-handoff/02-claims-and-outline.md) | 判级 rubric、result-to-claim、贡献聚合、Related Work 脚手架、outline、验收契约 |
| [records-paper-handoff/03-writing.md](records-paper-handoff/03-writing.md) | 写作纪律、证据标签、真实 bib 链、论文图/消融规则、五遍质量 pass |
| [records-paper-handoff/04-review-and-packaging.md](records-paper-handoff/04-review-and-packaging.md) | verify-trace、三个零上下文审查、修订循环、打包 |
| [records-paper-handoff/05-data-contracts.md](records-paper-handoff/05-data-contracts.md) | 程序化数据契约：evidence_chain.json、trace_audit.json、证据标签语法、脚本 CLI |

## 4. 程序化部分（概要）

### 4.1 脚本一：`extract`

通用 adapter 模式，当前 `--adapter athena`。确定性读取来源记录，产出 `evidence_chain.json`：

- 四张表：`task`、`baselines[]`（内部 + 可选外部论文 baseline）、`experiments[]`（每实验一行，含 `metrics`、`deltas`、`evidence_refs`）、`future[]`（PROPOSED/RUNNING）。
- `provenance.files` 只哈希被读取的源文件（如 `state.json`、`research_tree.json`）；不复制任何 artifact。
- 字段异常 WARN 并置 `null`；源文件缺失 exit 1。

### 4.2 脚本二：`verify-trace`

校验论文中的 `% evidence:` / `<!-- evidence: -->` 标签（`E:<experiment_id>` / `B:<baseline_id>`）：

- 未知标签 → FAIL。
- `SUPPORTED/REFUTED/INCONCLUSIVE/FAILED/SUCCEEDED` 实验未被引用且不在 `trace_exclusions.md` 中 → FAIL。
- 标签同/±1 行内数值与 `metrics.primary.value` 不一致 → WARN。
- 论文生成后 `evidence_chain.json` 的 `provenance.files` 源哈希被改 → FAIL。

产出 `trace_audit.json`：`{verdict: PASS|WARN|FAIL, per_experiment, uncovered, unknown_tags, stale}`。

### 4.3 实现位置

`athena_ts/packages/athena-autoresearch/scripts/records-paper-trace.mjs`，两个子命令 `extract` 与 `verify`。纯确定性 Node 脚本，可单测。

## 5. Handoff 部分（概要）

五份 Handoff 按阶段划分，agent 只需顺序执行 `00 → 01 → 02 → 03 → 04`：

```text
INTAKE        → 01 跑 extract + 写 record_digest
CLAIMS        → 02 判级 rubric + result-to-claim + 贡献聚合 + Related Work 脚手架 + outline + exclusions + 验收契约
WRITING       → 03 逐节写作 + 证据标签 + bib 链 + 质量 pass
REVIEW        → 04 跑 verify-trace + 三个零上下文审查 + 修订循环
PACKAGING     → 04 打包清单 + 最终报告
```

## 6. 关键决策记录

1. **为什么不做 Stage/Provider/Gate 扩展**：v1/v2 的框架扩展引入大量新代码，但流程本身（先理解记录、再写、再审）是 LLM 擅长且需要灵活性的部分。ARIS 证明 Markdown skill/runbook 足以承载；Athena 已有 `kaggle_handoff_agent.md` 先例。程序只保留两处必须确定性的地方：证据抽取与标签校验。
2. **为什么用通用 `evidence_chain.json` 数据契约**：它是 extract 与 verify 之间的唯一接口，也是人类查验论文的入口（data-to-paper 的 backward-traceable）；Athena 只是第一个 adapter，后续 wandb/csv/mlflow 翻译成同一张表。
3. **为什么标签用 `% evidence:` 注释而不是 `\ref`**：不侵入 LaTeX 结构，编译后不可见，但 grep 可查；`verify` 靠 `E:`/`B:` 标签建立论文声明与记录的机械对应。
4. **阴性结果强制**：`REFUTED/INCONCLUSIVE/FAILED` 默认必须被引用或显式排除，防止自动写作只挑好结果。
5. **ARIS 借鉴范围**：只借提示词与纪律（result-to-claim、paper-claim-audit、citation-audit、kill-argument、五遍质量 pass、DBLP bib 链），不借其 skill 层与 CLI。
6. **Spark-to-Paper 借鉴范围**：借“composable skill + 单入口 + 低预算”三件事，以及 paperjury 的 **review → verdict → revise → verify** 循环（落在 04）；不借其“从 idea 自动跑实验”的前半段——我们输入是已有实验记录。
7. **复用 AutoResearch 既有设计**：论文图/消融/编译/模板/阴性结果规则直接沿用 `figures-and-experiment-design.md` 与 `protocols-and-paper-engine.md`；Handoff 是这些规则在“记录→论文”场景下的执行手册。

## 6.5 与既有设计 / Spark-to-Paper 的映射

| 本设计 | AutoResearch design 对应 | Spark-to-Paper / ARIS 对应 |
|---|---|---|
| `evidence_chain.json` | `ExperimentEvidence` / `PaperEvidenceBundle` 的通用化 | `results.facts.json`（grounded quantitative evidence） |
| `result_integrity_mode = "data-aware"` | Data-Aware 写作约束 | Spark-to-Paper Data-Aware Mode |
| 01 `extract --adapter` | `ExperimentEngineProvider` 的 source 侧 | input routing + ts-paper-data |
| 02 claims / outline / admission | `PaperSpec` + Writing 前置 | blueprint.json + Claim Admission Protocol（五标签） |
| 03 writing | `PaperComposer` / `FigureDesigner` / `EvidencePlotter` / `AblationCompiler` | paper-write + role-aware figure 双路径 |
| 04 review | `PaperReviewer` + `compile_ok` 等 gates | paperjury：review→verdict→revise→verify |
| 00 阶段 gate 表 | `GateRef[]` 可插拔 gate list | Spark-to-Paper 确定性 gates（Template/Blueprint/Citation/Manuscript/Figure/Compilation） |
| `verify-trace` | `evidence_traceable` gate 的确定性实现 | data-to-paper backward-traceable + Spark-to-Paper result integrity |
| FAILURE_REPORT | 阴性结果保留原则 | Self-Refutation Loop 有界恢复 |

## 7. 验收场景（`new_kaggle_test`）

1. `extract --adapter athena` 跑通：`experiments` 行数 = 10；baseline `B-exp_baseline` primary=7784.6667；SOTA 行 `E-exp_hyp_26fc90321336` primary=8468.7333、`vs_parent`=63.2；3 个 PROPOSED 假设进 `future[]`。
2. 按 RUNBOOK 走完：出 PDF + draft；`verify-trace` 对已结束实验全部 PASS；RUNNING 实验 `E-exp_hyp_1677547f5770` 在 `trace_exclusions.md` 中。
3. 把论文里 SOTA 改成 8500.0：`verify-trace` 报该 tag `value_match: not_found` → WARN/FAIL。
4. 删掉 limitations 里 REFUTED 实验的标签：`verify-trace` 报 `uncovered` → FAIL。
5. 论文残留 `DATA_NEEDED` 注释 → REVIEW 阶段 Manuscript Gate FAIL。
6. 全部 `CONTRIB-*` 的成员实验降级为 `UNSUPPORTED` → 不强行出论文，产出 `FAILURE_REPORT.md`。

## 8. 实现顺序

1. `05-data-contracts.md` 中的 JSON 契约定稿。
2. `records-paper-trace.mjs extract` + 单测（用 `new_kaggle_test` 夹具）。
3. `records-paper-trace.mjs verify` + 单测（含未知标签、未覆盖、数值就近、哈希过期）。
4. 五份 Handoff 文档评审。
5. `new_kaggle_test` 端到端跑通，按 §7 验收。
