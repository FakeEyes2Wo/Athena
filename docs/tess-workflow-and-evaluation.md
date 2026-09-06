# TESS 数据划分与评估口径

本文档把 Athena 的通用研究流程映射到 TESS 二分类任务，作为数据隔离、复现和
结果解读的统一口径。数据集本身是外部输入，不随 Athena 仓库提交。

## 1. 阶段与数据可见性

TESS 数据按阶段拆为三个独立 split。它们不应拼接，标签也不应跨 split 传播：

| split | Athena 阶段 | 用途 | 可见性 |
|---|---|---|---|
| `train.csv` | PREPARE、SEARCH | 唯一带标签的数据。用于 EDA、基线训练、特征工程和训练内 OOF 阈值选择；SEARCH 候选沿用训练得到的拟合规则。 | Agent 和训练代码可见。 |
| `search_features.csv` | SEARCH | 无标签特征，用于候选实验的统一推理和比较。公开标签只在独立复核时使用。 | 可见特征，不可见标签。 |
| `final_features.csv` | FINAL | SEARCH 完成、SOTA 与配置冻结后，提交给独立 FINAL evaluator 进行终评。 | SEARCH/训练 Agent 不可见。 |

完整顺序是：

```text
PREPARE(train) → SEARCH(train 拟合 + search 推理) → 冻结配置 → FINAL(final 推理/评估)
```

`final_features.csv` 不参与 EDA 决策、模型选择、特征选择或阈值调参。断点恢复只
恢复 Athena 的状态和产物，不改变这一隔离边界。

## 2. 训练与特征工程约束

- 样本按 `(TIC, sector, w_index)` 排序，并在 TIC 内构造时序特征。
- 中位数、IQR 等统计量在每个训练折的拟合部分计算，再应用到验证折和无标签 split；
  不使用验证、SEARCH 或 FINAL 标签反向计算统计量。
- 分类阈值只使用训练集内按 TIC 分组的 5 折 OOF，在 `[0.40, 0.60]`、步长 `0.01`
  中选择；当前冻结阈值为 `0.41`。
- 推理入口只读取特征文件和已冻结配置，不读取标签文件；提交结果按
  `image_filename` 对齐，内部 Athena 轨迹可使用 `__athena_row_id`。

## 3. 指标定义

混淆矩阵中的 `TP/FP/TN/FN` 分别是真阳性、假阳性、真阴性和假阴性。主指标为
**Macro F1**：先分别计算正类和负类 F1，再取平均，避免类别不平衡时只看正类。

| 指标 | 定义 | 解释 |
|---|---|---|
| Precision | `TP / (TP + FP)` | 告警样本中真正为 flare 的比例。 |
| Recall / POD | `TP / (TP + FN)` | 真实 flare 被捕获的比例。 |
| F1 | `2·Precision·Recall / (Precision + Recall)` | 正类 Precision 与 Recall 的调和平均。 |
| Macro F1 | `(F1_positive + F1_negative) / 2` | 两类等权平均，作为主排名指标。 |
| FAR | `FP / (TP + FP)` | 告警中的误报比例，越低越好。 |
| Accuracy | `(TP + TN) / N` | 整体准确率，需结合 Macro F1 解读。 |
| TSS | `TPR − FPR` | `Recall − FP/(FP+TN)`；随机水平约为 0，越高越好。 |
| HSS | `2(TP·TN − FP·FN) / [(TP+FN)(FN+TN) + (TP+FP)(FP+TN)]` | 相对随机预报的技巧分数，1 为完美、0 约为随机。 |
| ROC-AUC | ROC 曲线下面积 | 使用连续概率衡量整体排序能力。 |
| PR-AUC | Precision–Recall 曲线下面积 | 对少数类更敏感，同样需要连续概率。 |

SEARCH 和 FINAL 的主排名均按 Macro F1；其余指标用于诊断漏报、误报、类别不平衡
和概率排序质量。离散 `prediction` 可复算混淆矩阵与阈值指标；若预测文件中的
`probability` 只是历史审计占位值，则不得据此宣称 ROC-AUC 或 PR-AUC。

## 4. 相关入口

- GUI 工作流与工作区隔离：根目录 [README](../README.md)。
- EDA 产物与 `EDA_HANDOFF.md`：[`athena-guide/12-eda-system.md`](athena-guide/12-eda-system.md)。
- Evaluator 的 ID 对齐契约：[`evaluator_contract_ch.md`](evaluator_contract_ch.md)。
- 离线 TESS 复现材料：[`../reproduction/tess/README.md`](../reproduction/tess/README.md)。
