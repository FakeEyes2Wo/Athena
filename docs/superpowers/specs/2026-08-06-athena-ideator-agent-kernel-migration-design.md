# Athena Ideator 统一 AgentKernel 迁移设计

> 日期：2026-08-06
> 状态：已确认
> 范围：以统一 AgentKernel 承载 Ideator 多 Agent 辩论，并补齐有界互评与生产科学输入

## 一、目标

当前 `athena.ideator` 已实现 proposal、review、revision、judge、quorum、lineage 与
audit，但仍以 `RuntimeThreadManager` 和 `_DebateRunner` 管理独立 Agent、history 与
关闭过程。这使 Ideator 成为第二个生命周期 owner，未达到统一 AgentKernel 设计所定的
单轨目标。

本迁移必须同时完成：

1. Ideator 只经共享 `AgentControl` 操作 Agent，不再创建私有 runtime。
2. 新增薄 `AgentTeam`，只封装成员并发、quorum、唯一 arbiter 与自有 Agent 清理。
3. 辩者人数仍可配置；arbiter 恒为一个，且低于 quorum 时不创建。
4. 匿名互评由全量 all-to-all 改为正常路径线性有界的确定性分配。
5. 生产 SEARCH 同时注入论文、模型、数据画像与完整 ResearchTree 冻结快照。
6. 以结构化字段确定性约束单一干预、测量指标、方向与最小效果。
7. 删除 Ideator 对旧 Thread/Turn runtime 的所有引用，不留开关、shim 或双写。

## 二、边界与所有权

```text
Composition Root
  ├─ AgentKernel / AgentControl      唯一 Agent 生命周期所有者
  ├─ workflow parent AgentHandle     工作流授权边界
  └─ Ideator
       └─ AgentTeam                  仅拥有本轮成员与 arbiter
```

composition root 启动 Kernel、持有共享 `AgentControl`，并把工作流父 Handle 交给
Ideator。Ideator 不获得 Kernel 内部对象，也不拥有 Kernel 的最终关闭权。

`AgentTeam` 创建的普通成员与 arbiter 均为该父 Handle 的直属子 Agent。Team 结束时只
中断或关闭自己创建的 Handle；不得调用 `AgentControl.aclose()`，不得关闭父 Handle，
不得影响同一 Kernel 中的其他工作流。

若部分成员创建失败，Team 先回收本次已经创建的成员，再把稳定失败结果交给 Ideator。
不存在“自建 Kernel”或“无父 Handle”第二种构造路径。

## 三、AgentTeam

新增：

```text
src/athena/core/agent_kernel/team.py
test/unit/agent_kernel/test_team.py
```

`AgentTeam` 进入 `athena.core.agent_kernel` 公开面，但不进入 `athena.ideator` 公开面。
构造输入为：

```python
AgentTeam(
    *,
    control: AgentControl,
    parent: AgentHandle,
    member_specs: Sequence[AgentSpec],
    arbiter_spec: AgentSpec,
    quorum: int,
    name: str,
)
```

单数 `arbiter_spec` 从类型与构造面保证 arbiter 恰为一个。构造时校验普通成员非空，且
`1 <= quorum <= len(member_specs)`。Ideator 自身仍以 `IdeatorConfig` 施加更严格的
`debater_count >= 2` 与 `quorum >= 2` 领域约束。

Team 只提供三项异步行为：

```python
await team.run_members(tasks, timeout=...)
await team.arbitrate(task, timeout=...)
await team.aclose()
```

`run_members()` 首次调用为尚未创建的成员执行 `spawn()`；后续调用对已有成员执行
`followup()`。调用者以稳定成员序号提供本阶段任务，Team 并发启动和等待这些 Run，并按
相同序号返回成功值或脱敏异常。Team 不解释任务内容，不自行淘汰成员，也不决定是否达到
领域 quorum。

`arbitrate()` 惰性创建 arbiter，且实例生命周期内只允许调用一次；第二次调用以
`INVALID_REQUEST` 拒绝。它不读取成员结果，裁决所需记录必须由 Ideator 显式放入任务。

