# AutoResearch 设计文档

日期：2026-08-15
状态：design

AutoResearch 是 Athena 之上的端到端研究自动化框架：
`Idea Generation → Experiment（Athena 子集）→ Paper Writing → Paper Refinement/Quality Gate → Artifact Packaging`。

## 当前执行基线（2026-08-20）

当前实现应从
[ML-first 精简控制平面设计](2026-08-20-autoresearch-ml-control-plane-design.md)
出发：复用 DSH 和现有 handoff，以一个主要 `AutoResearchService` 跑通真实 ML
纵切；旧版 RunSpec/PipelineRunner/ProviderRegistry 等通用框架延期到第二领域接入
后的 M4。领域规则仍以本目录对应文档为准。

## 文档

| 文档 | 内容 |
|---|---|
| [2026-08-15-autoresearch-ts-plugin-design.md](2026-08-15-autoresearch-ts-plugin-design.md) | AutoResearch 框架与 TS 插件设计：独立 `@athena/autoresearch` 包、阶段状态机、论文生成三路径、预算与质量闸 |
| [2026-08-15-autoresearch-generalization-and-minimalism.md](2026-08-15-autoresearch-generalization-and-minimalism.md) | M4 泛化参考：RunSpec + PipelineRunner + StageContext + Provider 可插拔；不作为 ML v1 实现基线 |
| [2026-08-15-autoresearch-detailed-design.md](2026-08-15-autoresearch-detailed-design.md) | 历史实现级设计：阶段语义与验收保留，代码结构由 2026-08-20 精简设计覆盖 |
| [2026-08-15-autoresearch-protocols-and-paper-engine.md](2026-08-15-autoresearch-protocols-and-paper-engine.md) | 阶段协议与 Paper Engine 详细设计：Athena 适配接口、入池协议、TemplateKit/Overleaf/LatexBuilder/Composer/Reviewer/Packaging 契约、事件 payload、错误码、BDD 场景 |
| [2026-08-15-autoresearch-figures-and-experiment-design.md](2026-08-15-autoresearch-figures-and-experiment-design.md) | 论文图与可信实验/消融设计：LLM 生成 SVG 架构图、SVG 渲染管线、EDA 图不进入论文、多数据集可信实验、消融矩阵、BDD 增量 |
| [2026-08-15-implementation-draft.md](2026-08-15-implementation-draft.md) | 已取代的实现草稿：只供查阅早期算法，不得直接作为 ML v1 实施计划 |
| [2026-08-15-api-generalization-design.md](2026-08-15-api-generalization-design.md) | M4 API 参考：第二领域接入后再验证 RunSpec/Stage/Provider/Gate 等抽象 |
| [2026-08-15-hypothesis-local-pool-design.md](2026-08-15-hypothesis-local-pool-design.md) | Hypothesis 本地池设计：`HypothesisPool` 服务、池状态机、与 `ResearchTree` 对账、论文回写 |
| [2026-08-16-idea-generation-design.md](2026-08-16-idea-generation-design.md) | 独立 idea generation stage：双模 brainstorm、TS 门禁（照搬 Python light pipeline 阈值）、proposal+hypothesis 双产出 |
| [2026-08-16-records-to-paper-design.md](2026-08-16-records-to-paper-design.md) | 已有实验记录→论文 全自动工作流设计（v3）：程序只做溯源，其余 Handoff 文档化；结合 Spark-to-Paper/ARIS 审计链 |
| [2026-08-16-candidate-to-paper-design.md](2026-08-16-candidate-to-paper-design.md) | candidate→paper 全流程：10 阶段 RunSpec preset、四段 ID 强追溯、实验阶段引擎解析与半自动模式 |
| [2026-08-20-autoresearch-ml-control-plane-design.md](2026-08-20-autoresearch-ml-control-plane-design.md) | ML-first 精简控制平面：复用 DSH 与现有 handoff，以最少 TS 代码补足状态恢复、动态 rubric、自主迭代和后续领域泛化边界 |
| [records-paper-handoff/00-RUNBOOK.md](records-paper-handoff/00-RUNBOOK.md) | 总控 RUNBOOK：阶段顺序、每阶段 gate、脚本调用点、人工闸、终止恢复与 FAILURE_REPORT |
| [records-paper-handoff/01-intake-and-evidence.md](records-paper-handoff/01-intake-and-evidence.md) | 通用证据链抽取（adapter=athena、data-aware mode）、digest 骨架与补全 |
| [records-paper-handoff/02-claims-and-outline.md](records-paper-handoff/02-claims-and-outline.md) | 判级 rubric、result-to-claim、Claim Admission 五标签、贡献聚合、Related Work 脚手架、outline、验收契约 |
| [records-paper-handoff/03-writing.md](records-paper-handoff/03-writing.md) | 写作纪律、role-aware 论文图、证据标签、真实 bib 链、abstract 最后修订、五遍质量 pass |
| [records-paper-handoff/04-review-and-packaging.md](records-paper-handoff/04-review-and-packaging.md) | verify-trace、Manuscript Gate、三个零上下文审查、bounded recovery、打包 |
| [records-paper-handoff/05-data-contracts.md](records-paper-handoff/05-data-contracts.md) | 程序化数据契约：evidence_chain.json（data-aware）、trace_audit.json、标签语法、脚本 CLI |
| [candidate-to-paper-handoff/00-RUNBOOK.md](candidate-to-paper-handoff/00-RUNBOOK.md) | candidate→paper 总控：阶段顺序、ID 链、人工闸、预算、失败报告 |
| [candidate-to-paper-handoff/01-candidate-intake.md](candidate-to-paper-handoff/01-candidate-intake.md) | candidate.md → candidate.json + candidate_id |
| [candidate-to-paper-handoff/02-brainstorm.md](candidate-to-paper-handoff/02-brainstorm.md) | 双模 brainstorm，产出 BRAINSTORM.md |
| [candidate-to-paper-handoff/03-ideation.md](candidate-to-paper-handoff/03-ideation.md) | 方向→结构化 hypothesis + TS 门禁 + 入树/池 |
| [candidate-to-paper-handoff/04-experiment-plan.md](candidate-to-paper-handoff/04-experiment-plan.md) | 实验预注册：实验设计、结果表骨架、evidence map |
| [candidate-to-paper-handoff/05-experiment.md](candidate-to-paper-handoff/05-experiment.md) | 引擎解析（auto/athena/external/none）与半自动三停点 |
| [candidate-to-paper-handoff/06-evidence.md](candidate-to-paper-handoff/06-evidence.md) | experiment → evidence_chain.json（转 records-paper 01） |
| [candidate-to-paper-handoff/07-claims-outline.md](candidate-to-paper-handoff/07-claims-outline.md) | 判级/五标签/贡献/outline/契约（转 records-paper 02） |
| [candidate-to-paper-handoff/08-writing.md](candidate-to-paper-handoff/08-writing.md) | 论文写作方向详细设计：混合 writer、Claims-Evidence Matrix 契约、逐节 prompt、图管线、bib worker、质量 pass |
| [candidate-to-paper-handoff/09-review.md](candidate-to-paper-handoff/09-review.md) | verify-trace + 三审查 + 有界修订（转 records-paper 04） |
| [candidate-to-paper-handoff/10-packaging.md](candidate-to-paper-handoff/10-packaging.md) | 终检打包 + FINAL_REPORT / FAILURE_REPORT |

