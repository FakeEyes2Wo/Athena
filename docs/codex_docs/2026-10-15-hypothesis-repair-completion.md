# Hypothesis 修复循环与 GUI 恢复修正完成报告

完成日期：2026-10-15。范围：SEARCH 失败反馈、同 Plan 修复与结算，其他数据集的输入/评估契约，
以及含中断会话的 GUI 工作区恢复。多名 subagent 分工审计、实现和独立复核，修改保留在当前工作树，未暂存、提交或推送。

以上是本轮修复验收时的状态；后续工程优化、上游集成和提交推送已经完成，见
[发布完成报告](2026-10-15-publication-completion.md)。

已完成代码修正和真实 GUI 复验。用户追加授权的第二个会话使用搜索预算 **4**、并行 **1**，
最终为 `COMPLETED`，没有剩余活动 Plan；实际模型训练使用 NVIDIA RTX 4070 Laptop GPU 的 `cuda:0`。
这证明该数据集能够走完受限的修复、评分和结算流程，不代表所有候选错误都会被模型修好。

## 发现并修正的缺陷

| 缺陷及影响 | 修正后的行为 |
|---|---|
| 失败的 submit 被当作结束意图；修复轮数空值可能无限重试；`patience=0` 在首个分数后提前结算 | 无 best 的失败提交留在同一 Plan 重试；新建假设默认最多 12 轮；零 patience 停用无改进提前停止，明确提交、放弃和轮数上限仍有效 |
| 已有可信 best 后提交，仍重新执行工作区并被“无新源码”拒绝 | submit 直接结算存档的分数、commit 和证据；未评分改动不能替换 best；continue 才执行并评估新改动 |
| 可信参考/best 未直接进入每轮提示，Agent 混用 OOF 与 SEARCH 分数；自动提交后的 diff 含义不清 | 每轮展示可信参考、历史 best 分数/commit；说明 OOF/CV 仅供诊断，平台在 continue 后自动评分和提交，后续 diff 相对最新 commit 检查 |
| 错误被截断或在修复开始前清除；缺失结构化决策静默丢失 | 持久化失败直到有效评分；阶段、命令、退出状态、脱敏错误尾部进入下一轮；无效/缺失决策有界重试；无 best 终结时保留根因 |
| 基础设施失败误交给候选修复；暂停/取消与 Agent 调度竞态；SEARCH 尚有工作就进入后续阶段 | 服务或冻结输入故障保留 Plan 并暂停；回收已运行结果，未启动轮次在明确 pause gate 拒绝时退回；取消可退出等待；活动 SEARCH Plan 阻止提前完成 |
| Agent 显式把共享环境根当作 Plan/EDA 工作目录，读错历史文件 | 每轮提供实际隔离目录；Plan 和 PREPARE EDA coordinator 的 shell workdir 限定在其工作区内；共享环境仍用于依赖，外部数据按绝对路径读取 |
| PREPARE 使用错误超时、静态报告被当作本轮输出、耗尽轮次后丢失错误 | 使用会话实验超时；提示 manifest 每次必须重新生成包括报告在内的全部声明输出；重试和终态保留有界根因与剩余预算 |
| 来源目录只返回主标题时，合法论文全标题被错误拒绝 | 精确匹配符合条件的冒号主标题变体；保留 DOI、年份、来源类型和引用门槛，证明中保留权威目录原始标题 |
| CSV 和评估契约对特殊列、ID、转义和分区覆盖处理不一致 | 增加输入预检、标准 CSV 探针、明确 label/prediction 列、严格 v2 必填检查、冻结前标签与特征 ID 覆盖检查；SEARCH/VALIDATE 按声明文件和 ID 列验证预测 |
| PowerShell 5.1 对象输出丢失；原生 stderr 警告导致包装脚本中断 | 流式输出对象文本；区分 native stderr 与真正 cmdlet 错误，保留多行警告后的完成输出、非零退出码和超时前日志 |
| GUI 重启遗漏只有草稿的会话；切换绑定、迟到澄清、设置读取和报告归档相互串扰 | 恢复当前 canonical 草稿/任务卡；先完成会话绑定再提交任务；版本/epoch 防护迟到响应；超时、容忍度按会话保存；报告归档按会话隔离 |
| 恢复 READY 卡片后输入文本，被错误送作新任务；已完成研究的任务卡仍显示“运行中” | 普通输入修改已有草稿并重新确认，独立任务使用新会话；卡片启动状态显示“已启动” |

