# candidate → paper 全流程链路设计

日期：2026-08-16
状态：design
上游：`2026-08-15-autoresearch-ts-plugin-design.md`、`2026-08-15-autoresearch-generalization-and-minimalism.md`、`2026-08-15-hypothesis-local-pool-design.md`、`2026-08-16-idea-generation-design.md`、`2026-08-16-records-to-paper-design.md`、Spark-to-Paper（arXiv 2608.11924）

---

## 0. 2026-08-20 控制平面集成

本链路继续作为 `ml.candidate-to-paper` 的默认真实入口。原有 10 个 handoff 文件
编号保持不变，AutoResearch 控制平面在两个边界插入新增行为：

```text
candidate_intake
  → task profile + 动态 RUBRIC.md 生成/审查/冻结
  → brainstorm → ideation → experiment_plan → experiment → evidence
  → autonomous decision
      ├─ 继续：生成新 PLAN 版本并回到后续实验
      ├─ 转向：回到 brainstorm/ideation
      ├─ 请求用户：WAITING
      └─ 结束：进入 claims_outline
  → writing → review → packaging
```

现有 RunSpec 只作为阶段顺序的描述，不要求实现通用 PipelineRunner。步骤推进、恢复、
预算和决策由精简 `AutoResearchService` 承担；执行细节仍由本目录 handoff 承担。
完整边界见
[`2026-08-20-autoresearch-ml-control-plane-design.md`](2026-08-20-autoresearch-ml-control-plane-design.md)。

## 1. 目标

把「人类模糊 idea（candidate.md 式）」一路自动跑到「论文 PDF/draft」的完整链路。链路由**一个 RunSpec preset + 一组按模块划分的 handoff 文档**承载；每条链路的产物满足四段 ID 强追溯。

## 2. 阶段总览

| # | stage | 职责 | handoff |
|---|---|---|---|
| 1 | `candidate_intake` | 读人类模糊 idea，生成 `candidate_id` | 00/01 |
| 2 | `brainstorm` | 双模发散-收敛，选定 1–3 方向 | 02 |
| 3 | `ideation` | 每方向生成结构化 hypothesis + TS 门禁 | 03（复用 idea-generation 设计） |
| 4 | `experiment_plan` | 预注册实验设计（表结构先定、数值后填） | 04 |
| 5 | `experiment` | 跑实验 / 恢复 / 外部短路 / 无实验 | 05 |
| 6 | `evidence` | Athena adapter 归一为 `evidence_chain.json` | 06（转 records-paper 01） |
| 7 | `claims_outline` | 判级 + result-to-claim + 贡献 + outline + 契约 | 07（转 records-paper 02） |
| 8 | `writing` | 逐节写 + 图 + bib（详细写作设计） | 08 |
| 9 | `review` | verify-trace + 三审查 + 修订 | 09（转 records-paper 04） |
| 10 | `packaging` | 打包 + FINAL_REPORT / FAILURE_REPORT | 10（转 records-paper 04） |

## 3. RunSpec preset：`candidate-to-paper`

> ML v1 可把下列 preset 当作内部常量或顺序配置，不需要先实现通用 RunSpec schema、
> StageRegistry 或 DAG runner。

```jsonc
{
  "schema": "autoresearch-run-spec/v1",
  "stages": [
    { "id": "candidate_intake", "provider": "candidate_intake" },
    { "id": "brainstorm", "provider": "brainstorm" },
    { "id": "ideation", "provider": "ideation" },
    { "id": "experiment_plan", "provider": "experiment_plan" },
    { "id": "experiment", "provider": "experiment",
      "config": { "engine": "auto", "mode": "auto" } },
    { "id": "evidence", "provider": "evidence" },
    { "id": "claims_outline", "provider": "claims_outline" },
    { "id": "writing", "provider": "writing" },
    { "id": "review", "provider": "review" },
    { "id": "packaging", "provider": "packaging" }
  ],
  "budgets": {
    "max_ideas": 20,
    "max_experiments": 10,
    "max_paper_rounds": 5,
    "max_tokens": 0,
    "project_time_limit": "PT12H"
  },
  "paper_spec": { "template": "plain_latex", "main_language": "latex", "latex_via": "none" }
}
```

- `experiment.config.engine`: `auto | athena | external | none`（见 05 模块）。
- `experiment.config.mode`: `auto | semi-auto`（见 05 模块）。
- `AUTO_PROCEED` 控制 brainstorm 后与 outline 后两个人工闸；semi-auto 另加实验三停点。

## 4. 四段 ID 强追溯链

```text
candidate_id  (candidate_intake 生成，如 cand-3f9a)
   → hypothesis_id  (ResearchTree 生成 hyp_...；HypothesisPool.origin_candidate_id 记上游)
   → experiment_id  (exp_<hypothesis_id>；evidence_chain 行带 candidate_id)
   → paper 标签 E:<experiment_id> / B:<baseline_id>
```

规则：

- 每个 stage 产物必须带上游 ID；06 转 `verify-trace` 后额外校验 evidence 行的 `candidate_id` 存在于 `candidate.json`。
- `HypothesisPool` 增加 `origin_candidate_id` 字段（池扩展，不改 ResearchTree 契约）。

## 5. 失败语义（有界 + 半自动）

- 实验-批判-修订循环上限 **7**；耗尽且核心贡献仍 unsupported → `FAILURE_REPORT.md`，失败轨迹保留。
- `mode: semi-auto` 时循环耗尽先置 `WAITING` 问人：换 idea / 降级 proposal / 停止；`mode: auto` 按预算自动换新 idea。
- 阴性结果一路保留：`REFUTED/INCONCLUSIVE/FAILED` → evidence_chain → claims → limitations。

## 6. 目录布局

```text
<work_dir>/candidate-to-paper/<run_id>/
  candidate.json
  RUBRIC.md
  PLAN-v1.md [PLAN-v2.md ...]
  BRAINSTORM.md
  IDEAS/
  EXPERIMENT_PLAN.md
  research/cycle-*/                # 每轮 hypothesis / experiment / evidence
  research/final-evidence-chain.json
  .athena/                         # Athena 实验产物（tree/state/artifacts）
  records-paper/<run_id>/          # 最终 evidence_chain 兼容副本及 01-04 全部产物
  packaging/
  FINAL_REPORT.md | FAILURE_REPORT.md
```

## 7. 模块文档

见 `candidate-to-paper-handoff/`：

| 文档 | 内容 |
|---|---|
| 00-RUNBOOK.md | 总控：阶段顺序、RunSpec preset、ID 链、预算、恢复 |
| 01-candidate-intake.md | candidate.md → candidate.json + candidate_id |
| 02-brainstorm.md | 双模 brainstorm，产出 BRAINSTORM.md |
| 03-ideation.md | 调用 idea-generation 设计，产出 proposals + hypotheses |
| 04-experiment-plan.md | 预注册实验设计，产出 EXPERIMENT_PLAN.md |
| 05-experiment.md | 引擎解析（auto/athena/external/none）、半自动三停点、出口归一 |
| 06-evidence.md | 每轮 experiment → evidence；研究结束后合并全部 cycle（转 records-paper 01） |
| 07-claims-outline.md | 冻结 rubric + 最终 evidence → 判级/五标签/贡献/outline/契约（转 records-paper 02） |
| 08-writing.md | 论文写作方向详细设计：W0–W8 管线、逐节规范、图/bib/质量 pass |
| 09-review.md | verify-trace + 三审查 + 有界修订（转 records-paper 04） |
| 10-packaging.md | 终检打包 + FINAL_REPORT / FAILURE_REPORT |