## 示例资产

| 路径 | 内容 |
|---|---|
| [../../examples/articles_tests/2026-08-15-autoresearch-figure-prompt-examples.md](../../examples/articles_tests/2026-08-15-autoresearch-figure-prompt-examples.md) | FigureDesigner 论文架构图 prompt 文本样例（4 个完整示例 + 通用模板） |

## 最小化验证

| 路径 | 内容 |
|---|---|
| [verify_exp/README.md](verify_exp/README.md) | 最小化验证实验目录：规则与子实验索引 |
| [verify_exp/figure-reconstruction/README.md](verify_exp/figure-reconstruction/README.md) | 图重建三后端最小验证：drawio MCP / HTML-SVG / native-svg 实验矩阵与通过标准 |

## 已确认的关键决策

1. **包形态**：独立 `@athena/autoresearch` 包 + 独立 `autoresearchPlugin`。
2. **论文交付物**：完整 LaTeX/PDF；ICLR / ICML 等 ML 顶会模板可选；始终生成 Markdown draft。
3. **Overleaf**：走官方 API。
4. **模板获取**：每次 AutoResearch 启动用 `curl` 从会议官网拉取模板；失败回退上次缓存。
5. **成本预算**：首版只做 token 预算（`max_tokens`），不接计费（不做 `max_cost`）。
6. **LaTeX 编译失败自修复**：不做轮次限制，只受 `project_time_limit` 限制；每次尝试返回控制台/编译器输出。
7. **阴性结果**：保留并写入 limitations / negative results。
8. **运行基础设施**：复用 DSH，不实现第二套 Agent Runtime、EventBus、DAG 或任务队列。
9. **控制平面**：ML v1 只注册一个主要 `AutoResearchService`，使用精简 state + events 记录。
10. **质量闭环**：实验前动态生成、独立审查并冻结任务专用 rubric。
11. **自主迭代**：evidence 后由 Supervisor 在预算内决定继续、转向、请求用户或结束；计划修订只影响未来。
12. **实验引擎**：暂时使用 Python Athena，Core 不依赖其私有状态类型。
13. **通用化时机**：ML v1 完成后，以第二个真实领域验证最小 Domain Profile，不预建空壳接口。
