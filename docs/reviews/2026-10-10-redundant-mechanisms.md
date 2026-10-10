# 冗余机制阅读记录

日期：2026-10-10。两位 subagent 分别阅读目录/调用路径与运行时/准备阶段代码。
本次只整理目录和记录发现，不实施以下机制合并。这里的重复是待评估项，
不能直接解释为可删除代码；涉及已有未提交改动的发现以当时工作区为准。

## GUI runtime factory 的兼容性检查

- [`gui_gateway/__main__.py`](../../src/gui_gateway/__main__.py)：
  `_factory_accepts` 使用 `inspect.signature` 检查指定参数或 `**kwargs`，
  调用处据此决定是否传递 `controller_factory`。
- [`gui_gateway/handler.py`](../../src/gui_gateway/handler.py)：
  `_build_runtime` 再次检查 signature、具名参数与 `**kwargs`，决定是否传递
  `skip_validate`；工作区初始化与重建路径调用此函数。

重复点是 factory 的参数能力探测。可考虑共享一个小型探测函数，但先明确
签名不可读取时的回退语义：两处处理目的和回退行为不同。验证至少覆盖具名参数、
`**kwargs`、不可内省 callable，以及控制器注入和 skip_validate 的实际传递。

## 本地执行路径的命令输入校验

[`execution/runtime.py`](../../src/athena/execution/runtime.py) 中
`ExecutionRuntime.run` 与 `CommandExecutor.run` 均调用 `_validate_command_input`。
普通路径是 `ExecutionRuntime.run` →
[`backend.py`](../../src/athena/execution/backend.py) 的 `LocalBackend.run` →
`CommandExecutor.run`，会对同一命令进行两次检查。

这是调用链上的重复防御，但两个方法可能各自作为调用边界。上层检查还保护
SSH/自定义 backend，不能只因本地路径重复就去掉它。后续需检查直接 backend
调用者，测试空命令、错误命令类型、SSH 和自定义后端，再决定是否统一校验归属。
本次没有性能测量，也未认定这会造成可感知开销。

## PREPARE 的 agent 注册样板

- [`prepare/eda.py`](../../src/athena/research/prepare/eda.py)：`_register_eda_agent`。
- [`prepare/baseline.py`](../../src/athena/research/prepare/baseline.py)：
  `_register_prepare_agent` 与 `_register_ideator`。

这些函数重复 `registry.contains` → 已有则返回 → 传递 provider、artifacts、
workspace、execution 和 tools 的注册流程。可评估抽取共有参数构造，但不同
agent 类型和工具集（如 kaggle_tools、baseline_ideator_tools）仍有实际语义。
[`prepare/evaluator.py`](../../src/athena/research/prepare/evaluator.py) 存在主动
注销再注册以重绑 workspace 的路径；不能把所有注册都改成全局“已存在就跳过”。
合并前应验证首次注册、重复注册、workspace 变化和 evaluator 重绑定。

## 阅读范围与保留决定

代码阅读覆盖上述文件及 `execution/backend.py`、`execution/remote/ssh.py` 的
符号/调用查询，`prepare/orchestrator.py`、`research/runtime/bootstrap.py`、
`research/runtime/facade.py`、`scripts/gui_gateway_entry.py` 和 `athena/cli.py`。
目录审查同时阅读 Python/npm/Cargo 入口与 clarification 跨语言 fixtures 引用。
这不是全仓逐文件审查，也不关闭 CURRENT 中尚未完成的模块简化计划。

`codex_docs/CURRENT.md` 是规定的工作入口，`docs/codex_docs/` 存放计划和报告；
两者有不同职责，本次保留。各语言的测试目录同样不因名称相似而合并。
