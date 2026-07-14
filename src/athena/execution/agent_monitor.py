"""待实现：Qoder/Codex 等长运行 Agent 的进度监控。

监控器应订阅 ThreadManager 的事件与工具调用状态，识别无进度、等待循环或资源
耗尽等可能死锁，并将诊断、超时和建议中断动作写为 artifact。它不应自行修改
Agent 代码或工作流状态；是否重试、终止或请求人工介入由 Scheduler 决定。
"""
