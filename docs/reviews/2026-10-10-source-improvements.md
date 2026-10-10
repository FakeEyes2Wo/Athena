# 源码改进与冗余清单

核对日期：2026-10-10。Athena 基线为 `8fcc801e` 加本地未提交源码改动。
提交后复审：2026-10-11，审阅 `e3bf0969` 并补充下述修正；上游对照基线不变。
状态：包含初次审查记录和本轮最小补丁结果；**以下修复状态表为当前状态**。
后面的原始清单保留触发条件和审查证据，不代表缺陷仍全部存在。
Codex 对照另见[差异台账](codex-app-server-comparison.md)。

## 2026-10-10 最小补丁结果

用户授权多个 subagent 实施后，三个 worker 与主进程按文件分工修复并交叉审阅。
已阅读并按[代码规范](../代码规范.md)复核新增/修改代码；保留既有工作区改动。
完整命令与限制见[实施报告](../codex_docs/2026-10-10-source-review-repair-completion.md)。

| 条目 | 当前状态与验证 |
|---|---|
| R01 | 已修复失败误读：先检查真实 RunStatus.COMPLETED，再允许已有文件回退；FAILED/INTERRUPTED/RUNNING 都拒绝旧文件，仍正常 reap。完成但没有结构化响应时保留原文件契约，没有引入新产物身份迁移。 |
| R02 | 已修复：断连 drain pending，发送/锁等待/响应共用 deadline，错误和取消清理；普通 RPC 5 分钟，报告/验证 30 分钟。Rust bridge 10 项测试通过，包含 blocked-send deadline。 |
| R03 | **部分完成**：每订阅者锁保证 snapshot-before-delta，shield 防止一个发布者取消共享快照；Task/Future 返回值同样等待。并发及取消回归通过。慢订阅者仍形成背压；有界隔离队列与溢出重同步需要独立契约，未改成静默丢弃。关闭期间已开始的 emit 也没有新增加 drain 契约。 |
| R04 | 已修复：每会话/epoch 单在途轮询，成功和失败回调都检查会话，effect 依赖 currentSessionId；hook 49 项测试和 GUI build 通过。 |
| R05 | **保留候选**：结构化路径替代持久化 prose 涉及 state schema、旧 checkpoint 迁移和 SEARCH/FINAL 兼容；本轮不以小范围字符串改写冒充迁移。 |
| R06/R07 | 已修复并覆盖：拒绝非 2xx、无 DONE 的截断流和无效 SSE；完整行到达后解码 UTF-8，等待 header/chunk 可取消。独立 Rust agent 15 项通过（新增 5 项本地 HTTP 测试）。取消沿用 Provider Error 契约。 |
| R08/R11 | 已修复：取消事件等待会 join getter 并保留已取出消息；请求发送和响应等待共同受 finally 清理 pending。timeout 仍为既有发送/响应各阶段预算，不宣称总 deadline。 |
| R09 | 已修复：锁内启动并登记，重复 ID 在创建前拒绝，启动取消/失败清理未登记 runtime；重复、关闭竞争及取消回归通过。 |
| R10 | 已修复父子同文件：独立 child rollout key，按 completed-turn marker 恢复指定历史。2026-10-11 复审补充：引用和历史在同一次 submission 中取快照，避免并发完成造成错配；辅助 marker 写入失败会记录警告并保留实际成功结果，缺 marker 的指定历史 fork 明确报错，默认内存快照仍可用。日志 I/O 失败后的追加先补换行，防止与半行粘连。无持久化时仅支持最新完成的内存快照；没有 marker 时不伪造历史截点。 |
| R12/R13 | 已修复：关闭与队列操作同步线性化，唤醒所有等待方并拒绝新写；server 待执行队列有界，满时 OVERLOADED，控制回复仍可达。满响应队列时关闭错误回包不阻塞，shutdown 最终关闭 transport。 |
| R14 | **保留候选**：未直接截断 Journal/完成索引，避免破坏重放和 fork；需要保留窗口、游标过期响应和长时内存证据。 |
| D01/D02 | 已精简：删除两层纯转发 `_register`；公共 source_payload 统一 tar 判断与大小常量，保留两端错误映射。相关 73 项测试通过；一份旧测试桩移除了 PlanDecision 已不接受的 suggestions 字段。 |
| D04/D05/D06 | 已删除占位 registry、未消费 setter/注入和 trace_context；真实 SubscriptionRegistry/FairMux 保留，接口与测试同步调整。 |
| D03 | **保留候选**：没有实测性能证据，跨阶段复用解包结果会改变资源生命周期；暂保留有界解压。 |

