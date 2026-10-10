# 源码清单最小补丁实施报告

日期：2026-10-10。用户授权：根据源码改进清单，多开 subagent，以最小 patch 完善源码。
三个 worker 与主进程分文件实施、交叉审阅；没有提交或推送本轮改动。

## 结果

- R01、R02、R04、R06–R13 的具体缺陷已修复；R03 完成快照顺序和取消隔离。
- D01/D02/D04/D05/D06 已精简，真实订阅分发与研究流程保留。
- Codex 对照中的取消、断连、入口背压、线程所有权与 fork 隔离约束得到加强。
  仍使用 Athena 自有协议，没有宣称 Codex wire 兼容或完整 API 覆盖。
- 已完整阅读 `docs/代码规范.md`；修正本轮相关导入、异常说明、公开 docstring、
  单次简单 helper 和冗余 guard 状态。所有涉及的 Python 生产文件通过严格 docstring 检查。

当前状态与边界详见：

- [源码改进清单](../reviews/2026-10-10-source-improvements.md)
- [Codex 设计差异台账](../reviews/codex-app-server-comparison.md)
- [app-server 指南](../athena-guide/02-app-server.md)

## 核心行为

失败的 handoff 不再误读旧产物；GUI RPC 的发送锁、发送与响应共享 deadline，
断连/取消清理 pending；人类审批轮询按 session/epoch 隔离且防重叠。
事件快照支持 coroutine、Task、Future，发布取消不会取消共享快照。

客户端取消事件读取会回收 getter 并保存已经取出的消息；发送失败也会清理请求。
Transport 通过同步队列操作与变更信号协调关闭，不再依赖可能无法入队的 EOF sentinel。
服务器有界 pending 队列拒绝超载请求，保留控制回复路径；服务端反向请求在发送、
响应、取消和关闭时都清理。关闭错误回复采用尽力发送，避免满响应队列阻塞 shutdown。

线程在锁内启动和登记，重复 ID 先拒绝，取消清理未登记 runtime；fork 使用独立文件，
以完成 turn 的 marker 恢复指定历史。内存模式仅保证最新完成快照，较早历史明确拒绝。
Rust provider 以 HTTP 状态和 DONE 标记判断成功，跨块保留 UTF-8 字节，等 header/chunk 可取消。

## 验证证据

最终核心 Python 命令：

```powershell
.venv\Scripts\python.exe -m pytest -q tests/unit/app_server tests/unit/research/test_handoff_agent_events.py tests/integration/test_tui_protocol.py tests/test_gui_gateway_transport.py tests/unit/research/test_message_identity.py tests/unit/test_rollout.py tests/unit/agent/test_agent_runtime.py tests/unit/agent/test_agent_runtime_reap.py tests/unit/agent/test_agent_runtime_waits.py tests/unit/agent/test_memory_restore.py
```

结果：**262 passed、24 subtests passed**。
将下列解析/注册选择集加入上述命令的联合运行此前得到 **335 passed、27 subtests passed**；
之后的逻辑收尾仅影响已重跑的核心选择集：

```powershell
.venv\Scripts\python.exe -m pytest -q tests/unit/agent/test_prepare_agent.py tests/integration/research/test_prepare_agent_contract.py tests/unit/test_tex_source.py tests/unit/test_paper_source.py
```

该选择集 73 项通过。初始基线 1 失败来自旧测试 provider 仍输出已移除的
`PlanDecision.suggestions`；仅修正该测试桩，没有放宽生产 schema。

GUI 与 Rust（由负责 subagent 执行）：

```powershell
# athena-gui/
npm test -- --run src/hooks/__tests__/usePipeline.test.tsx
npm run build
# athena-gui/src-tauri/
cargo test --lib
cargo fmt -- --check
# athena-rust/
cargo test -p athena-agent
cargo fmt --check
```

结果：hook 49 项通过；GUI build 通过；Tauri Rust 10 项通过；独立 Rust agent 15 项通过，
其中 5 项新增本地 HTTP 回归。Tauri 真实网关测试在拆连接时打印基线已有的
`ConnectionResetError`，测试返回成功。没有访问真实模型服务。

规范检查：`scripts/check_code_style.py --enforce-docstrings` 对本轮涉及的 16 个 Python
生产文件通过；`git diff --check` 通过。新增文档的本地链接检查通过。
26 个既有无关文件的哈希与开始时相同；已有改动的 `phase_runner.py` 额外差异仅为
RunStatus 导入、logger 位置、handoff 状态检查和注释，既有逻辑完整保留。

## 明确保留的工作

- R03/C07/C13：慢读者隔离、事件溢出重同步和关闭时在途 emit drain 需要单独定义。
- R05：结构化数据路径需连同旧 checkpoint 迁移，未改写其 prose 兼容链。
- R14/C12/C14：历史保留、游标过期和权威状态读取不能以直接截断代替设计。
- D03：重复解压优化先取得性能证据，暂保留有界解压和阶段错误语义。
- R10：旧文件缺少 turn marker 时不伪造历史截点；本轮没有做旧历史格式迁移。

这些是原清单中的设计/性能候选或明确剩余边界，并非本轮声称已经关闭的缺陷。
没有执行全仓测试、真实模型长任务或 Codex 互操作验收。

本轮计划验收完成并删除；旧 GUI、error-ledger、module-simplification 计划保持开放。
