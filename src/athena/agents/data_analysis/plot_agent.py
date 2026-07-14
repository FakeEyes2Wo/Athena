"""待实现：独立 AthenaThread 中运行的 EDA 绘图子 Agent。

PlotAgent 读取文字 EDA 草稿中的图规格，为每节生成回答指定问题的图，并回填图片
引用和观察结论到 ``data_analyze/EDA.md/EDA.md``。绘图请求应显式声明是否等待：
等待时结果回流 DataAnalysis，非等待时仅用于报告展示；图形美观规范与具体工具
选择尚待设计。
"""