最终 Python 核心选择集：262 passed、24 subtests passed；另一个未再修改的解析/注册选择集
73 项通过。此前联合运行结果为 335 passed、27 subtests passed。不是全仓测试或真实模型端到端验收。

2026-10-11 从实际提交创建隔离 worktree：原提交 **335 passed、27 subtests passed**；
仅应用审阅修正后的相同选择集 **339 passed、27 subtests passed**，覆盖快照竞争、
marker 写入失败、部分写入及 flush 错误恢复。没有混入既有未提交研究流程改动。
具体审阅发现、提交和推送证据见[提交后审阅报告](../codex_docs/2026-10-11-source-repair-review-completion.md)。

## 范围和证据等级

- 结构扫描涉及 325 个生产源码文件，约 69,416 行：Python 主程序/TUI、GUI 前端、
  Tauri 桥、独立 Rust crates。排除了 colocated tests；不是全量逐行正确性审计。
- 三个 subagent 分模块阅读；主进程追踪调用链、执行最小复现，并专项阅读 app_server。
- **复现**：实际调用当前实现，以内存桩或临时目录触发边界，观察到问题。
- **静态确认**：源码控制流支持结论，尚未做完整产品端到端复现。
- **改进候选**：耦合/资源增长风险，不能宣称已发生生产故障。
- `phase_runner.py`、`prepare/data.py`、`execution/runtime.py` 等文件有既有未提交改动；
  相关结论针对工作区，不能只用 `8fcc801e` 的源码重现所有行为。

运行路径必须区分：

```text
GUI -> gui_gateway -> ResearchRuntime -> AgentRuntime -> RuntimeThreadManager
独立进程内 RPC -> AthenaClient/Transport/MessageProcessor -> ExecutionAdapter -> RuntimeThreadManager
独立 Rust workspace -> athena-agent / athena-server（不是 Python GUI 后端）
```

## 研究流程与 GUI

| ID / 优先级 | 位置与证据 | 触发条件、影响和建议 |
|---|---|---|
| R01 / 高 / 复现 | [PhaseRunner._run_handoff_agent](../../src/athena/research/runtime/phase_runner.py)，约 186–195 行；[load_agent_result](../../src/athena/research/supervisor/experiment.py)，105 行起 | `load_agent_result` 对非 COMPLETED 返回 None；调用方看到 None 后，只要文件存在便返回内容。FAILED + 旧 `EDA_INDEX.md` 已返回旧内容，掩盖本次失败。区分失败与“完成但无结构化结果”，为允许复用的产物建立 run/task 身份校验。不能简单删除所有旧文件，恢复流程可能有合法复用。 |
| R02 / 高 / 静态确认 | [Tauri PythonBridge](../../athena-gui/src-tauri/src/python.rs)，140–196 行 | 接收循环遇 Close/Error 直接退出，没有 drain pending；send 失败也未删除已插入的请求，`rx.await` 无 deadline。断连可能使桌面操作一直等待。应在连接失效时唤醒全部请求、清理发送失败项并设置调用 deadline。 |
| R03 / 高 / 复现 | [RuntimeEvents._publish](../../src/athena/research/runtime/events.py)，375–407 行 | 首次发布在 `await ready` 前把订阅者置为 `(emit, None)`，并发第二次发布绕过初始快照等待。实际顺序为 `second -> snapshot -> first`。另外 `gather` 会等待最慢订阅者，慢 UI 可阻塞生产者。建议每订阅者独立、有界、串行投递，并明确溢出后重同步策略，而非无界 fire-and-forget。 |
| R04 / 中 / 静态确认 | [usePipeline humanPending 轮询](../../athena-gui/src/hooks/usePipeline.ts)，1080–1117 行 | `setInterval` 可重叠调用；结果仅检查 effect 的 cancelled 标记，未检查 session/epoch，依赖也不包含 session ID。相同状态间切换会话时旧响应可能写回新会话。加单在途限制，并在成功和失败回调中检查发起时的 session/epoch。其他 hook 路径已有 epoch 保护，不能笼统说整个 hook 都没有。 |
| R05 / 中 / 改进候选 | [DataContract.contract_text](../../src/athena/research/prepare/data.py)，61 行；[预测路径解析](../../src/athena/research/runtime/phase_runner.py)，53–94 行 | 持久化英文 prompt，再用固定英文 marker 和 `. Read it from` 切出路径。文案变化、旧合同迁移或分隔符进入路径会影响执行。把 SEARCH/FINAL 路径存成结构化 state，prompt 由它生成；旧文本只在迁移入口解析。 |

## 独立 Rust provider

