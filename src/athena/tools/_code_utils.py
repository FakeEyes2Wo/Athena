"""共享的 LLM 响应解析工具。

用于从自由文本 LLM 响应中提取代码块和结构化文件内容。
供 data_prepare、baseline_builder 等工具使用。
"""

import re

# project_code_gen 必须产出的 5 个文件
_REQUIRED_CODE_FILES = {"model.py", "dataset.py", "train.py", "infer.py", "config.yaml"}


def extract_code_block(text: str) -> str | None:
    """从 LLM 文本响应中提取第一个 Python 代码块。

    优先匹配 ```python ... ```，回退到任意 ``` ... ```。
    返回代码内容字符串，无匹配时返回 None。
    """
    # 优先：python 语言标记的代码块
    m = re.search(r"```python\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # 回退：任意代码块
    m = re.search(r"```\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return None


def parse_code_files(text: str) -> dict[str, str]:
    """从 LLM 文本响应中解析多文件项目代码。

    支持的格式：
    - ``### filename.py`` 标题后跟代码块
    - ``## filename.py`` 标题后跟代码块

    必须包含全部 5 个文件：model.py, dataset.py, train.py, infer.py, config.yaml。
    缺任何文件抛出 ValueError。

    Returns:
        {filename: code_content} 字典。
    """
    files: dict[str, str] = {}

    # 按 Markdown 标题分割：### filename.ext 或 ## filename.ext
    parts = re.split(r"\n(?=##?#?\s+\S+\.(?:py|yaml|yml)\s*\n)", text)

    for part in parts:
        m = re.match(r"##?#?\s+(\S+\.(?:py|yaml|yml))\s*\n", part)
        if not m:
            continue
        fname = m.group(1)
        body = part[m.end():]
        code = extract_code_block(body)
        files[fname] = code if code else body.strip()

    missing = _REQUIRED_CODE_FILES - set(files.keys())
    if missing:
        raise ValueError(
            f"Missing required files: {', '.join(sorted(missing))}"
        )
    return files
