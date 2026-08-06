# Athena Ideator 多 Agent 辩论设计

> 日期：2026-07-31
> 状态：已确认，待实施规划
> 范围：以 `athena.ideator` 取代 `athena.brainstorm`，并将 IdeaGeneration 改为多 Agent 辩论

## 一、所求

今之 `athena.brainstorm` 仅作一轮假说生成，虽能取结构化结果，未有独立 Agent、互评、修案与裁决。此改造当成以下诸事：

1. 旧 `athena.brainstorm` 尽迁尽删，不留兼容重导出。
2. 新立 `athena.ideator`，为 IdeaGeneration 唯一假说生成入口。
3. 辩者人数可配置，至少二人；诸辩者皆为独立 Agent Thread。
4. 裁决者恒一，不因失败而另生第二裁决者。
5. 全量注入 `ResearchTree` 所有假说、实验、指标、状态、谱系、SOTA、失败记录及 artifact 引用。
6. 返最终假说，亦返立论、驳议、修案、裁决理由及失败记录，以资审计与后续 IDE 展示。

## 二、总制

取“乙为骨、甲为法”之制：

```text
RuntimeThreadManager
    司独立 Thread、连续 Turn、上下文、等待、中断与事件
                    |
                    v
Ideator
    司立论、互评、修案、法定人数与唯一裁决
                    |
                    v
DebateResult -> IdeaGeneration -> ResearchTree
```

通用 runtime 不知辩论之义；`Ideator` 不自造 Agent 内核。此与 Codex 相近：每一子 Agent 各有 Thread，父级编排其任务，待其结果而汇总。

## 三、简约边界

对外仅有三成员：

```python
Ideator
IdeatorConfig
DebateResult
```

公开调用唯此：

```python
result = await ideator.generate(profile, papers, models, tree)
```

目录仅置二实质文件：

```text
src/athena/ideator/
├── __init__.py   仅重导出三公开成员
├── types.py      配置、最终结果及私有结构输出模型
└── ideator.py    上下文组装、辩论编排、校验与裁决
```

不公开 `IdeationRequest`、`AgentPool`、`ResearchSnapshot`、Proposal、Review 或 Revision 等中间类型。中间结构仍以私有 Pydantic 模型校验，免使调用者负其心智之重。

## 四、数据

### 四之一 研究上下文

`Ideator.generate()` 于一轮之始调用 `ResearchTree.to_dict()`，所得为本轮冻结快照。其含：

- 全部假说之陈述、干预、预期效果、状态、来源与证据引用；
- 全部实验之父子关系、计划、状态、指标、裁决、提交与产物引用；
- 当前 SOTA、根实验及可由父子字段复原之路径；
- 失败与取消实验，不得隐去。

所谓全量注入，乃全量注入结构化记录及 artifact 引用；日志、diff、逐样本结果等大产物不逐一解引用。诸辩者与裁决者皆据同一快照，辩论期间树之变化不入本轮。

### 四之二 配置

`IdeatorConfig` 仅载：

```python
debater_count: int = 3
quorum: int = 2
stage_timeout_seconds: float = 120.0
max_hypotheses: int = 5
```

其约束为 `debater_count >= 2`、`2 <= quorum <= debater_count`、`3 <= max_hypotheses <= 5`，时限须为正数。

### 四之三 结果

`DebateResult` 仅载：

```python
hypotheses: list[Hypothesis]
transcript: list[dict]
failures: list[dict]
artifact_ref: ArtifactRef
```

`transcript` 依时序记录 Agent、阶段、论稿、所评对象、来源、所据实验及裁决理由。每一最终假说皆可由记录反溯至修案、驳议与初案。

## 五、运行时序

一轮辩论依次为：

1. 取数据画像、论文、模型及 `ResearchTree.to_dict()`，合成同一研究上下文。
2. 依 `debater_count` 并发建立辩者 Thread；每 Thread 绑定独立 Agent 与独立 memory。
3. 并发提交“立论”Turn；每位辩者提出三至五项有来源、可证伪之案。
4. `Ideator` 匿名汇总诸案，将他人之案投予各辩者，再并发提交“互评”Turn；辩者不得评己案。
5. 将批评送回原作者，在同一 Thread 并发提交“修案”Turn。
6. 每阶段皆验法定人数。三阶段俱成后，始建立唯一裁决者 Thread。
7. 裁决者取得研究上下文、初案、驳议、修案及失败记录，选并三至五项最终 `Hypothesis`，逐项陈取舍之由。
8. 将完整辩论记录存入 `ArtifactStore`，所得引用置于 `DebateResult.artifact_ref`。
9. `IdeaGeneration` 仅登记 `DebateResult.hypotheses`。诸假说先全量校验并预置唯一 ID，再依现有 `ResearchTree.add_hypothesis()` 登记，不为此另增树之公开方法。

辩者不得任意互投消息；中间论稿悉由 `Ideator` 转发。如此可守轮次、匿名、公平与可复现，而各推理仍出自独立 Agent Thread。

