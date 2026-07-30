"""DEPRECATED — 此模块已被 TaskUnderstandAgent 吸收。

DataAgent 的数据分析、清洗、EDA 能力已由以下工具替代：
- athena.tools.data_prepare.DataAnalyzeTool
- athena.tools.data_prepare.DataCleanCodeGenTool

TaskUnderstandAgent 通过 ToolRegistry 持有这些工具，
不再需要独立的 DataAgent 进程。

如需数据分析能力，请使用上述工具或直接调用
athena.workflows.prepare.data_analysis 中的工作流。
"""
