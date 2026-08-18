# Task Understanding 多轮选择题澄清机制设计

日期：2026-08-18
状态：approved design

## 1. 目标

让任务理解阶段由 Agent 事无巨细地收集完成当前 task 所需的信息，并通过前端弹窗让用户选择 1 / 2 / 3。澄清结果不扩展现有 TaskUnderstanding 字段，而是写入 `TASK_CLARIFICATION.md` 并注册到现有 `state.handoff_refs`，作为全局记忆供后续所有阶段使用。

## 2. 现状

现有链路：

- 前端 `sendMessage` → `parse_intent` → 一次 LLM 调用 → `TaskUnderstanding` → `IntentPreviewCard` → 用户确认启动。
- 运行中已有 `request_user_input` 工具 + `HumanRequestBroker` + `human_pending / human_reply` + `HumanRequestDialog`，但只支持自由文本问答。

## 3. 设计原则

- 复用现有 HumanRequestBroker / request_user_input / human_pending / human_reply / HumanRequestDialog。
- 只扩展“问题”为“选择题”，不新建第二套交互框架。
- 启动前和运行中走同一套弹窗机制。
- 澄清结果写入 Handoff / 全局记忆，不扩展 TaskUnderstanding 字段。
- Agent 自主决定何时结束，但受最大追问数硬上限和每题超时约束。

## 4. 后端：选择题型 Human Request

`src/gui_gateway/human.py` 中 `HumanRequestBroker` 的 pending 条目从 `(prompt, future)` 扩展为：

```json
{
  "request_id": "...",
  "prompt": "主指标应该是什么？",
  "choices": [
    {"label": "accuracy", "value": "accuracy"},
    {"label": "f1", "value": "f1"},
    {"label": "AUC", "value": "auc"}
  ],
  "allow_custom": true,
  "allow_skip": true
}
```

`ask()` 扩展签名：

```python
async def ask(
    prompt: str,
    *,
    choices: list[dict] | None = None,
    allow_custom: bool = True,
    allow_skip: bool = True,
    timeout_s: float = 120.0,
) -> str | None
```

`reply()` 接受并归一化：

- `choice` → `"choice:<value>"`
- `answer` → `"text:<answer>"`
- `skip` → `"skip"`

向后兼容：无 choices 的请求仍返回旧结构 `{request_id, prompt}`。

## 5. Agent 工具：request_user_input 支持选择题

`src/athena/core/agent/tools/user_input.py` 的 input schema 增加：

```json
{
  "prompt": "string",
  "choices": [{"label": "string", "value": "string"}],
  "allow_custom": true,
  "allow_skip": true
}
```

`AskUser` 回调类型扩展为：

```python
(prompt, choices, allow_custom, allow_skip) -> answer
```

Supervisor 运行逻辑不变，只是可以传入 choices。

## 6. 启动前：parse_intent 多轮澄清 Loop

`src/athena/gui/service.py::parse_intent` 从一次 LLM JSON 调用改为多轮 loop：

```text
while not done and questions_asked < CLARIFY_MAX_QUESTIONS:
    1. LLM 根据 task + 已收集答案，输出：
       - final TaskUnderstanding JSON，或
       - clarification question + 2~3 choices
    2. question → broker.ask(...) 阻塞等待回答，并把 Q&A 追加进对话历史
    3. final → 结束循环
```

规则：

- 必问项由 system prompt 固定，Agent 可自主追加问题。
- `CLARIFY_MAX_QUESTIONS = 8` 硬上限。
- 每题超时 120s，超时按 skip 处理。
- LLM 调用失败时回退现有 `_heuristic_understanding`。
- 澄清结束后写 `TASK_CLARIFICATION.md`，内容包含原始 task、全部 Q&A、最终 TaskUnderstanding、未解决项。

## 7. 启动后：Supervisor 继续追问

`src/athena/core/agent/prompts/supervisor_agent.md` 增加：

```markdown
During PREPARE/SEARCH, when a critical fact is unknown:
- call request_user_input with choices=[...] whenever possible;
- ask ONE question at a time;
- stop asking once the decision no longer depends on the answer.
```

