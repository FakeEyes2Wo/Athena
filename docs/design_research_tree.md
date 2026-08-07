# ResearchTree v2 设计

Status: current
Owner: Athena maintainers
Last verified: 2026-07-31
Source of truth: `src/athena/core/research_tree.py`

ResearchTree v2 分离 Hypothesis 与 Experiment。Experiment 通过 `hypothesis_id` 引用假设，通过 `parent_id` 形成执行谱系，并拥有 GitWorkBranch、状态、EvalResult、ComparisonVerdict、artifacts 与 error。

```text
ResearchTree
├── hypotheses: dict[hypothesis_id, Hypothesis]
├── experiments: dict[experiment_id, Experiment]
├── derived children index
└── sota_id
```

Experiment 不保存自己的 ID，不嵌入 Hypothesis，也不持久化 children。加载时先完整解析临时图，再校验引用、父环、状态与 SOTA 资格，全部成功后才返回新树。保存使用同目录临时文件与原子替换。

生命周期固定为：

```text
PENDING -> RUNNING -> SUCCEEDED
   |           |----> FAILED
   |           `----> CANCELLED
   `----------------> CANCELLED
```

终态不可变。只有 RUNNING 实验可由 `complete_experiment` 写入成功证据；失败必须带非空 error。SOTA 只可指向成功的 baseline 或 search 实验。

验证实验也是普通 v2 Experiment：`plan.kind` 为 `ablation` 或 `final-test`，但不具 SOTA 资格。报告 artifact 附在所选 SOTA 上。