## 六、Runtime 最小改动

既有 `RuntimeThreadManager` 已具 `start()`、`submit()`、`interrupt()`、`events()` 与 `aclose()`，且同一 Thread 可承连续 Turn。今仅补一项公开能力：

```python
async def wait_turn(self, thread_id: str, turn_id: str) -> ArtifactRef: ...
```

其候终态；成功则返该 Turn 的 `result_ref`，失败或中断则抛明定异常。`ThreadHandle` 内可有同名私有协助法，然不另造 Agent 管理门面。

每轮 Ideator 使用其自有 `RuntimeThreadManager` 实例，终局以 `aclose()` 收诸 Thread。其 runner 于每个新 Thread 首 Turn 时私下建立对应 Agent，后续 Turn 复用同一 Agent 与 memory。因而无需新增 `spawn_agent`、`send_message`、`followup_task`、`list_agents`、`close_agent` 等表面 API。

## 七、错误与降级

- 辩者某阶段超时、异常或结构不合，记入 `failures`，并自存活者中除之。
- 每阶段存活者不少于 `quorum`，则降级续行；不足则抛 `RuntimeError("ideator quorum not met")`。
- 辩者前轮虽成、后轮若败，其旧稿仍存档，然不入最终候选。
- 裁决者超时、异常或结构不合，抛 `RuntimeError("ideator judge failed")`；不另起裁决者。
- 不保留模板假说降级。未成真实辩论，不得伪报成功。
- 外部取消时，中断所有活跃 Turn，关闭 manager，而后续抛 `asyncio.CancelledError`；取消之轮不产成功 artifact。
- 最终假说在登记前悉数校验可证伪性、来源、数量与唯一 ID；生产工作流串行登记，免引入新事务抽象。

## 八、旧码迁移

- `athena.brainstorm.types` 中仍需之结构校验移入 `athena.ideator.types` 私有模型。
- `validate_falsifiable` 与 `check_falsifiability` 之逻辑归入 `athena.ideator`，只保留一个规范实现。
- `template_hypotheses`、`single_turn`、`BrainStormResult`、`HypothesisInput` 及文本客户端兼容路径皆删除。
- `workflows/search/idea_generation.py` 改调 `Ideator.generate()`，并保存辩论 artifact 引用。
- 生产、测试、示例与文档中的 `athena.brainstorm` 导入悉改毕后，删除 `src/athena/brainstorm/`。
- 不留旧路径重导出，不设弃用期。

## 九、测试

### 九之一 领域测试

- 配置拒绝少于二辩者、非法法定人数、非法时限与假说数量。
- 研究上下文与 `ResearchTree.to_dict()` 等值，故所有假说、实验、指标、谱系、失败与 artifact 引用悉在。
- 最终假说拒绝无来源、不可证伪、重复及数量越界者。

### 九之二 编排测试

- 以异步屏障证明同阶段辩者确为并发，而非逐一等待。
- 证明各辩者 Thread ID 相异，同一辩者三阶段 Thread ID 不变。
- 证明诸辩者所得研究快照相同，互评时看他案而不见作者身份。
- 证明裁决者恰生一次，且得全量初案、驳议、修案与失败记录。
- 证明最终 `DebateResult` 可反溯每项假说之来源。

### 九之三 故障测试

- 一辩者失败而仍达法定人数者可降级成功。
- 少于二人或少于配置法定人数者必抛稳定讯息之 `RuntimeError`。
- 裁决失败必抛稳定讯息之 `RuntimeError`，且无模板结果。
- 外部取消必传播至所有活跃 Turn，并无遗留任务。

### 九之四 集成与迁移测试

- `RuntimeThreadManager.wait_turn()` 覆盖成功、失败、中断及竞争终态。
- `IdeaGeneration` 仅登记裁决所得假说，并留完整辩论 artifact。
- 静态扫描证明 `athena.brainstorm` 导入为零，旧目录不存。
- 运行关联 Python 测试、全量 Python 测试与 `git diff --check`。

## 十、非目标

- 此轮不造 IDE 辩论界面；后续可据事件与 artifact 展示，而毋改领域结果。
- 不造通用辩论图引擎、角色注册表、第二套 Agent runtime 或任意 Agent 直连网络。
- 不改排名、预算、Supervisor、Evaluator 或实验执行策略。
- 不解引用全量大 artifact 内容入模型上下文。
- 不为旧 `athena.brainstorm` 保兼容门面。

## 十一、完成之验

1. `athena.ideator` 为唯一假说生成 owner，公开成员仅三。
2. 配置数目的辩者以独立 Thread 并发立论、互评、修案，唯一裁决者定案。
3. 所有 Agent 得同一全量 ResearchTree 结构快照。
4. 法定人数、超时、取消、结构错误与裁决失败皆有确定行为。
5. 完整辩论可审计，最终假说可溯源，且无模板伪成功。
6. `athena.brainstorm` 目录及一切导入尽删。
7. 关联与全量测试通过，且无无关改动混入。
