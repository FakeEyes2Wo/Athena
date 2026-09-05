# SEARCH 可证伪性检查错误使用 API Key：故障分析与修复

## 前情提要

在 `question3_run_01` 的 SEARCH 阶段，两个 Ideator 均成功生成了具体、可执行的候选假设，例如移除高星体间方差特征、增加星内归一化特征、调整类别权重和使用排序目标等。然而 SEARCH 最终没有留下任何假设，也没有创建实验分支。

对应日志位于：

- `D:\WorkRoot\question3_run_01\.athena\logs\agents\ideator-1-1.jsonl`
- `D:\WorkRoot\question3_run_01\.athena\logs\agents\ideator-1-3.jsonl`

日志中的共同拒绝原因是 falsifiability checker 收到 401：

```text
Authentication Fails, Your api key: ****<key-A> is invalid
```

同一运行中的普通 Agent 能正常调用模型，实际使用的有效 DashScope key 后缀为 `<key-B>`。因此故障不是“没有配置 checker 专用 API”，也不是 Ideator 没有生成假设，而是 checker 没有复用正常 Agent 已经持有的运行时 LLM client。

## 根因

Athena 的 `ResearchRuntime` 已持有由启动层注入且验证可用的 `client`。普通 Agent 通过该 client 调用模型。

light ideation pipeline 原先只传递 `model` 和 `ArtifactStore`。下游的 `single_turn_structured_chat()` 支持接收 client，但由于调用链没有传入，参数保持为 `None`，最终执行 `create_provider(model, client=None)`。

这会触发第二套全局配置解析，通过 `settings.get_client()` 从进程环境和 `.env` 重新选取凭据。它与 runtime client 不具有同一性，也不保证使用相同 key。本次请求实际发送了日志所示的 `****<key-A>`，随后被 DashScope 拒绝；正常 Agent 使用的 `****<key-B>` 没有到达 checker。

## 故障调用链

```text
AgentTurnRunner._finish_ideator_batch
  -> run_light_pipeline                         （原先丢失 rt.client）
    -> _screen_and_review
      -> falsifiability_check                   （只传 model/artifacts）
        -> single_turn_structured_chat
          -> create_provider(model, client=None)
            -> settings.get_client              （重新解析另一套 key）
              -> DashScope 401，实际 key=****<key-A>
      -> except Exception
        -> degraded_falsifiability_report
          -> is_falsifiable=False
            -> pre_gate 拒绝候选
              -> 所有候选被删除
                -> SEARCH 无假设、无实验分支
```

401 还被现有 fail-closed 逻辑转换成了 `is_falsifiable=False`，所以界面表现为候选“不可证伪”，而不是直接显示 checker 的基础设施故障。这放大了客户端丢失的影响，也掩盖了真正原因。

## 修复思路

采用依赖注入链路的最小修复，不创建 checker 专用配置，也不改变 gate 的判断规则：

1. 从 `_finish_ideator_batch` 传入 `rt.client`。
2. `run_light_pipeline` 和 `_screen_and_review` 原样向下传递同一个 client。
3. falsifiability checker 和两个 review perspective 的 structured chat 均显式使用该 client。
4. 保留 `client=None` 默认值，使已有独立调用和测试保持兼容。

之所以同时修复 review 调用，是因为它们与 falsifiability checker 使用同一个 `single_turn_structured_chat` 边界；如果只修第一道检查，候选通过后仍会在 methodology/statistics review 中重新读取全局 key。

## 修改前后对比

修改前：

```python
run_light_pipeline(drafts, model=rt.model, artifacts=rt.store)
```

修改后：

```python
run_light_pipeline(
    drafts,
    model=rt.model,
    artifacts=rt.store,
    client=rt.client,
)
```

structured checker 和 methodology/statistics review 也由只传 `model`、`artifacts` 改为额外传递 `client=client`。

## 修改文件

| 文件 | 修改内容 |
|---|---|
| `src/athena/research/agent_turn_runner.py` | 将 `rt.client` 注入 light pipeline |
| `src/athena/research/idea_generation/gate.py` | 在 pipeline 和单候选 gate 中传递 client |
| `src/athena/research/idea_generation/pre_gate_checks.py` | falsifiability structured call 使用注入的 client |
| `src/athena/research/idea_generation/review_board.py` | 两个 review structured call 使用注入的 client |
| `test/unit/idea_generation/test_light_pipeline.py` | 验证三次 LLM 检查使用同一个注入 client |
| `test/unit/idea_generation/test_gate_retry.py` | 验证 runtime client 到达 gate pipeline |

## 回归测试覆盖

新增测试覆盖两个关键边界：

1. `_finish_ideator_batch` 必须把 `runtime.client` 传给 `run_light_pipeline`。
2. 一条候选经过 falsifiability、methodology review、statistics review 时，三次调用必须收到同一个 client 对象。

测试在生产代码修改前分别以“缺少 `client` 参数”和“未收到 `client`”失败；完成最小修改后通过。这能够防止未来重构再次退回到隐式读取全局 API key 的行为。

## 范围说明

本次修复不修改 API key 内容、不改变 `.env`、不改变模型或 gate 阈值，也不自动恢复已经被拒绝的历史候选。继续当前流程或从 SEARCH 断点恢复时，新产生/重新进入 gate 的候选将使用 runtime 中的有效 client。
