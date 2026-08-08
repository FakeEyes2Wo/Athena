"""兼容层：ideator 实现已移入 ``athena.agents.ideator``，这里只做再导出。

新代码应直接从 ``athena.agents.ideator`` 导入；旧导入点（main.py、search
workflow 与既有测试）经由本模块保持可用。
"""

from athena.agents.ideator import DebateResult, Ideator, IdeatorConfig

__all__ = ["Ideator", "IdeatorConfig", "DebateResult"]
