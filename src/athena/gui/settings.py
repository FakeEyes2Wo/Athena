"""Settings field schema for the GUI settings form.

The get/set logic lives on ``ResearchRuntime.settings`` /
``ResearchRuntime.apply_settings``; this module only declares the field schema
(name/label/type/writability/constraints) the frontend renders.
"""

SETTINGS_FIELDS: list[dict[str, object]] = [
    {"name": "model", "label": "模型", "type": "string", "writable": False},
    {
        "name": "concurrency",
        "label": "并发度",
        "type": "int",
        "writable": True,
        "min": 1,
    },
    {
        "name": "search_limit",
        "label": "搜索上限",
        "type": "int",
        "writable": True,
        "min": 0,
    },
    {
        "name": "ideator_count",
        "label": "Ideator 并行数",
        "type": "int",
        "writable": True,
        "min": 1,
        "max": 8,
    },
    {
        "name": "hypotheses_per_ideator",
        "label": "每个 Ideator 假设数",
        "type": "int",
        "writable": True,
        "min": 1,
        "max": 5,
    },
    {
        "name": "direction",
        "label": "优化方向",
        "type": "enum",
        "writable": True,
        "options": ["maximize", "minimize"],
    },
    {"name": "tolerance", "label": "容忍度", "type": "float", "writable": True, "min": 0},
    {"name": "auto_validate", "label": "自动验证", "type": "bool", "writable": True},
    {"name": "manual_mode", "label": "手动选假设", "type": "bool", "writable": True},
    {"name": "model_connection", "label": "模型连接", "type": "model_connection", "writable": True},
    {"name": "phase", "label": "阶段", "type": "string", "writable": False},
    {"name": "status", "label": "状态", "type": "string", "writable": False},
]

# 可写字段集合（与 ResearchRuntime.apply_settings 的白名单一致）。
WRITABLE_FIELDS = {
    field["name"] for field in SETTINGS_FIELDS if field.get("writable")
}
