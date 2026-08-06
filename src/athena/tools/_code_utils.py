"""共享的 LLM 响应解析工具。

用于从自由文本 LLM 响应中提取代码块。
供 data_prepare 等工具使用。
"""

import re


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