这里的风险属于保留的 Rust workspace，不代表当前 Python provider 有相同问题。

| ID / 优先级 | 位置与证据 | 触发条件、影响和建议 |
|---|---|---|
| R06 / 高（启用该 provider 时）/ 静态确认 | [OpenAiProvider.run_stream](../../athena-rust/crates/athena-agent/src/openai_provider.rs)，86–96、190 行；[Agent.sampling_step](../../athena-rust/crates/athena-agent/src/agent.rs)，约 195 行 | HTTP send 成功后不检查状态；普通错误 JSON 被忽略，EOF 默认发出 `ResponseCompleted(stop)`。4xx/5xx 或缺少完成标记的截断流可能被解释为成功。检查 HTTP 状态，并独立校验协议完成条件，保留可诊断错误。 |
| R07 / 中 / 静态确认 | [流读取和解码](../../athena-rust/crates/athena-agent/src/openai_provider.rs)，102–118 行 | 每块独立 `from_utf8_lossy`，跨网络块的多字节字符可能变为替换字符；cancel 仅在下一块抵达后检查。采用跨块字节缓冲/增量解码，使用 `select!` 同时等待数据、取消与 deadline。两个问题需要各自的分块/停流测试。 |

## app_server 与记忆边界

| ID / 优先级 | 位置与证据 | 触发条件、影响和建议 |
|---|---|---|
| R08 / 高 / 复现 | [ClientWorker.next_event](../../src/athena/app_server/client.py)，132–162 行 | 外层取消时两个 `Queue.get` 子任务未清理。取消读取后新事件被遗留 getter 取走，下一次读取得到 None。统一在 finally 取消/等待子任务，同时处理取消瞬间已取出的消息；不能只取消仍 pending 的任务。 |
| R09 / 高 / 复现 | [RuntimeThreadManager.start](../../src/athena/app_server/thread_manager.py)，102–139 行 | 同名 thread ID 直接覆盖 `_handles`。重复创建后，manager 关闭只关闭新实例，旧 `loop_task` 仍活着。锁内预留/校验 ID；创建失败和取消时关闭未入表 runtime。不能以默认 UUID 足够随机代替显式 ID 的边界保护。 |
| R10 / 高（rollout_dir + manager.fork）/ 复现 | [fork 与文件命名](../../src/athena/app_server/thread_manager.py)，87、167 行；[RolloutRecorder](../../src/athena/memory/rollout.py) | fork 沿用 session_id，rollout 又按 session_id 命名，父子 recorder 实际指向同一文件。后续记录会混合分支；历史恢复也不是“独立分支快照”。应区分逻辑来源身份和可写历史身份，为分支建立独立存储，并校验 after_turn_id 对应的历史截点。 |
| R11 / 中 / 复现 | [AthenaClient._raw_request](../../src/athena/app_server/client.py)，342–367 行 | register pending 后进入 send；发送阶段只 catch OverloadedError。队列满时取消，pending 仍保留 1 项。将发送和响应等待包进同一清理作用域，并明确 timeout 是整体 deadline 还是每阶段预算。 |
| R12 / 中 / 复现 | [Transport.aclose](../../src/athena/app_server/transport.py)，101–117 行及发送方法 | 满队列投 EOF 超时后仍将该队列标记为已投 EOF；发送不检查 closed。已观察 closed=True 后新请求仍成功入队，以及关闭后队列耗尽却等不到 EOF。定义幂等关闭、拒绝新写入、唤醒所有等待方的统一契约。 |
| R13 / 中 / 复现 | [MessageProcessor._dispatch](../../src/athena/app_server/server.py)，125 行起 | 有界 Transport 后接无界 pending。control_capacity=1、max_inflight=1，32 次请求仍形成 30 个 pending + 1 个等待 slot + 1 个执行中请求。为待执行队列设置上限/拒绝语义，同时让取消、回复等控制消息保持可达。 |
| R14 / 中（长寿命线程）/ 改进候选 | [EventJournal](../../src/athena/app_server/events.py)，38–79 行；[ThreadRuntime](../../src/athena/app_server/thread_runtime.py) 的 `_turn_done`、完成结果/上下文索引 | Journal 和历史索引在单线程存活期持续保留，尚未做长时内存测量。先定义重放与 fork 的历史窗口、归档及游标过期响应，再决定回收。不能通过直接截断列表破坏 after_sequence。 |

## 冗余与保留决定