实现主要位于 [supervisor](../../src/athena/research/supervisor/)、
[prediction_contract.py](../../src/athena/research/prediction_contract.py)、
[prepare](../../src/athena/research/prepare/)、[execution/runtime.py](../../src/athena/execution/runtime.py)、
[GUI gateway](../../src/gui_gateway/handler.py) 和 [usePipeline.ts](../../athena-gui/src/hooks/usePipeline.ts)。
当前规则见 [修复循环与数据错误边界](../loop_failure_modes_ch.md)。

## 真实数据集验证

数据集：`newdata/gui_demo_task/gui_demo_task`。入口为本地 Web GUI（1421）及 GUI gateway（17601），
没有重新打包桌面安装程序。训练 17,280 行，SEARCH 特征 5,760 行，目标 `fault`，主指标 macro F1，
ID 为 `unit_serial`；并行 1、实验超时 120 秒、容忍度 0.005，跳过 VALIDATE。

第一轮 `s-1792038464604-1` 的预算 4 已完整保留：前三个假设暴露错误 shell workdir，
第四个暴露零 patience 提前结算，因此仅作为诊断证据。
该轮曾人工修正一处未封存来源候选的 title 为权威目录返回的 `XGBoost`，原文件和前后摘要已保存；
没有修改可信分数、标签或评估器。跑测准备期间重建过任务工作区的依赖环境并恢复依赖；仓库自身环境及锁文件未因此重建。

用户明确批准第二轮独立预算 4。会话 `s-1792042489977-1` 最终结果如下，按实际执行顺序列出：

| 实验 | 最终存档的可信 SEARCH macro F1 | 解释 |
|---|---:|---|
| baseline | 0.7147502579080955 | 可信 PREPARE 基线，实际 `cuda:0` |
| hyp_ffd9f13940bd | 0.7348974079432975 | 偏差特征替换；当前 SOTA 指针 |
| hyp_9713e1f2c122 | 0.7387486544346207 | 阈值协议调整；本轮最高观测值 |
| hyp_d7bfa1cde95f | 0.7147502579080955 | 新序列实现执行失败，预算耗尽后保留早先与基线同分的可信版本 |
| hyp_5c148d1a71de | 0.7266565428349199 | 保留历史 best；最后 submit 未再次执行工作区 |

最高观测值比当前 SOTA 高约 0.00385，小于 0.005 容忍度，因此 GUI 的最佳值仍显示 0.7349。
四个假设的统计比较均为持平/无法判定；这里的数值变化不构成已证实的统计或泛化提升。

实际修复证据：`hyp_9713e1f2c122` 在同一工作区出现
`ModuleNotFoundError: No module named 'solution'`，Agent 修正本地导入后重新取得可信分数，
随后优化到 0.7387486544346207。第二轮没有人工编辑候选实现或来源候选文件。
相反，`hyp_d7bfa1cde95f` 的 XGBoost 分组长度错误被准确反馈了多次，模型仍未实际应用修复，
最终新实现没有得到分数。界面的“成功”表示有存档可信结果，不能解释为该假设的所有新实现均修复成功。

