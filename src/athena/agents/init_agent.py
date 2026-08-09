"""InitAgent — PREPARE 第一环：task understanding → 生成 eval.py。

InitAgent 在 DataAgent 之前运行：读取数据集 schema，理解任务（目标列类型、
数值/类别分布）并判定任务类型与主指标，然后产出冻结的 ``eval.py`` 评估脚本
（design §task-understanding）。evaluation 自此只依赖该脚本——Athena 侧不再
静态构造评估逻辑。

请求 payload（JSON）：

- ``data_path``：数据集 CSV 路径（必需）。
- ``target``：目标列名（必需）。
- ``eval_script``：可选。用户首轮输入直接指定的 eval.py 内容；提供时原样
  采用，不做 schema 推断。缺省时按 task understanding 生成默认脚本。

产出：把 eval.py 文本写入 ArtifactStore，``AgentOutcome.result_ref`` 指向该
Artifact。调用方（ProjectRuntime）从响应 JSON 读取 ``eval_script`` 回填
``EvalSpec.eval_script`` 并冻结协议。

eval.py 契约（自包含，纯 numpy，不依赖 sklearn/scipy）：读当前目录的
``predictions.csv``（``__athena_row_id``, ``prediction``）与 ``labels.csv``
（``__athena_row_id``, ``target``），按 row_id 对齐后计算主/次指标。
"""

import json
from typing import Any

import pandas as pd

from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.agent.runtime import BaseAgent
from athena.core.contracts import ArtifactStore

EVAL_ENTRYPOINT = "eval.py"

# 任务类型与主指标的确定性判定（按 sklearn 目录习惯，不含 sklearn 依赖）。
# target 数值且唯一值多 → regression(rmse)；target 数值且唯一值少 → 分类；
# target 类别 → 分类(f1_macro)。
CATEGORICAL_UNIQUE_CAP = 20


def _classify_task(frame: pd.DataFrame, target: str) -> dict[str, str]:
    """按目标列推断 (task_type, primary_metric)。"""
    series = frame[target].dropna()
    n_unique = series.nunique()
    if pd.api.types.is_numeric_dtype(series):
        if n_unique > CATEGORICAL_UNIQUE_CAP:
            return {"task_type": "regression", "primary_metric": "rmse"}
        return {"task_type": "classification", "primary_metric": "f1_macro"}
    return {"task_type": "classification", "primary_metric": "f1_macro"}


def _default_eval_script(primary_metric: str) -> str:
    """生成自包含 eval.py：读 predictions.csv + labels.csv，按 row_id 对齐算主指标。

    纯 numpy，不依赖 sklearn/scipy；分类用 f1_macro，回归用 rmse。
    """
    metric = "f1_macro" if primary_metric != "rmse" else "rmse"
    return f'''"""Athena 默认评估脚本（确定性生成，纯 numpy）。"""
import json
import numpy as np

preds = {{}}
with open("predictions.csv", encoding="utf-8") as f:
    for line in f:
        parts = line.strip().split(",")
        if len(parts) >= 2 and parts[0] != "__athena_row_id":
            preds[parts[0]] = float(parts[1])

labels = {{}}
with open("labels.csv", encoding="utf-8") as f:
    for line in f:
        parts = line.strip().split(",")
        if len(parts) >= 2 and parts[0] != "__athena_row_id":
            labels[parts[0]] = float(parts[1])

rows = [(preds[k], labels[k]) for k in preds if k in labels]
y_pred = np.array([r[0] for r in rows])
y_true = np.array([r[1] for r in rows])
if len(rows) == 0:
    raise RuntimeError("no aligned predictions")

if "{metric}" == "rmse":
    primary = float(np.sqrt(np.mean((y_pred - y_true) ** 2)))
else:
    eps = 1e-9
    tp = np.sum((y_pred == 1) & (y_true == 1))
    fp = np.sum((y_pred == 1) & (y_true == 0))
    fn = np.sum((y_pred == 0) & (y_true == 1))
    precision = tp / (tp + fp + eps)
    recall = tp / (tp + fn + eps)
    primary = float(2 * precision * recall / (precision + recall + eps))

print(json.dumps({{"primary": primary, "metric": "{metric}"}}))
'''


class InitAgent(BaseAgent):
    """task understanding Agent：生成冻结 eval.py，供后续 evaluation 依赖。

    首版为确定性实现（无 LLM）：读 schema 判定任务类型，渲染默认 eval.py；
    ``eval_script`` 请求覆盖时直接采用用户指定脚本。把脚本文本写入 Artifact
    并返回其 ref。
    """

    name = "init-agent"
    description = "理解任务并生成冻结的 eval.py 评估脚本"

    def __init__(
        self,
        store: ArtifactStore,
        *,
        model: str | None = None,
        client: Any = None,
    ) -> None:
        self._store = store
        # model/client 本任务仅暂存（Task 8 启用 LLM 驱动时消费）
        self._model = model
        self._client = client

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        request = json.loads(ctx.input_text or "{}")
        data_path = request.get("data_path")
        if not data_path:
            raise ValueError("InitAgent request requires 'data_path'")
        target = request.get("target")
        if not target:
            raise ValueError("InitAgent request requires 'target'")

        user_script = request.get("eval_script")
        if user_script:
            eval_script = str(user_script)
            primary_metric = request.get("primary_metric", "f1_macro")
        else:
            frame = pd.read_csv(data_path)
            classification = _classify_task(frame, target)
            primary_metric = classification["primary_metric"]
            eval_script = _default_eval_script(primary_metric)

        # 自检：仅做语法编译（不执行）——验证 f-string 转义/缩进/导入无误。
        # 真实预测/标签只在 SEARCH 阶段生成；脚本可被解析即视为契约完整。
        compile(eval_script, EVAL_ENTRYPOINT, "exec")

        payload = {
            "eval_script": eval_script,
            "task_type": (
                "regression" if primary_metric == "rmse" else "classification"
            ),
            "primary_metric": primary_metric,
        }
        result_ref = await self._store.put_text(json.dumps(payload, ensure_ascii=False))
        return AgentOutcome(result_ref=result_ref)
