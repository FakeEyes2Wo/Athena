# 02 — CLAIMS & OUTLINE

目标：把 `evidence_chain.json` 每行实验判级，聚合 2–4 条贡献，出 Related Work 脚手架、大纲、排除清单，并冻结验收契约。

## AutoResearch rubric 输入

由 `ml.records-to-paper` 启动时，同时读取已通过独立 Reviewer 的 `RUBRIC.md`。该
rubric 标记为 `post-hoc`，用于限制 claim 强度和指出缺失证据，不能追溯性地重定义
主要指标、删除不利实验或声称预注册。任务 rubric 比下方默认阈值更严格时，以任务
rubric 为准；更宽松时不得突破核心证据约束。

## 默认判级 rubric（脚本数值 + agent rubric）

| 级别 | 条件 | 处理 |
|---|---|---|
| strong | SOTA；或 SUPPORTED 且 vs_baseline 方向一致且改善 ≥1% | Results 主表 |
| weak | SUPPORTED 但 <1% 或方向不符 | 弱声明 |
| negative | REFUTED/INCONCLUSIVE/FAILED | 必须进 limitations，不可排除 |
| future | RUNNING / future[] PROPOSED | Future work |

## result-to-claim（跨模型一批审）

只给 reviewer：`evidence_chain.json` + report artifact 路径。输出逐 experiment：`claim_supported yes|partial|no`、`suggested_claim_revision`、`confidence`。外部 paper baseline：正 Δ 且同设定才可 `outperform`，否则 `competitive`。

## Claim Admission 五标签

| label | 动作 |
|---|---|
| SUPPORTED | retain，evidence-matched 措辞 |
| PARTIALLY_SUPPORTED | narrow 或补证据 |
| UNSUPPORTED | 弱化/移除/limitations |
| CONTRADICTED | 移除或 limitation |
| NEEDS_CONFIRMATION | 作者确认；不得留在最终稿 |

修订跨节传播，abstract 最后改。

## 产出

`claims.md`（含 `candidate_id` 列，若 evidence 行有）→ 贡献聚合 `CONTRIB-*` → `related_work_scaffold.md` → `outline.md` → `trace_exclusions.md` → `paper_acceptance_contract.md`（总是对抗谈判 3 轮）。
