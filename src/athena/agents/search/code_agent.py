"""待实现：根据选中假设在隔离 worktree 中提出最小代码变更的 Agent。

CodeAgent 可通过 Qoder 或 Codex 适配器完成代码工作，但每次变更必须关联实验计划、
父 commit 与冻结 evaluator；提交前由 Supervisor 审查 diff。Agent 返回 patch、日志和
结构化结果 artifact，不直接改写主工作区或其他实验分支。
"""
