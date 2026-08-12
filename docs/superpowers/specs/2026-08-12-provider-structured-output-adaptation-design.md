# Provider 结构化输出适配(OpenAI / DeepSeek / Claude 槽位)

> 状态:已认可设计,待写实现计划
> 日期:2026-08-12
> 关联:`src/athena/core/agent/provider.py`、`src/athena/core/agent/settings.py`

## 背景与问题

`ResponsesProvider.stream()` 只要收到 `output_type` 就**无条件**发送
`response_format={"type": "json_schema", ...}`(`provider.py:76-83`)。仓库默认
后端是 DeepSeek(`settings.py:10` `DEFAULT_BASE_URL = api.deepseek.com`,`.env`
配 `DEEPSEEK_API_KEY`),其 OpenAI 兼容 API **不支持 `json_schema`**,因此每次
结构化调用先吃 HTTP 400 `BadRequestError` → 打 warning
"provider does not support json_schema response_format; retrying with
prompt-guided JSON" → 去掉 `response_format` 重试。结果:

- 每次结构化调用白烧一次注定失败的请求(延迟×2);
- warning 刷屏;
- 回退丢失 JSON 强制,偶发非 JSON 输出触发 `runtime.py` 的
  `model_validate_json` 重试轮,极端抛 `RuntimeError("structured output invalid
  after retries")`。

## 目标与范围

- **本轮**:OpenAI 与 DeepSeek 两个 provider 完整接线(DeepSeek 优先,消除 400 与
  回退);Claude 原生留明确槽位。
- **不做**:原生 Anthropic provider 实现、`anthropic` SDK 依赖、provider 推断
  (仅显式环境变量选择)。
- `stream()` 公开接口**完全不变**(`StreamEvent` / 取消 / 工具 / `output_type`),
  因此 `runtime.py`、agents、TUI 均不需改动。

## 决策记录(头脑风暴确认)

| 决策点 | 结论 |
| --- | --- |
| 优先级 | 先兼容 DeepSeek;OpenAI 顺带接通;Claude 下一轮 |
| DeepSeek 结构化形态 | `json_object` + 把完整 schema 注入 prompt |
| provider 选择 | 仅显式环境变量 `LLM_PROVIDER=deepseek\|openai\|anthropic`,不做推断 |
| 架构 | 多 provider 类(方案 C) |
| Claude 本轮程度 | 保留槽位,`AnthropicProvider` 构造即抛清晰错误 |

## 类层级(`src/athena/core/agent/provider.py`)

```
BaseProvider (ABC)                    # 稳定接口:model_name / client / stream()
├── ResponsesProvider                 # 保留现名 = OpenAI 兼容基类(现有 stream 主体平移),
│                                     #   新增 provider_kind 参数,按 kind 选结构化策略
│   ├── OpenAIProvider(ResponsesProvider)   # kind=openai → json_schema response_format(现状)
│   └── DeepSeekProvider(ResponsesProvider) # kind=deepseek → json_object + schema 注入 prompt
└── AnthropicProvider(BaseProvider)   # 保留槽位;__init__ 抛 NotImplementedError
                                      #   ("native Anthropic provider not yet implemented;
                                      #    set LLM_PROVIDER=deepseek|openai")
```

- `ResponsesProvider` 保留类名作为 OpenAI 兼容基类,`stream()` 主体从现状平移,
  仅新增结构化策略分流。
- `provider_kind` 作为构造参数(缺省取 `settings.provider_kind()`,即仓库现状
  `deepseek`)。测试可注入 fake client + 显式 kind。

## settings 选择机制(`src/athena/core/agent/settings.py`)

- 新增 `provider_kind() -> str`:读 `LLM_PROVIDER`,校验 ∈ {deepseek, openai,
  anthropic};非法值 → `ValueError`(构造期即失败);缺省 `"deepseek"`。
- 与现有 `model_name()`/`base_url()` 并列,保持 `get_client()` 不变。

