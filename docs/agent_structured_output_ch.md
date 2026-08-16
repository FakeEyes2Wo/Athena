# 结构化输出与工具调用

Status: current
Owner: Athena maintainers
Last verified: 2026-08-16
Source of truth: `src/athena/core/agent/provider.py`, `src/athena/core/agent/runtime.py`,
`test/unit/test_agent.py`

Athena 的每个业务 Agent 都带 `output_type`：evaluator/prepare 返回 `PlanDecision`，
Ideator 返回 `IdeatorHypothesisBatch`，Supervisor 返回 `SupervisorAnswer`。同时它们
也都带工具——`read_file`、`write_file`、`shell_command`，Ideator 还有 `paper_*`。

**这两件事在同一次请求里互斥。** 本文说明为什么，以及 Athena 怎么同时拿到它们。

## 一、`response_format` 会让模型发不出 tool call

OpenAI 兼容端点的 `response_format={"type": "json_schema", ...}` 把**整条回复**约束
成 schema。回复既然必须是那段 JSON，模型就没有位置再放 tool call——它不是"倾向于不
调工具"，是结构上不可能。

改造前 `ResponsesProvider.stream` 只要 `output_type is not None` 就挂 `response_format`，
与工具是否存在无关。结果是每个 Agent 都变成"只会写 JSON 说自己要干什么"的空转。

真机复现（qwen3.7-plus，evaluator 的真实 system prompt，`generic_tool_registry` 的真实
工具定义，各 3 次）：

| 请求 | `finish_reason` | tool calls |
|---|---|---|
| `tools` + `response_format` | `stop` ×3 | 无 ×3 |
| 仅 `tools` | `tool_calls` ×3 | `shell_command` ×3 |

在 loop 里的表现是 evaluator 连着 12 轮返回同一句话、一次工具都不调，PREPARE 报
`evaluator turn budget exhausted without a frozen evaluator`：

```
{"decision": "continue", "reason": "I need to examine the dataset first.
 Let me read the training CSV file to understand its structure.", "suggestions": []}
```

它说"让我读一下 CSV"，但从未发出 `read_file`。

## 二、规则：有工具就把 schema 放进 prompt

```python
tool_defs = [spec.to_openai_tool() for spec in tools.specs]
if output_type is not None and (tool_defs or self.provider_kind == "deepseek"):
    api_msgs = [*api_msgs, _schema_instruction(output_type)]
...
if output_type is not None and not tool_defs:
    kw["response_format"] = self._response_format(output_type)
```

- **有工具**：不发 `response_format`，改把完整 JSON Schema 作为一条 system 消息追加到
  对话末尾（`_schema_instruction`）。ReAct 循环照常用工具，末轮自行返回 JSON，由
  `Agent.run` 校验。
- **无工具**：仍用严格 `response_format`（openai 走 `json_schema`，deepseek 走
  `json_object`），因为此时没有什么可以被它挤掉。

`_schema_instruction` 不是新写的——DeepSeek 的 `json_object` 模式本来就不把 schema 传给
模型，一直靠它注入。改造只是把这条既有路径的适用范围从"deepseek"扩到"任何带工具的
请求"。

### 为什么不靠 `tool_choice="required"`

强制调工具解决不了问题：Agent 的最后一轮**必须**返回纯文本 JSON 才能结束 ReAct 循环，
`required` 会让它永远结束不了。真正的约束是"过程中能调工具、末轮返回 JSON"，只有
prompt 注入能同时满足。

### 代价：末轮不再有 schema 强制

去掉 `response_format` 后模型可能返回不合 schema 的文本。`Agent.run` 原有的
`_MAX_STRUCTURED_RETRIES = 3` 重试链负责兜底，把校验错误作为反馈发回。实测这条链够用
——但前提是第三节。

## 三、markdown 围栏是格式噪声，不该烧重试预算

去掉 `response_format` 之后，模型在带工具的对话里习惯把终态 JSON 包进代码块：

````
```json
{"decision":"submit","reason":"All required files are present …"}
```
````

`model_validate_json` 对此直接失败（`Invalid JSON: expected value at line 1 column 1`）。
真机上这一条足以打死整个 SEARCH：Ideator 连着三次返回围栏 JSON，重试预算耗尽后抛
`structured output invalid after retries`，该 lane 整条作废。

`runtime._unfenced` 在校验前剥掉围栏：

```python
_FENCED_JSON = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)

def _unfenced(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    match = _FENCED_JSON.search(stripped)
    return match.group(1) if match else text
```

边界刻意收紧：**只有整段文本以 ` ``` ` 开头才处理**。不去正文里搜"哪一段看起来像
JSON"——那等于替模型猜意图，会把真正的乱答伪装成合法输出。前后带散文的回复仍然判为
无效并走重试。

修复后同一个 evaluator 从 94 个事件降到 29 个，PREPARE 一次通过。

## 四、这两条改动各自的回归测试

| 用例 | 钉住的行为 |
|---|---|
| `test_an_agent_with_tools_keeps_them_instead_of_the_strict_schema` | 有工具时不发 `response_format`，且 schema 出现在末尾 system 消息里 |
| `test_deepseek_with_tools_also_drops_the_json_object_constraint` | deepseek 同样让位给工具 |
| `test_stream_sets_response_format_when_output_type_given` | 无工具时严格 schema 不变（既有用例，空 registry） |
| `test_a_fenced_json_answer_is_accepted_on_the_first_try` | ` ```json ` / ` ``` ` / CRLF 三种围栏都一次通过，`calls == 1` |
| `test_prose_around_the_fence_still_counts_as_invalid` | 只剥围栏、不猜正文；乱答仍然重试并最终报错 |

## 五、给新 provider 实现者

接一个新 provider 时，`stream` 必须保持这条不变量：

> 当 `tools.specs` 非空时，不得向端点发送任何把整条回复约束成固定结构的参数。

不同厂商的名字不一样（`response_format`、`response_schema`、`guided_json`、
`grammar`），但性质相同。schema 的传达一律走 `_schema_instruction`。

## 相关文档

- [Prompt 驱动 Agents 设计](architecture/2026-08-09-prompt-driven-agents-design.md) —
  两层 Agent 结构与 `output_type` 的来历
- [真机跑测暴露的 loop 失效模式](loop_failure_modes_ch.md) — 同一轮跑测里其余四个缺陷
