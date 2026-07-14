"""待实现：Athena 的唯一工作流控制平面。

Scheduler 负责推进 Session 的 Prepare、Search、Validate 与 Report 阶段，调度
Agent、工具和 SandboxRuntime 任务，并统一管理预算、重试、并发、优先级及
Human-in-the-Loop。它通过 ThreadManager 事件流观察执行，而不承担线程历史或
重复实现 checkpoint；所有状态事实写入 StateStore。
"""