## DeepSeek 结构化输出(核心适配)

在基类 `stream()` 内按 `provider_kind` 分流:

- `openai`:`kw["response_format"] = {"type": "json_schema", "json_schema":
  {"name": ..., "schema": output_type.model_json_schema()}}`(现状,不动)。
- `deepseek`:
  1. 往 `api_msgs` **末尾追加**一条 system 消息:
     `"Return a JSON object matching this schema:\n" + json.dumps(output_type.model_json_schema(), ensure_ascii=False)`;
     该消息含 "JSON" 字样,满足 DeepSeek `json_object` 模式要求 prompt 含 "json"
     的前置条件。
  2. `kw["response_format"] = {"type": "json_object"}`。
  3. **不发送** `json_schema`。
- 现有 `BadRequestError` 回退**保留为安全网**(`_response_format_unavailable`
  匹配后去掉 `response_format` 重试),覆盖仍拒绝 `json_object` 的第三方兼容端点。

注入的 schema 消息在 `kw` 构造前追加到 `api_msgs` 副本,首次与回退重试均携带。

## 工厂与调用点迁移(共 22 处)

- 新增 `create_provider(model, *, client=None) -> BaseProvider`:按
  `settings.provider_kind()` 返回对应类(`openai`→`OpenAIProvider`,
  `deepseek`→`DeepSeekProvider`,`anthropic`→`AnthropicProvider`)。
- **src 5 处** `ResponsesProvider(model, client=client)` → `create_provider(...)`:
  - `src/athena/agents/production.py:77`
  - `src/athena/agents/reflection_agent.py:185`
  - `src/athena/agents/prompt_agent.py:64`
  - `src/athena/core/agent/runtime.py:488`
  - `src/athena/research/runtime.py:149`
  随环境变量自动适配。
- **测试 17 处** 保持 `ResponsesProvider("model", client=...)` 不变:基类缺省
  kind=`deepseek` = 仓库现状,现有断言不破;新增响应格式测试用
  `OpenAIProvider`/`DeepSeekProvider` 或显式 kind 精确断言。
- 保留 `ResponsesProvider` 名 → 外部引用零破坏。

## 错误处理

- `LLM_PROVIDER=anthropic` → `AnthropicProvider.__init__` 抛清晰
  `NotImplementedError`,不静默走错路径。
- `LLM_PROVIDER` 非法值 → `settings.provider_kind()` 抛 `ValueError`。

## 测试

- `test/unit/test_agent.py` 现有流式/工具测试:**应全绿**(基类行为不变,缺省
  kind=deepseek)。
- 新增单测(fake client 脚本化 chunk):
  - deepseek:断言 `kw["response_format"] == {"type": "json_object"}` 且
    `api_msgs` 含 schema 消息、**不含** `json_schema`;
  - openai:断言 `json_schema` 结构;
  - 回退:`BadRequestError` 后去掉 `response_format` 重试仍工作;
  - 工厂:`LLM_PROVIDER` 各值 → 对应类;`anthropic` → 清晰报错;非法值 →
    `ValueError`;
  - settings:`provider_kind()` 缺省 / 显式 / 非法三态。
- 不改 `runtime.py` 的 stream 消费方、agents、TUI、`core/agent/models.py`。

## 文件清单

- `src/athena/core/agent/provider.py`(重构:主体平移 + kind 分流 + 子类 + 工厂)
- `src/athena/core/agent/settings.py`(+`provider_kind`)
- `src/athena/agents/production.py`、`reflection_agent.py`、`prompt_agent.py`
- `src/athena/research/runtime.py`(仅两处换工厂)
- `test/unit/test_agent.py`、settings 相关单测(补 provider 适配测试)

## 保留后续(不在本轮)

- 原生 `AnthropicProvider`(加 `anthropic` SDK,`messages.create` + `output_schema`)。
- provider 能力表扩展到更多 OpenAI 兼容端点。
