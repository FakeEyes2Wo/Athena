# Idea Generation Stage 设计（独立可插拔 + 双模 Brainstorm）

日期：2026-08-16
状态：design
上游：`docs/idea-generation.md`（Python light pipeline 门禁）、`2026-08-15-autoresearch-generalization-and-minimalism.md`（RunSpec/Stage）、brainstorming skill（交互式发散-收敛）

---

## 0. 2026-08-20 自主循环集成

本 stage 的双模 brainstorm、候选门禁、重提上限和产物契约继续有效。ML v1 中：

- candidate intake 后先生成并冻结任务专用 `RUBRIC.md`；
- ideation 和 `EXPERIMENT_PLAN.md` 必须引用 rubric 中的主要指标、证据要求和停止条件；
- evidence 产生后，Supervisor 可依据 rubric 决定进入论文、继续实验或回到本 stage
  转向；
- 回到本 stage 时生成新的 `PLAN-vN.md` 和新 hypothesis，旧候选、拒绝理由和实验
  记录不覆盖；
- `{ id: "ideation", provider: "ideation" }` 只表示流程能力，不要求实现通用
  ProviderRegistry 或 PipelineRunner。

## 1. 定位

- 新增一个独立 stage：`{ id: "ideation", provider: "ideation" }`。
- `provider` 只是现有框架的 stage 注册名，**不新增 Provider 类/接口**；实现就是一个普通 stage 模块。
- 可单独跑，也可用 RunSpec 插到 `experiment` 前、或 `records→paper` 后形成下一轮研究闭环。

## 2. 文件（4 个）

```text
athena-autoresearch/src/
  stages/ideation.ts          # 编排：brainstorm → 提案 → 门禁 → 重提 → 落盘/入池
  ideation/gate.ts            # 门禁纯函数 + 两视角审阅调用（阈值照搬 Python 生产值）
  ideation/schemas.ts         # 一个 zod schema：候选 + 门禁报告
  prompts/ideation.md         # 全部 prompt：ideator / falsifiability / 两视角 / brainstorm 双模
```

## 3. 流程

```text
Brainstorm（双模，产 BRAINSTORM.md）
  ├─ 交互：AUTO_PROCEED=false → 一次一问，给 2-3 方向，人选定
  └─ 自治：零输入 → 发散 5-10 方向 → 批评收敛 → 选 1-3
        ↓
每个方向：Ideator 一次结构化调用 → 1-5 条候选
        ↓
门禁（同现有 light pipeline 语义，TS 内联实现）：
  structural → falsifiability → pre_gate → methodology/statistics 两视角 → hard_gate
        ↓
PASS/EXPLORATORY：proposal 写 IDEAS/，hypothesis 入 ResearchTree + HypothesisPool
REVISE/REJECT：带 rejection 同线程重提 ≤2 次，耗尽返回空并告警
```

## 4. Brainstorm 段（双模）

| 模式 | 触发 | 行为 | 产物 |
|---|---|---|---|
| 交互式 | `AUTO_PROCEED=false` 或 `stdin interactive` | 按 brainstorming skill：一次一问（目的/约束/成功标准）→ 给 2-3 方向带权衡 → 用户选 → 记录 | `IDEAS/<run_id>/BRAINSTORM.md` |
| 自治式 | `AUTO_PROCEED=true` 或零输入 | 发散：LLM 生成 5-10 个方向 → 每个方向做 novelty/feasibility/evidence 三方批评 → 收敛：选 1-3 | 同上 |

`BRAINSTORM.md` 固定结构：

```markdown
# Brainstorm
## Inputs
## Directions considered
## Selected directions
## Rejected directions + why
```

零输入且文献源不可用时：交互式直接问人；自治式写失败报告返回空，不硬编方向。

## 5. Idea Generation 段

每个选定方向：

