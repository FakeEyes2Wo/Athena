"""将原始比赛描述文本解析为结构化的 TaskMetaData。

通过 LLM 调用从非结构化比赛文本中提取任务类型、数据模态、目标变量、
主要指标和约束条件。
"""

import json

from athena.core.schemas import MetricSpec, TaskMetaData
from athena.utils.single_turn_chat import single_turn_chat

# 系统提示词：让 LLM 从原始比赛描述中提取结构化字段
_PARSE_SYSTEM_PROMPT = """\
You are a competition metadata parser. Given a raw competition description,
extract structured metadata as JSON.

Output a JSON object with these fields:
- task_type: one of "binary_classification", "multi_class_classification",
  "regression", "clustering", "time_series_forecasting", "object_detection",
  "image_segmentation", "image_generation", "style_transfer", "nlp",
  "recommendation", "other"
- data_type: one of "tabular", "text", "image", "time_series", "video",
  "audio", "multi_modal", "other"
- target_vars: list of target variable / label column names (strings)
- primary_metric_name: the main evaluation metric name (e.g. "accuracy",
  "f1", "rmse", "log_loss")
- primary_metric_direction: "maximize" or "minimize"
- constraints: list of constraint strings

If a field cannot be determined, use: task_type="other", data_type="other",
primary_metric_name="unknown", primary_metric_direction="maximize".
"""


def parse_competition_info(raw_text: str, source_url: str) -> TaskMetaData:
    """通过 LLM 将比赛描述文本解析为 TaskMetaData。

    Args:
        raw_text: 抓取的比赛描述或 markdown 内容。
        source_url: 比赛 URL，用于溯源。

    Returns:
        包含 task_type, data_type, target_vars, primary_metric, constraints 的 TaskMetaData。
    """
    user_prompt = (
        f"Competition URL: {source_url}\n\n"
        f"Competition description:\n{raw_text}"
    )
    result = single_turn_chat(
        system_prompt=_PARSE_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        response_format={"type": "json_object"},
    )
    data = json.loads(result)

    return TaskMetaData(
        task_type=data.get("task_type", "other"),
        data_type=data.get("data_type", "other"),
        target_vars=data.get("target_vars", []),
        primary_metric=MetricSpec(
            name=data.get("primary_metric_name", "unknown"),
            direction=data.get("primary_metric_direction", "maximize"),
        ),
        constraints=data.get("constraints", []),
    )