Supervisor 已绑定 `HumanRequestBroker.ask`，因此运行中追问不需要改 AgentRuntime。

## 8. 澄清结果写入 Handoff / 全局记忆

不新增 ResearchState 字段，复用现有 `state.handoff_refs`：

```text
TASK_CLARIFICATION.md 内容：
- 用户原始 task
- 每个 Q&A（含选择的 option）
- 最终 TaskUnderstanding
- 未解决的不确定项
```

```python
ref = await runtime._store.put_text(clarification_md)
runtime.state.handoff_refs["task_clarification"] = ref
runtime.state.save(...)
```

`research/agent_turn_runner.py::_collect_handoff_texts()` 增加固定来源：

```python
if "task_clarification" in rt.state.handoff_refs:
    texts.append(await rt._store.get_text(...))
```

后续 PREPARE / baseline_ideator / SEARCH ideator 均通过 mailbox 获得澄清记录。

## 9. 前端：选择题弹窗

`src/athena_tui` 不涉及；前端改动在 `athena-gui`。

类型：

```ts
export interface ClarificationChoice {
  label: string;
  value: string;
}

export interface HumanRequest {
  request_id: string;
  prompt: string;
  choices?: ClarificationChoice[];
  allow_custom: boolean;
  allow_skip: boolean;
}
```

`HumanRequestDialog.tsx`：

- 有 choices 时渲染 1 / 2 / 3 按钮；
- 点击直接选择并提交；
- `allow_custom` 时显示文本框；
- `allow_skip` 时显示“跳过”。

`usePipeline.ts`：

- `sendPrompt` 调用 `sendMessage()`，该 Promise 在澄清期间保持 pending；
- 同时启动 `humanPending` 轮询；
- `answerHuman` 支持 choice / text / skip。

## 10. 数据流

```text
用户提交 task
  → parse_intent
    → LLM 生成 question(1/2/3) 或 final understanding
      → broker.ask() 阻塞
        → 前端 human_pending 轮询
          → HumanRequestDialog 弹窗
            → 用户选 1/2/3 / 自定义 / 跳过
              → human_reply 归一化回答
                → LLM 继续下一问或结束
                  → 写 TASK_CLARIFICATION.md + handoff_refs
                    → 返回最终预览卡
                      → 用户确认启动
                        → PREPARE/SEARCH 中 Supervisor 需要时继续弹窗
```

## 11. 错误处理与降级

- 每问超时按 skip 处理，不卡死前端。
- 用户连续跳过导致信息不足时，LLM 仍必须返回 TaskUnderstanding，字段允许为空。
- LLM 失败回退 `_heuristic_understanding`。
- 断点续传时若 `handoff_refs["task_clarification"]` 已存在，不重复生成。

## 12. 测试计划

- `HumanRequestBroker`：choices 存储、choice/text/skip 归一化、向后兼容。
- `RequestUserInputTool`：新 schema 校验、choices 透传。
- `parse_intent`：无提问直接 final、多轮提问、超时、最大问题数、LLM 失败回退。
- Handoff 收集：`task_clarification` ref 进入 mailbox。
- 前端：`HumanRequestDialog` 渲染 1/2/3、自定义、跳过并回调。

## 13. 文件改动清单

| 层 | 文件 |
|---|---|
| 后端 broker | `src/gui_gateway/human.py` |
| 工具 | `src/athena/core/agent/tools/user_input.py`、`src/athena/core/tool_types.py` |
| 服务 | `src/athena/gui/service.py` |
| Prompt | `src/athena/core/agent/prompts/supervisor_agent.md` |
| Handoff 收集 | `src/athena/research/agent_turn_runner.py` |
| 前端 | `athena-gui/src/lib/tauri-bridge.ts`、`athena-gui/src/components/shell/HumanRequestDialog.tsx`、`athena-gui/src/hooks/usePipeline.ts` |
| 测试 | broker / tool / service / frontend 单测 |
