# dsh_docs — 实施计划索引

Status: active
Owner: Athena maintainers
Last verified: 2026-08-11

从「流程优化 + 更有效算法」brainstorm 转出的实施计划索引。每份计划独立、可单独实施；正确性批次优先于有效性批次。

| 计划 | 批次 | 范围 |
|---|---|---|
| [search-selection-upgrade-plan.md](search-selection-upgrade-plan.md) | P0 + P1(rank) | 选择/比较正确性 + rank 有效性（rank_pending 方向、math.isclose tie、多准则 rubric prior、family BT strength） |
| [graph-completion-ablation-plan.md](graph-completion-ablation-plan.md) | P1(图补全) | `depends_on` 边 + 消融（依赖闭包 leave-one-out + Shapley 归因） |
| [mcts-tree-search-plan.md](mcts-tree-search-plan.md) | P2(结构性) | MCTS/UCB1 树搜索 + 流水线式 ideation |
| [statistical-rigor-plan.md](statistical-rigor-plan.md) | P2(严谨性) | paired bootstrap + 显著性 + `ComparisonVerdict.p_value` |
| [athena-preset-tools-plan.md](athena-preset-tools-plan.md) | P3(预设侧) | DSH 预设的树预览/rank 真工具 + 研究快照 |
| [session-isolation-plan.md](session-isolation-plan.md) | GUI 后端 bugfix | 同一目录下多对话隔离（每对话独立 runtime + 事件/暂停按会话路由） |

研究向批次（DPP 多样性选择、语义 novelty、图 embedding）暂缓，待上表稳定后再立项。

## 通用约定

- 每份计划遵循同一结构：目标/成功标准 → 现状证据(file:line) → 任务(改动+测试+验收) → 依赖顺序 → 验证命令 → 验收 checkbox → 决策点/风险 → 规范引用。
- 正确性优先于有效性；任何升级不得破坏「无 model / 无历史数据 / 无评估证据」时的确定性回退，绝不伪造。
- 规范 owner 不变：`athena.core.research_models` 拥有领域模型，`athena.research.supervisor` 拥有编排，`athena.gui.graph` 拥有图算法，`athena.research.evaluation` 拥有可信评估。
