"""待实现：Codex 作为代码生成后端的适配器。

该适配器与 Qoder 一样只执行 CodeAgent 在隔离 worktree 中提交的明确任务，并通过
artifact 返回工具事件、patch、审批信息和结果；它不自行推进实验、选择指标或绕过
Supervisor。具体 SDK、认证和工具权限协议尚待设计。
"""