1. Ideator 按 `prompts/ideation.md` 的 ideator 段产出 1-5 条结构化候选。
2. TS 门禁逐候选执行（单候选失败不影响其他候选，fail-closed）：
   - `structural_check`（纯函数）：每个 `SUPPORTED_PREMISE` 至少绑定一个证据 ref；`predicted_observations` 与 `disconfirming_observations` 非空。
   - `falsifiability`（单轮结构化）：存在可执行证伪测试、列出不可观测变量；调用失败降级为不可证伪，不静默放行。
   - `pre_gate`：`evidence_traceable` + `falsifiable`。
   - `review_board`：methodology / statistics 两视角反方审阅；审阅失败映射 `failed=true`。
   - `hard_gate`：短路判定，`blocking_factor` 记第一项。
3. 阈值照搬 Python 生产校准值：
   - `MAX_TOLERATED_RISKS = 6`
   - `max_total_risks(N) = 6 * N - 1`
   - `fatal_flaw_found` 为主判据，风险条数只作“明显失控”上限
4. 验证方案：固定 domain `machine_learning`，只保留 `ablation_replication`；无 verifier 不虚构，标 `EXPLORATORY`。
5. verdict：`PASS` 与 `EXPLORATORY` 放行；`REVISE` / `REJECT` 本轮丢弃。

## 6. 输入 / 产出 / 落点

| | 内容 |
|---|---|
| 输入 | 零输入 / 一句话方向 / `evidence_chain.json` / 本地 corpus，四选一；零输入时经 stage 内子进程调用现有 Python 文献链 paper_scout → paper_fetch → paper_markdown → paper_rag 自行补文献证据（子进程失败降级为 web search 或本地 corpus） |
| Proposal 产出 | `IDEAS/<run_id>/<idea_id>.md`：gap、why now、方法草图、所需实验、风险 |
| Hypothesis 产出 | `PASS/EXPLORATORY` → `ResearchTree.register_hypotheses` + `HypothesisPool.upsert(QUEUED)`；`REJECT` → `HypothesisPool.upsert(REJECTED, gate_summary)` |
| 报告 | `IDEAS/<run_id>/IDEA_GENERATION_REPORT.md`：存活率、可实验率、预算消耗、被拒原因汇总 |

## 7. 预算与错误处理

- 预算：读 RunSpec `budgets.max_ideas / max_tokens / project_time_limit`；`tokens_used` 累计；超时置 `WAITING`。
- 文献源不可用：零输入自治式 → 失败报告返回空；有输入方向 → 继续但 proposal 标注 `literature_unavailable`。
- LLM 调用失败：阶段内重试 1 次；仍失败置 `WAITING` 保留现场。
- 门禁全拒：同线程带 rejection 重新提案，`MAX_GATE_RETRIES=2`；耗尽返回空并发布 `idea_generation_exhausted`，绝不静默。

## 8. 测试与验收

### 测试

- `gate.ts` 单测：structural 两条不变量、falsifiability 失败降级、review-board fail-closed、hard-gate 阈值边界（5 vs 6 条风险、fatal_flaw 短路）、verifier 匹配。
- golden 对拍：同一批候选 fixture，TS 门禁与 Python `run_light_pipeline` 的 verdict 一致。
- 集成：fake 文献源下跑通零输入自治；验证 BRAINSTORM.md、proposal 落盘、PASS 入树+池、REJECT 带 gate_summary、重提上限。

### 验收标准

1. 零输入自治跑通并产出 ≥1 个方向。
2. 门禁存活率与可实验率写入报告。
3. `PASS/EXPLORATORY` 候选 100% 入池。
4. 门禁全拒时最多重提 2 次，耗尽返回空且有告警事件。

## 9. 开放项

- 自治 brainstorm 的发散数量（5-10）与收敛数量（1-3）先用常量，是否需要按 `max_ideas` 缩放。
- 两视角审阅在 TS 侧是否复用同一模型、与 writer/executor 不同 model family 的约束是否在首版强制。
- `IDEAS/` 目录是否随 `HypothesisPool` 归档策略一起清理。