`aclose()` 幂等。它取消仍在运行的自有 Run，等待其终态，再关闭普通成员与 arbiter。
调用者取消 `aclose()` 不得取消共享关闭过程；错误对并发等待者一致传播。

Team 不拥有以下内容：

- proposal、review、revision、judge 阶段次序；
- 匿名 alias、审查分配、candidate key 与 lineage；
- 假说去重、可证伪性验证与 ResearchTree；
- transcript、failure 或 audit schema；
- Kernel registry、scheduler、store、serializer 或全局关闭。

## 四、Ideator 构造与 Runner

`athena.ideator` 的公开导出保持：

```text
Ideator
IdeatorConfig
DebateResult
```

`Ideator` 构造改为：

```python
Ideator(
    *,
    control: AgentControl,
    parent: AgentHandle,
    agent_factory: Callable[[str, int], StructuredAgent],
    artifacts: ArtifactStore,
    config: IdeatorConfig | None = None,
)
```

`agent_factory` 的既有角色和序号语义保留，以减少模型接线改动。Ideator 为每个辩者及
arbiter 创建一个独立、无共享字典的 Runner，并包装成 `AgentSpec`。每个 Runner 只绑定
自己的结构化模型对象和 ArtifactStore；跨 Run 对话历史唯一取自对应
`AgentSession.memory`。

删除 `_DebateRunner`、`_agents`、`_bindings`、`_histories`、`put_request()`、
`read_result()`、动态 `__call__()` 与 `run_with_context()` 双签名。新 Runner 只实现：

```python
async def run(request_ref, *, session, emit) -> ArtifactRef
```

Runner 从 artifact 读取 `_TurnRequest`，以 `session.memory.items` 作为模型
`message_history`，通过受 generation 门禁的 `session.append_message()` 写入本次
ModelRequest 与 ModelResponse，持久化结构化输出并返回结果引用。

## 五、ArtifactRef Codec

`AgentCodec` 当前为同步协议，而 `ArtifactStore` 为异步协议。此迁移不扩大 Kernel codec
契约，也不把请求正文塞入 GraphStore。

新增无状态私有 `_ArtifactRefCodec`：

```text
encode_request(ref)  -> ref
decode_request(ref)  -> ref
encode_response(ref) -> ref
decode_response(ref) -> ref
```

Ideator 在提交 Run 前异步保存 `_TurnRequest`；Runner 异步读取请求、保存结果；Ideator
等待 Run 后再异步读取结果，并按阶段对应的 Pydantic schema 验证。GraphStore 始终只见
短 ArtifactRef，不持有 prompt、模型结果或额外缓存。

## 六、辩论数据流

一轮执行如下：

1. 恰调用一次 `ResearchTree.to_dict()`，连同 DataProfile、论文与模型引用形成冻结输入。
2. 将冻结输入保存为 artifact；创建配置数量的辩者 spec 与一个 arbiter spec。
3. 创建 `AgentTeam`，并发执行所有辩者 proposal 首 Run。
4. 检查 proposal quorum；不足则关闭 Team 并失败，arbiter 不创建。
5. 为 proposal 存活者生成确定性匿名审查分配，并发执行 review followup。
6. 验证 alias 覆盖；处理一次有界故障重派，再检查 review quorum。
7. 把有效批评送回原作者，执行 revision followup，再检查 revision quorum。
8. 将完整冻结输入、提案、批评、修案、失败与 lineage 写入唯一 arbiter 任务。
9. 调用一次 `team.arbitrate()`，验证全部裁决覆盖与最终假说结构。
10. 保存版本化 audit；把 audit ref 写入每个最终 Hypothesis 的 `evidence_refs`。
11. 返回 `DebateResult`，最终由 IdeaGeneration 原样登记进 ResearchTree。
12. 无论成功、失败或取消，均只关闭本轮 Team。

各阶段继续使用同一辩者 Handle。review 与 revision 必须是原 Agent 的 `followup()`，不得
关闭后重生同名 Agent，不得复制或旁存 history。

