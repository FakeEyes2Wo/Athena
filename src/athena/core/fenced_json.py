"""剥掉模型 JSON 输出外面的 markdown 代码块围栏。

模型在拿不到 ``response_format`` 约束时（带工具的对话里发不了，见
``provider.stream``），习惯把终态 JSON 包进 ```json 围栏。围栏是**格式噪声，
不是内容错误**：内容本身是合法 JSON，只是外面裹了三个反引号。

这一条已经打死过两次真机运行：

* SEARCH —— Ideator 连着三次返回围栏 JSON，重试预算耗尽后抛
  ``structured output invalid after retries``；
* VALIDATE（2026-08-31 01:58）—— ``review_validation_diff`` 拿模型回答直接喂
  ``ValidationDiffReview.model_validate_json``，围栏让它抛
  ``Invalid JSON: expected value at line 1 column 1``。整轮 5 小时的搜索结果
  死在最后一步，而那次回答的内容其实是 ``{"accepted": true, ...}``。

第二次之所以会发生，是因为第一次的修复是 ``core/agent/runtime.py`` 里的一个私有
函数，只覆盖了 agent 那条路径。所以它现在放在这里：**任何拿模型回答去解析 JSON
的地方都该先过一遍。**
"""

from __future__ import annotations

import re

# 非贪婪，跨行；``json`` 语言标记可有可无。
_FENCED_JSON = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def unfence_json(text: str) -> str:
    """返回去掉围栏的文本；没有围栏就原样返回。

    只在文本**以** ```` ``` ```` 开头时才动手。正文里碰巧含代码块的回答不受影响——
    那种情况下第一个字符不是反引号，直接原样返回，解析错误照常暴露出来。
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    match = _FENCED_JSON.search(stripped)
    return match.group(1) if match else text
