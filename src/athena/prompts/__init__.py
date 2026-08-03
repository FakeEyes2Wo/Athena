"""统一的 prompt 加载工具。

将 prompt 文本从 Python 代码中分离到独立文件，便于：
- 非开发人员调优 prompt 内容
- 不同场景切换 prompt 变体
- 减少 Python 文件的长度和复杂度

用法::

    from athena.prompts import load_prompt, load_prompt_json

    prompt = load_prompt("demo/titanic_seeded_prompt.txt")
    metadata = load_prompt_json("demo/titanic_task_metadata.json")
"""

import json
from importlib.resources import files


def load_prompt(relative_path: str) -> str:
    """从 prompts 目录加载纯文本 prompt 文件。

    Args:
        relative_path: 相对于 prompts/ 的路径，例如 "demo/titanic_seeded_prompt.txt"。

    Returns:
        文件的完整文本内容（UTF-8）。
    """
    return (
        files("athena.prompts")
        .joinpath(relative_path)
        .read_text(encoding="utf-8")
    )


def load_prompt_json(relative_path: str) -> dict:
    """从 prompts 目录加载 JSON 格式的 prompt 配置文件。

    Args:
        relative_path: 相对于 prompts/ 的路径，例如 "demo/titanic_task_metadata.json"。

    Returns:
        解析后的 dict。
    """
    return json.loads(
        files("athena.prompts")
        .joinpath(relative_path)
        .read_text(encoding="utf-8")
    )