## 七、有界匿名互评

正常路径采用稳定环形分配。对排序后的存活辩者：

```text
reviewer[i] 审 author[(i - 1) mod N]
```

因此每位作者的全部候选恰交给一名非自身 reviewer，每个 reviewer 正常只审一位作者的
候选，工作量为 `N * H`。模型仅见本轮临时 alias，例如 `candidate-1`；prompt 不含作者、
Agent 序号或内部 `idea-{author}-{ordinal}` key。Ideator 在结果返回后才把 alias 映回内部
key。

若某 reviewer 失败，其尚未覆盖的作者候选至多重派一次给排序中的下一名存活非作者
reviewer。重派不得交回原失败者，不得交给候选作者，也不得无限递归。重派仍失败或找不到
合法 reviewer 时，该作者不进入 revision；若剩余可修案作者少于 quorum，整轮失败。

正常复杂度为 `O(NH)`；单次故障重派使最坏审查量不超过 `2NH`。

## 八、结构化科学约束

私有 draft schema 不再只以自然语言描述“可证伪”。proposal、revision 与 judge draft
统一包含：

```python
statement: NonBlankText
interventions: list[NonBlankText]  # min_length=1, max_length=1
metric: NonBlankText
direction: Literal["increase", "decrease"]
minimum_effect: float              # gt=0，且须为有限数
sources: list[NonBlankText]        # min_length=1
candidate_keys: list[str]          # 仅 revision/judge 按阶段要求
```

最终 `Hypothesis.intervention` 取唯一 intervention；`expected_effect` 由 metric、direction 与
minimum_effect 规范化生成，而非接受任意空泛文本。Ideator 继续验证 statement 唯一、来源
非空、candidate coverage、disposition 与 lineage；Pydantic 拒绝 NaN、Infinity、空白来源
和多重 intervention。

此结构保证实验假说至少具备一个可控制改动、一个测量指标、明确方向与正的最小效果；不以
字符串启发式声称理解任意自然语言的科学真伪。

## 九、生产输入

`SearchLoop` 增加可注入的 `reference_search: PaperSearch | None`，默认仍构造
`PaperSearch`。当无 pending hypothesis 时，以同一稳定查询并发执行：

```python
papers, models = await asyncio.gather(
    reference_search.search(query),
    reference_search.search_models(query),
)
```

两类结果原样传入 `generate_hypotheses()`。检索器返回空列表是合法输入；检索调用抛出的
异常不得静默改为空列表，以免把“检索失败”伪装成“无证据”。

所有辩者与 arbiter 均得到同一版本的：

- DataProfile；
- PaperRef 与 HFModelRef；
- 全部 hypotheses 与 experiments；
- 指标、状态、失败、取消、父子谱系、root、SOTA；
- ArtifactRef，但不自动解引用大日志、diff 或逐样本结果。

## 十、Audit Schema

新增私有、严格、版本化 Pydantic 模型：

```text
_TranscriptEntry
_DebateFailure
_HypothesisLineage
_DebateAudit
```

`_DebateAudit` 固定包含 `schema_version=1` 与
`codec_id="athena.ideator.debate.v1"`，并设 `extra="forbid"`。公开
`DebateResult.transcript/failures` 暂保持既有 JSON 兼容形状，但内容必须由上述私有模型
导出，不再手写自由字典。

失败记录只含 member、stage 与稳定异常类型；不得写 prompt、traceback、credential、
provider 原始响应或未经授权的 artifact 内容。

## 十一、错误、超时与取消

- 每个成员 Run 独立计时；超时只取消该精确 Run，并记录 `TimeoutError`。
- 每阶段在结构校验与一次重派完成后检查 quorum。
- 低于 quorum 时不创建、不调用 arbiter，抛稳定 `RuntimeError("ideator quorum not met")`。
- arbiter 失败统一抛 `RuntimeError("ideator judge failed")`，不重试，不生成第二 arbiter，
  不使用模板结果。
