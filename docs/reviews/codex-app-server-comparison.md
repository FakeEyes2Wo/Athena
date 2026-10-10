# Athena 与 Codex app-server 设计差异台账

状态：持续维护入口；2026-10-10 实施最小修复，2026-10-11 完成提交后复审修正，当前状态见下表。

Athena 借鉴了 Codex 的 Thread/Turn、双向请求和事件流组织方式，但当前实现不是
Codex app-server 的直接协议兼容层。最值得对齐的是资源所有权、取消、断连清理、
队列容量和分支历史隔离这些行为约束；研究流程及展示层不必复制上游。

## 对照基线

- Athena：`8fcc801e` 加本地未提交源码改动，具体范围见[源码清单](2026-10-10-source-improvements.md)。
- Athena 提交后复审：`e3bf0969` 及 2026-10-11 的 fork/rollout 修正；没有更新上游快照。
- 上游：OpenAI `openai/codex` 公共仓库 main 在本次查询时的
  [提交 806d9732c974bc8a51b8317c1bd8985544fe627c](https://github.com/openai/codex/commit/806d9732c974bc8a51b8317c1bd8985544fe627c)，
  committer 时间 `2026-10-10T08:51:21Z`，标题 `Report exec runtime resets before accepting further requests (#52825)`。
- “当前 Codex”在本文只指这个固定公共源码快照，不指本机安装版本，也不推断闭源桌面应用内部实现。
- 另核对了[官方 app-server 文档](https://learn.chatgpt.com/docs/app-server)：
  双向 JSON-RPC、初始化、Thread/Turn 与 schema 生成入口。动态文档今后可能变化，下面的源码链接均固定提交。
- 下载并校验了 15 个源码文件的 Git blob 身份；本地缓存位于忽略的 `build/codex-review/`，
  缓存不是文档运行依赖，持久证据为下方固定提交链接。没有执行上游代码或跑跨项目互操作测试。

## 运行路径与适用边界

```text
Athena GUI -> gui_gateway -> ResearchRuntime -> AgentRuntime -> RuntimeThreadManager
Athena 进程内 RPC -> AthenaClient -> Transport -> MessageProcessor -> ExecutionAdapter
                                                               -> RuntimeThreadManager
Codex client -> app-server 协议/连接处理 -> core ThreadManager -> thread/turn 执行
```

Athena GUI 没有统一经过进程内 MessageProcessor；只修改该处理器不会自动修复 GUI 桥的
pending 请求或 RuntimeEvents 快照顺序。另一方面 RuntimeThreadManager 被实际执行路径使用，
不能因 RPC facade 使用较少就删除整个 app_server。独立 `athena-rust/` 也不是 Python GUI 后端。

## 差异清单

### 本轮实施状态（优先于下方初次对照快照）

上游仍固定 `806d9732c974bc8a51b8317c1bd8985544fe627c`，本轮没有宣称新的上游版本。
Athena 初轮补丁已提交为 `e3bf0969`；初轮证据见[修复报告](../codex_docs/2026-10-10-source-review-repair-completion.md)。
2026-10-11 的复审补充见 C09 与末尾核对记录。

| 差异 | 当前处置 |
|---|---|
| C03 | 待执行队列已设上限（默认 max_inflight，可用 max_pending 调整），满队列返回自有 OVERLOADED；审批控制回复在执行饱和时仍可完成。仍保留 Athena 错误码。 |
| C04/C05/C06 | 已验证取消 getter/发送阶段 pending 清理、transport EOF 及时失败请求；服务端调用统一 finally 清理，关闭时失败 pending，包括阻塞发送场景。满响应队列不再让 shutdown 卡住。 |
| C08/C09 | 已验证重复线程 ID 拒绝与取消启动清理、独立 fork 文件和指定历史边界。2026-10-11 补充：引用与历史原子取快照；辅助 marker I/O 失败保留真实成功结果并记录警告，缺 marker 的指定历史 fork 拒绝，默认 fork 仍取最新完成内存。部分日志写入后先隔行再追加。无历史 marker 的旧 rollout 不支持精确历史恢复，无持久化的较早历史明确拒绝。 |
| C11 | 已删除占位 ThreadEventHandlerRegistry 和未消费注入；真实订阅/多路投递保留。 |
| C10 | 生命周期边界得到加强，但没有整体复制上游 TurnAdmission/连接架构；全生命周期审计仍开放。 |
| C13 | 已修复快照顺序和发布取消（含任意 awaitable）；研究投影保留。慢订阅者隔离/关闭回调 drain 仍开放。 |
| C01/C02/C07/C12/C14 | 保留既有协议、初始化和能力范围；本轮未宣称实现 Codex wire 兼容、thread/read、历史回收或完整慢读者隔离。 |

### 初次对照快照

状态说明：**保留差异**表示不必追齐；**兼容差异**表示不能声称直接互通；
**正确性缺口**有本地反例；**待专项验证**表示控制流风险尚缺完整生命周期测试。
下表是修复前的证据和目标；当前完成范围以实施状态和针对性回归为准。

| ID / 分类 | Athena 当前行为 | 固定上游证据与差异 | 后续验收条件 |
|---|---|---|---|
| C01 / 兼容差异 | [protocol.py](../../src/athena/app_server/protocol.py) 使用 `request_id`、`server_call_id`；事件有 `subscription_id/sequence/kind/data` | [U1] 使用 `id/method/params`、`id/result/error`；通知为 method/params。命名相似不足以兼容 | 明确维持自有协议还是增加边界适配器；若适配，使用固定上游 schema 做请求、响应和通知的契约测试 |
| C02 / 兼容差异 | [server.py](../../src/athena/app_server/server.py) 238 行起要求 initialize 的 ID 为 0，支持自有 `protocol_version=1`，状态属于整个 MessageProcessor | [U2/U11] 使用连接级初始化与能力状态，含 experimental API/通知选项；不能把 Athena 的版本号当 Codex schema 版本 | 记录连接级状态机，测试重复初始化、未初始化请求和不支持能力；若复用 processor 支持多个客户端，必须隔离状态 |
| C03 / 正确性缺口 R13 | server.py 125 行起无界 pending 位于有界 Transport 后，信号量只约束执行数 | [U3] 446、624–657 行使用有界 processor channel，满请求以 overload 返回，notification 满有明确处理 | 卡住 executor 后持续发送，队列有上限且请求及时失败；回复、取消等控制消息仍可达。错误码也不同：Athena 为 -32005，官方文档为 -32001 |
| C04 / 正确性缺口 R08/R11/R12 | [client.py](../../src/athena/app_server/client.py) 取消可遗留 getter/pending；[transport.py](../../src/athena/app_server/transport.py) 满队列关闭可能丢 EOF，closed 后仍可写 | [U3] 的请求取消 token/drop guard 和关闭流程提供另一种所有权设计，不能直接等同于 Python task 清理 | 发送中取消、读取中取消、满队列关闭均释放 waiters；pending 归零，关闭拒绝新请求。以本地反例驱动实现 |
| C05 / 待专项验证 | client.py 99–105 行 control EOF 只结束事件流；fail_all_pending 在显式 cleanup 才调用 | [U4] 510–518 行退出读循环后逐一失败 pending RPC；[U3] 780–790 行也清理请求 | 挂起 RPC 后注入 EOF，立即得到连接关闭错误，无需等业务 timeout |
| C06 / 待专项验证 | server.py 287–330 行的服务端调用 map 主要靠回复/超时清理，shutdown 未显式 drain | [U5] 538–555 行 cancel_all_requests drain callback；[U3] 关闭时调用 | 挂起 approval/user-input 后断连/关闭，future 结束且 map 归零；确认上层任务取消的实际作用 |
| C07 / 待专项验证 | client.py 116–127 行事件 buffer 满后等待，再逐层反压 transport/FairMux，可能影响其他订阅 | [U6] 365–369 行本地 client event queue 为无界以避开前台请求死锁；[U3] 745–760 行服务端仍有界并区分事件策略 | 停止消费一个订阅，检查其他订阅和 RPC 延迟；逐类定义背压、丢弃或重同步。上游无界队列也有内存代价，不能直接照搬 |
| C08 / 正确性缺口 R09 | [thread_manager.py](../../src/athena/app_server/thread_manager.py) 102–139 行重复显式 ID 覆盖旧 runtime，旧任务失去 owner | [U7] 2451–2478 行用 Vacant entry 注册；重复实例执行 shutdown_and_wait 后报错 | 串行/并发重复 start 均不覆盖，取消和失败不遗留 runtime，manager close 后全部退出 |
| C09 / 正确性缺口 R10 | manager.fork 继承 session_id，rollout 文件按 session_id 命名，父子写同一路径 | [U7] 1492–1640 行 fork 有独立线程身份和历史构建；既有 Copied 也有内部 Referenced 持久化策略 | 分开来源身份、分支身份和可写存储；父子继续执行后历史互不污染，指定截点恢复正确。不要求复制整个历史文件，也不把内部 Referenced 当公开 API 承诺 |
| C10 / 设计核对项 | Athena 的 semaphore、TaskGroup、manager close 分散承担生命周期控制；R09/R12 证明部分边界未闭合 | [U8] 将 drain/admit 放在同一锁内并用 permit 维护 active；[U9] 用 JoinSet 收集、drain/abort 连接清理任务 | 列出每类 task/future/queue 的唯一 owner；验证 shutdown 开始后不接纳新工作、在途完成或明确取消，超时后无孤儿任务 |
| C11 / 冗余候选 D04/D05 | [lifecycle.py](../../src/athena/app_server/lifecycle.py) 的 ThreadEventHandlerRegistry 只存 None；set_event_system 保存的字段无消费 | [U10] 维护连接、线程与 listener 的实际关系；同名 registry 并不等价 | 删除占位装配或补清晰契约；以真实订阅/断连行为测试替代仅断言占位字典 |
| C12 / 保留差异、待度量 R14 | Athena EventJournal + after_sequence 重放与研究投影属于自己的契约，当前内存保留无界 | 本轮未证明上游存在与 Athena Journal 等价的统一保留窗口；不能据此宣称 Codex 已解决该问题 | 做长时内存测量，定义游标过期/归档/重同步及 fork 需要的窗口后再回收 |
| C13 / 保留差异，含本地缺口 R03 | GUI 使用研究专用事件和初始快照，不通过上述 RPC 通知层；当前并发 publish 可越过快照 | [U1/U2] 的通用 Thread/Turn 通知不能直接替代研究树、阶段和审批展示投影 | 保留研究投影，单独保证 snapshot-before-delta、session/epoch 隔离和慢读者策略；不把 R03 当上游同一路径的缺陷 |
| C14 / 兼容差异、能力边界 | [ExecutionAdapter](../../src/athena/app_server/execution.py) 19–68 行只覆盖 start/fork、turn start/interrupt、订阅管理，没有 thread/read；内存游标重放不等于权威状态读取 | [U2] 提供 thread/read 等更广方法集合，并区分 snapshot 与活动通知 | 发布支持方法清单；明确仅支持活进程内重放，或定义状态 snapshot + cursor 的恢复契约。不能把 GUI 的研究快照视作已有 RPC thread/read |

## 固定源码索引

以下路径均属于同一个上游提交；行号为本轮阅读定位，更新基线时必须重新核对。

- [U1：协议信封 rpc.rs](https://github.com/openai/codex/blob/806d9732c974bc8a51b8317c1bd8985544fe627c/codex-rs/app-server-protocol/src/rpc.rs)
- [U2：app-server README](https://github.com/openai/codex/blob/806d9732c974bc8a51b8317c1bd8985544fe627c/codex-rs/app-server/README.md)
- [U3：进程内通道、取消和关闭 in_process.rs](https://github.com/openai/codex/blob/806d9732c974bc8a51b8317c1bd8985544fe627c/codex-rs/app-server/src/in_process.rs#L624)
- [U4：远程客户端退出清理 remote.rs](https://github.com/openai/codex/blob/806d9732c974bc8a51b8317c1bd8985544fe627c/codex-rs/app-server-client/src/remote.rs#L510)
- [U5：服务端调用清理 outgoing_message.rs](https://github.com/openai/codex/blob/806d9732c974bc8a51b8317c1bd8985544fe627c/codex-rs/app-server/src/outgoing_message.rs#L538)
- [U6：客户端事件队列 lib.rs](https://github.com/openai/codex/blob/806d9732c974bc8a51b8317c1bd8985544fe627c/codex-rs/app-server-client/src/lib.rs#L365)
- [U7：核心线程与 fork thread_manager.rs](https://github.com/openai/codex/blob/806d9732c974bc8a51b8317c1bd8985544fe627c/codex-rs/core/src/thread_manager.rs#L1492)
- [U8：turn admission](https://github.com/openai/codex/blob/806d9732c974bc8a51b8317c1bd8985544fe627c/codex-rs/app-server/src/turn_admission.rs)
- [U9：连接清理任务所有权](https://github.com/openai/codex/blob/806d9732c974bc8a51b8317c1bd8985544fe627c/codex-rs/app-server/src/connection_cleanup.rs)
- [U10：线程和连接状态](https://github.com/openai/codex/blob/806d9732c974bc8a51b8317c1bd8985544fe627c/codex-rs/app-server/src/thread_state.rs#L322)
- [U11：每连接初始化状态](https://github.com/openai/codex/blob/806d9732c974bc8a51b8317c1bd8985544fe627c/codex-rs/app-server/src/message_processor.rs#L176)

另外抽查了 `message_processor.rs`、`connection_rpc_gate.rs`、`request_serialization.rs`、
`transport.rs`、`request_processors/thread_lifecycle.rs`。这不是上游全量代码审计。

## 后续跟踪规则

在修改 app_server、GUI 传输或升级所参考 Codex 基线时更新本文。当前没有设置后台定时任务。

1. 记录 Athena HEAD 与有关工作区差异；读取 `codex_docs/CURRENT.md`，按当时活动计划推进。
2. 查询上游 main 的新 SHA 和时间，保留本轮 SHA；先比较上述路径的提交差异，再阅读受影响调用链。
   若开始追踪上游稳定 release，另记 tag 到 SHA 的解析结果，不能静默把 main 当 release。
3. 保留 C01–C14 ID。每项写明旧/新上游行为、Athena 实际行为、影响路径和证据等级；新增项另编号。
4. 修复生命周期边界时同步复核本表验收条件和 R01–R14，必要时加故障注入回归。
   协议兼容目标若改变，必须明确支持的 schema/能力集合，并新增互操作验证。
5. 只有修复提交与针对性测试都存在才标“已关闭”；有意保留的差异记录理由。
   普通回归通过不足以关闭当前已有反例。

| 核对日期 | 上游基线 | 结论与验证 |
|---|---|---|
| 2026-10-10 | `806d9732c974bc8a51b8317c1bd8985544fe627c` | 首次固定公共源码基线，建立 C01–C14；Athena 相关回归 171 passed、25 subtests passed；边界反例见源码清单；尚无跨项目互操作验证 |
| 2026-10-10 修复轮 | 同上 | C03–C06、C08/C09、C11 的具体缺口修复并验证，C10/C13 部分改善；最终 Python 核心回归 262 passed、24 subtests passed；GUI/Rust 证据见报告，仍无跨项目互操作验收 |
| 2026-10-11 提交后复审 | 同上，未重新查询上游 | C09 追加快照一致性、checkpoint I/O 结算及损坏尾行恢复回归；隔离的原提交 335 passed、27 subtests passed，修正后 339 passed、27 subtests passed；这些是 Athena 的本地行为验证，不是上游实现或协议互通证明 |

下一轮重点：慢事件消费者 C07/C13、历史保留与恢复 C12/C14，及有需求时再确定
协议兼容 C01/C02 的目标；已经修复的边界继续保留回归，不重复当作开放缺陷。
