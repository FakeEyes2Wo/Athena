"""待实现：限制实验修改边界的策略平面。

Supervisor 审查 CodeAgent 与修复流程提出的 Git diff，确保变更服务于当前实验假设、
未越过任务约束，且不破坏冻结 evaluator、数据切分或最终未触碰测试的完整性。批准、
拒绝、风险和质量判断必须使用版本化 rubric 并输出逐项证据；其结论交由 Scheduler
执行，不直接写入实验 worktree。
"""