- 外层取消时，Team 取消所有在飞 Run，等待终态并关闭自有 Handle，然后重新抛出
  `asyncio.CancelledError`。
- Artifact 写入或阶段 schema 校验失败按该成员阶段失败处理；最终 audit 写入失败则整轮
  失败，不返回未审计结果。
- Team 创建过程部分失败时回收已创建 Handle；清理错误不得掩盖原始业务失败。

## 十二、测试迁移

### AgentTeam 单元测试

- 拒绝零成员、非法 quorum；单数 arbiter 构造面不可产生零或多个 arbiter。
- 首轮 spawn、后续 followup 与成员序号稳定。
- 成员并发、部分失败、timeout 与返回顺序。
- 低层 `arbitrate()` 最多调用一次。
- 部分创建失败回收成功成员。
- `aclose()` 幂等，只关闭自有 Handle，从不调用共享 `AgentControl.aclose()`。
- 外层取消后无遗留 Run 或等待任务。

### Ideator 领域测试

- 保留公开导出、配置与 DebateResult 契约。
- proposal 并发；每个辩者跨三阶段保持同一 Agent 与 memory。
- 所有 Agent 获得完全相同的冻结研究输入，且 tree 只快照一次。
- 五辩者时每项候选正常恰受一次非作者匿名审查，总审查量为 `NH`。
- reviewer 失败时只重派一次；无法覆盖时正确影响 revision quorum。
- proposal、review、revision 任一阶段低于 quorum 时 arbiter 调用次数为零。
- 达到 quorum 后 arbiter 恰调用一次。
- 最终 candidate coverage、disposition、lineage、单一 intervention、有限正效果与来源校验。
- audit 不自引用，最终 hypothesis 均链接同一 audit ref。
- timeout、外层取消及 judge 失败不产生模板结果或残留 Agent。

删除直接导入 `_DebateRunner`、`RuntimeThreadManager`、`AthenaTurn` 的测试，以及锁定旧
manager 错误字符串、Thread ID、私有 history 字典和 `run_with_context` 双签名的断言。

### Search 集成测试

- 同一查询各调用一次 `search()` 与 `search_models()`，并证明二者并发。
- papers/models 原样传入 Ideator。
- 已有 pending hypotheses 时不进行检索。
- 缺 Ideator 时在检索前显式失败。

## 十三、实施范围

预计修改：

```text
src/athena/core/agent_kernel/team.py
src/athena/core/agent_kernel/__init__.py
src/athena/ideator/ideator.py
src/athena/ideator/types.py
src/athena/workflows/search/search_loop.py
examples/trial_run.py
examples/ai4ml_pipeline.py
test/unit/agent_kernel/test_team.py
tests/test_ideator.py
tests/test_search_workflow.py
相关架构文档
```

不修改 ranking、budget、Supervisor、Evaluator、实验执行策略、ResearchTree schema 或
app-server 协议。旧 app-server runtime 的迁移属于统一 Kernel 总计划的另一实施单元；本
单元只保证 Ideator 不再是旧 runtime 消费者。

## 十四、完成定义

1. `src/athena/ideator` 与其测试对旧 Thread/Turn runtime 零引用。
2. Ideator 所有 Agent 只经注入的共享 AgentControl 创建、继续、取消及关闭。
3. 普通成员数可配置；arbiter 恰为一个、只调用一次且不计入 quorum。
4. Team 只拥有通用并发与生命周期动作，全部辩论领域规则仍由 Ideator 所有。
5. 匿名互评正常为 `O(NH)`，故障重派有一次上限。
6. profile、papers、models 与完整 ResearchTree 快照注入每个 Agent。
7. 最终假说具单一干预、指标、方向、有限正效果、来源与完整 lineage。
8. 结束或取消只关闭本轮 Team，不关闭共享 Kernel 或父 Agent。
9. 无兼容 runtime、feature flag、双写、模板 fallback 或无界重试。
10. AgentKernel、Ideator/Search 定向测试、Python 全量测试、Black 与
    `git diff --check` 全部通过。
