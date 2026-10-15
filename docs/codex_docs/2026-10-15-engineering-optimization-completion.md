# Athena 全仓工程与代码规范优化完成报告

日期：2026-10-15。范围由用户明确选择为全仓逐模块优化。

本报告记录本地工程优化阶段；后续上游合并、最终回归和已核验的提交推送见
[发布完成报告](2026-10-15-publication-completion.md)。

本轮通过并行模块审查和交叉复核，完成 Python 核心、Agent、研究流水线、执行层、
GUI/TUI/gateway、检索、Kaggle、预测服务、开发脚本和 Rust 的必要修复及回归。
以本轮开始时的工作区副本为比较基准，保留上一轮 hypothesis 修复和会话恢复
改动，以及用户已有的其他工作区修改。

逐文件决定、调用者和定向验证见[模块审查索引](../reviews/2026-10-15-engineering-optimization/README.md)。

## 关键结果

| 范围 | 问题与最终行为 |
| --- | --- |
| 修复循环 | 三份重复的脱敏诊断截断逻辑集中到一个策略，保留根因尾部；基础设施失败的持久化/通知路径合并。候选失败重试、基础设施暂停、可信历史最佳值结算的分界保持。 |
| 数据与评估 | 每次冻结 evaluator 只解析一次 metric/layout，验证、README 和 descriptor 共享同一结果；保留 legacy/v2 和隐藏标签边界。复核 prediction contract 与环境修复的有界策略。 |
| 工作区与人类输入 | 锁竞争失败者不再删除运行者 PID；解析无效 reply kind 时返回统一契约异常；用真实 reply 类替代不可用于 isinstance 的 Annotated union。 |
| GUI | 修复工作区异步加载覆盖新选择、WebSocket 同步发送失败残留 pending、旧人类回复跨会话/问题覆盖新状态。clarification 计数和恢复消息投影集中维护。 |
| trace | 阻止越界 agent 路径，递归脱敏工具参数/摘要；只解码外层 JSON，内层数字样式/null 样式字符串保持原类型。 |
| 执行与取消 | acquire/stage/channel 启动失败会关闭已获取资源；重复取消期间等待 backend close 后才释放 GPU 租约。spawn 的尽力取消发送设超时并消费晚到异常；EOF 后显式 close 仍停止 transport。 |
| 内存上下文 | 写入和导出均隔离嵌套工具参数/结果，外部修改不能绕过 token/version 计数；保留快照和压缩契约，修复 memory-first 冷导入循环。 |
| 检索与服务 | 修复重定向地址二次解码、缓存页面忽略当前字符限制；缺少可选模型库时明确报错；非法 Content-Length 返回 HTTP 400；CLI 拒绝 NaN/Inf 超时。 |
| 工具链 | headless runner 使用项目锁、显式非交互确认、正确失败退出和 finally 清理；构建使用独立 argv 并限定清理目录；PDF 提取异常也关闭文件。 |
| Rust | 工具事件采用 provider call_id；空闲 mux 避免忙轮询；满载请求可返回 Overloaded 并接受关闭。dispatcher 拥有 handler，满响应队列可被 stop 打断，重复 shutdown/Drop 的资源所有权有回归。 |

## 规范落实

- `src/` 和 `scripts/` 的严格规范门禁通过，pre-commit 已启用严格 docstring。
- 检查器拒绝缺失路径、空目录、非 Python 目标、不可读/无效编码/语法错误，
  避免“没有检查却通过”；支持重复调用、去重目标、UTF-8 BOM，按 AST/token
  区分代码、示例字符串与注释。
- 新增特定 except 注释存在性检查；人工复核注释是否说明实际输入、资源或状态，
  处理真实的深分支，保留必要的资源/异常边界。
- 整理公开 API 文档、导入和重复逻辑。空行规则与项目固定 Black 版本统一，
  没有为了缩短代码删除取消、epoch、可信评估或公开契约保护。