| ID | 证据 | 建议与边界 |
|---|---|---|
| D01 | [prepare_agent._register](../../src/athena/agents/prepare_agent.py) 与 [task_agents._register](../../src/athena/agents/task_agents.py) 都原样转发给 register_prompt_agent | 可直接使用既有公共函数，保留 agent-specific output/tool/workspace 参数。验证 callable workspace/tools，不必再增加一层大工厂。 |
| D02 | [tex_source.is_tar_payload](../../src/athena/research/literature/paper_markdown/tex_source.py) 与 [fetcher._is_tar_payload](../../src/athena/research/literature/paper_source/fetcher.py) 的 AST 函数体相同；两端重复源大小常量 | 合并基础 payload 探测/限制，保留各阶段不同错误映射，优先级低。 |
| D03 | fetcher 分类 gzip 时解压，processor 后续将同一 artifact 交给 tex_source，再次解包 | 可能增加大批论文处理开销，尚无性能测量。评估头部探测或复用解包结果；缓存的内存/存储成本也必须计入。 |
| D04 | [ThreadEventHandlerRegistry](../../src/athena/app_server/lifecycle.py)，27 行起，只存 `{thread_id: None}` 并清空；ExecutionAdapter 仍调用 attach | 是占位依赖链，不是实际 listener 注册。考虑连同生命周期注入清理；真正事件传递由 SubscriptionRegistry/FairMux 完成。现有测试仅断言占位字典，不证明有回调能力。 |
| D05 | [MessageProcessor.set_event_system](../../src/athena/app_server/server.py)，73 行，只保存 `_event_handlers` / `_mux`，没有读取 | 可以和 D04 一起去掉无消费的状态、setter 与装配代码；注意公共导出与测试迁移。 |
| D06 | [Submission.trace_context](../../src/athena/app_server/submissions.py)，58 行，仅声明，无生产/消费路径 | 未接入的 tracing 占位字段；删除或明确实现契约，不能把字段存在当 tracing 已可用。 |

以下看似重复的机制**不要直接删除**：

- `ExecutionRuntime.run -> LocalBackend.run -> CommandExecutor.run` 的多边界校验。
  `_validate_command_input` 已是共享函数，进一步合并必须考虑直接 executor 调用与其他后端。
- GUI/TUI 的不同展示投影、Python/Rust 各自协议 DTO，不能只因名称类似就合并。
- 既有[冗余阅读记录](2026-10-10-redundant-mechanisms.md)中的 factory 参数探测和
  PREPARE 注册样板仍是候选；evaluator 的 workspace 重绑定语义必须保留。

## 验证记录

2026-10-10 重新运行：

```powershell
.venv\Scripts\python.exe -m pytest -q tests/unit/app_server tests/unit/research/test_handoff_agent_events.py tests/integration/test_tui_protocol.py tests/test_gui_gateway_transport.py
```

结果：**171 passed，25 subtests passed**。该结果与下列反例并不矛盾：现有测试未覆盖相应边界。

| 最小复现 | 实际观察 | 与测试覆盖的差别 |
|---|---|---|
| R01：FAILED summary + 临时目录中旧 handoff | 返回 `stale previous-run output` | 现有 handoff 测试为 COMPLETED 且预先写文件 |
| R03：阻塞 snapshot，连续启动两次 publish | `second, snapshot, first` | 已有 snapshot 测试只有一次 publish |
| R03：阻塞一个 subscriber | publish task 不能完成，释放 subscriber 后才结束 | 验证背压耦合，不代表已做实际网络拥塞测量 |
| R08：取消 next_event 后放入一个事件 | 下一次读取超时，返回 None | getter 子任务仍然消费了事件 |
| R09：两次 start 显式同 ID，再 close manager | 原 runtime loop 仍存活 | 探针末尾手动关闭原 runtime，没有遗留探针任务 |
| R10：临时 rollout_dir，创建 parent 后 fork | 两个 recorder.path 相等 | 未声称执行了长历史回放或真实分支任务 |
| R11：填满 c2s，request 进入 send 后 cancel | pending 数量仍为 1 | 响应等待阶段的取消覆盖不到发送阶段 |
| R12：关闭 transport 后发送；满队列关闭后耗尽 | 接受 EOF 后的新请求；后续 recv 等不到 EOF | 验证实际关闭边界 |
| R13：入口容量/执行上限均为 1，连续发 32 请求 | 无界 pending 长度 30 | 不是内存压力基准，但证明入口背压被转移 |

复现采用当前函数、内存桩和系统临时目录，不访问真实模型/API，不改生产源码。
优先回归 R01/R03/R08/R09/R10；随后修请求与关闭契约，再按性能证据处理 D03/R14。
后续修复继续按当前状态表和验收条件推进，候选项不等于已有生产故障。