复验过程中为加载实跑发现的框架修正，暂停、重启并恢复了同一会话；SEARCH 预算没有重置或增加。
第一假设在第 4 轮附近、第四假设在第 2 轮后分别验证了同一 Plan、工作区、轮数与 best 恢复。
最终再次重启后，任务历史、五条实验记录和设置 4/1/120/0.005 均恢复。
这是保留失败历史并逐步修正后的真实运行，不是最终代码从零一次无人干预跑通的证明。
自动评分/commit 时序的最后一处提示澄清在运行结束后补充，以回归测试验证，未额外消耗 SEARCH 预算。

本次明确跳过 VALIDATE，没有独立 FINAL 分数；不能把本轮结果写成最终测试或泛化结果。

## 自动修复的边界

| 故障类别 | 能否由候选修复循环处理 |
|---|---|
| manifest、候选代码异常、超时、缺输出、预测格式/ID/行覆盖错误 | 能反馈到同一 Plan，让 Agent 在剩余轮数内修改并重跑；是否修好取决于模型实际行动 |
| 训练输入损坏、重复/空表头、行宽不符、空目标、空分区、配置目标与预分割文件不符 | PREPARE 预检阻止无效实验；需修正输入或配置，不让候选篡改原数据 |
| 冻结标签覆盖错误、明确列契约错误、不完整 v2 契约 | 在冻结/契约阶段拒绝并明确诊断；候选不能通过修改冻结评估器“修复” |
| Agent/执行/存储服务或冻结输入不可用 | 暂停并保留进度；恢复基础设施后再继续，不反复消耗代码修复轮数 |
| 模型只承诺修改、实现偏离假设、未接入入口的辅助源码 | 提示要求当轮行动、默认最多 12 轮；无法保证模型完成修复或正确实现任意假设 |

源码差异门禁能拒绝无变化或仅输出/文档变化，不能证明任意程序的语义变化。
shell workdir 限制是目录参数防误用，不是完整 shell 安全沙箱。预分割数据在明确配置目标列时检查目标语义，
不会从自由文本强猜目标列；非 CSV 输出继续依赖对应任务评估契约。旧 checkpoint 的显式轮数语义和人工无限续跑控制保留。

## 最终验证

- 后端研究、Agent、执行和 GUI gateway 范围：**1870 passed, 1 skipped, 1 warning**，303.74 秒。
  跳过项依赖 PowerShell 7 语法，本机为 5.1；警告为已出现过的 Windows asyncio 子进程析构时事件循环关闭。
- 前端：23 个文件、**250 tests passed**，另有 launcher 测试 1/1；生产构建通过。
- 33 个本次涉及的生产 Python 文件 Black 检查通过；规范硬规则通过，45 条已有 docstring 提示不阻断。
- `git diff --check` 通过。既有无关 TUI、文档删除和工作区改动均保留，没有合并整个上游分支。
- 真实脚本/冻结评估器回归覆盖：失败提交后同 Plan 修复成功；零 patience 下继续评分 0.5 → 1.0；
  再故意写入未评分的坏代码并 submit，结算仍为存档 1.0，执行次数严格为 2。
- 独立控制流复核确认：缺失决策、畸形输出、暂停空返回、基础设施失败均不能伪装为成功 submit。

后端复验命令：

```powershell
.venv/Scripts/python.exe -m pytest tests/unit/research tests/integration/research tests/unit/agent tests/unit/execution tests/test_gui_gateway_e2e.py tests/test_gui_gateway_handler.py -q
```

前端命令（`athena-gui`）：`npm test`、`npm run build`。

本地证据位于忽略目录 [submission/reproduction/2026-10-15-hypothesis-repair](../../submission/reproduction/2026-10-15-hypothesis-repair/)：
`verification.json`、两轮 state/tree 快照、来源原始备份、四个候选的实验执行日志，以及重启/终态截图。
终态研究树是可信分数来源，Agent 自写报告中的 OOF 或“尚未评分”描述不能替代它。
本计划的验收已完成；实现计划已移除，`codex_docs/CURRENT.md` 改为指向本报告，其他活动计划保留。