- 冻结 GUI/TUI 启动脚本必须先安装 inspect 源码读取兼容处理，再导入会在
  初始化时检查源码的依赖。这两处模块级启动顺序明确保留并说明原因。

本轮不是单纯缩减行数：文档、触发条件说明和生命周期保护增加了代码。
已删除的重复诊断、重复契约解析和单次转发层，在模块审查记录中分别列明。

## 验证

| 检查 | 最终结果 |
| --- | --- |
| Python 全仓默认测试集 `pytest -q` | 3126 passed，1 skipped，52 subtests passed，1 warning；523.28 秒 |
| 根目录 `npm test` | 259 GUI tests、5 build tests、1 launcher test 全部通过 |
| GUI TypeScript/Vite build | 通过（tsc + Vite） |
| Rust workspace offline tests | 92 passed；本地假 HTTP/transport，不连接真实模型服务 |
| Rust fmt / all-target Clippy | 通过，Clippy 使用 `-D warnings` |
| Tauri native bridge | offline check/test 与 fmt 通过，10 tests |
| Python strict style / Black / compileall | 通过；Black 覆盖 245 个生产与脚本文件 |
| `git diff --check` | 通过 |

上表为本轮验收。逐模块定向测试互有重叠，不累加成另一套总数。
关键缺陷采用先复现再修复的测试，覆盖锁竞争、消息别名、HTTP 输入、缓存限制、
重复取消、远端清理、GUI 迟到响应和 Rust 饱和/关闭。

Python 使用项目默认 `-m 'not slow'` 配置，未额外执行 slow 集合。
跳过项与本机未安装 PowerShell 7、PowerShell 5.1 不支持 `&&` 有关。
Windows asyncio 的 subprocess finalizer 仍有一条既有 closed-loop 警告，
没有屏蔽该警告，也不把它记为已修复。

## 覆盖与历史台账

旧清单有 602 个基线文件，其中 231 个此前仍标 Pending。本轮补齐其中实际存在的
136 个 Pending 文件的逐文件决策；其余 95 个路径已不在当前仓库，明确记录为
历史删除/退役，不计为新审过的代码。全部基线中共有 226 个路径已不存在，
其中包括用户此前要求移除的 `athena_ts/` 138 个条目，不重建该目录。

另外核对基线之外的当前新增文件，并保留各模块原有已审结论及本轮差异复查。
[审查索引](../reviews/2026-10-15-engineering-optimization/README.md)列出
56 个当前代码/源文件路径及其模块记录；这些路径不重复计入 602 个基线文件。
[历史台账](../reviews/2026-10-15-engineering-optimization/module-review-ledger.md)已更新现存文件和退役路径的状态，并在后续发布完成后归档。
本轮实施计划在验收后移除，CURRENT 更新为完成报告；其他 GUI/error-ledger
工作保持原状态。未把未完成的历史远端发布事项标记成已执行。

## 验证范围与恢复记录

- 未启动新的付费模型/SEARCH 实验。此前经用户授权完成的预算 4、并行 1、
  CUDA 数据集复验记录保留于[上一轮 hypothesis 修复报告](2026-10-15-hypothesis-repair-completion.md)；
  本轮使用完整本地默认回归。
- 未做真实 SSH 主机、真实模型 API、Kaggle 提交或安装器打包验证；相应测试使用
  本地进程、假 provider/transport 或命令组合桩。
- Python backend close 仍需底层清理最终结束；不会承诺自动恢复永不返回的驱动。
  Rust 正常 stop 路径会 join handler；强制超时兜底和同步 Drop 只能请求子任务
  取消，不能承诺在该同步路径等待全部子任务退出。
- 一次批量注释写入误用了 PowerShell 默认编码，损坏 9 个文件。已保全事故副本，
  从本轮前副本与当前差异恢复，逐字节重放校验保留全部修改，随后完成语法和全量
  回归；没有用 HEAD 覆盖未提交工作。
- 未提交或推送整个脏工作区；用户原有改动、会话和数据集证据均保留。

本地详细日志与恢复证据：
`submission/reproduction/2026-10-15-engineering-optimization/`。
