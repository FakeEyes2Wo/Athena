"""待实现：Qoder Agent SDK 的代码执行适配器。

实现前需完成最小 SDK 验证实验。适配器应把 Qoder 过程事件交给 ThreadManager 与
AgentMonitor，供死锁监测；Human-in-the-Loop 可配置为每个 Agent 任务后审批，Debug
可配置为每次大模型交互前审批。适配器只在隔离 worktree 中执行受控请求，返回日志、
patch 和结构化结果 artifact。
"""
