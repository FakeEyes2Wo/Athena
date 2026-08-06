# Athena Agent 接口收敛设计

> 日期：2026-07-29
> 状态：已确认
> 范围：Task 2 Agent 公共接口与代码归属

## 1. 背景

Athena 已有两条经过测试的 Agent 运行路径：

- `athena.core.agent.Agent` 提供流式采样、工具调用与上下文执行。
- App Server 通过 `agent_runner`、`ThreadRuntime` 和既有中断机制运行 Agent。

原 Task 2 在 `athena.agents` 中增加了另一套基于 `CodeEngine` 的 `Agent`、结果类型、角色注册表和中断状态。这会形成两个 Python Agent 内核，也会增加未来切换到 Rust core Agent 框架时的适配面。

## 2. 决策

`athena.core.agent.Agent` 是唯一的 Python Agent 实现。App Server 继续使用现有运行路径。Task 2 将 model 与 client 合并到现有 `ResponsesProvider`，并提供一个参数顺序固定的构造接口：

```python
def create_code_agent(
    model: ResponsesProvider,
    tools: ToolRegistry,
    system_prompt: str,
    config: AgentConfig | None = None,
) -> Agent:
    return Agent(model, tools, system_prompt, config or AgentConfig())
```

`ResponsesProvider(model, client=client)` 同时拥有模型标识和 OpenAI-compatible client，作为 LangChain 风格的 model 对象传入。不得再把 model 名称和 client 拆成两个工厂参数，也不新增 `ModelClient` 包装类型。

```python
class ResponsesProvider:
    def __init__(
        self,
        model: str,
        *,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self._model_name = model
        self._client = client

    @property
    def model_name(self) -> str:
        return self._model_name
```

`system_prompt` 与 `tools` 保持显式，使调用处可以直接看出 Agent 的能力与指令。`AgentConfig` 位于最后，只保留 `max_turns`、`max_tokens`、`temperature` 和 `name`。不得把 model、client、tools 或 system prompt 放回 config。

`create_code_agent` 返回现有 `Agent`。它不定义新 Agent 类，也不复制 Agent loop、上下文、事件或中断逻辑。项目不引入 LangChain 依赖，只采用 model 对象、显式 prompt/tools 和末尾运行配置的接口布局。

现有 `create_agent` 保持为兼容包装器：它接受现有的 model 字符串、tools、system prompt、client 和调优参数，组装 `ResponsesProvider` 与 `AgentConfig` 后调用 `create_code_agent`。没有消费者读取自定义 Agent description，因此删除该未使用参数；`Agent.description` 改为由 model 派生。`agent_tool_example.py` 的调用方式保持有效。

## 3. 三 Agent 组装

运行环境分别调用三次 `create_code_agent`，通过调用参数配置 code、data、plot 三个 Agent：

```python
model = ResponsesProvider(model_name, client=client)
code_agent = create_code_agent(model, code_tools, code_prompt, code_config)
data_agent = create_code_agent(model, data_tools, data_prompt, data_config)
plot_agent = create_code_agent(model, plot_tools, plot_prompt, plot_config)
```

三个配置都是现有 `AgentConfig` 实例，分别通过 `name` 标识 Agent。工厂函数不接受 `role`。三个 Agent 的差异由显式 prompt、tools 和末尾 config 表达，不建立角色枚举、注册表或复合容器。

`Agent` 只保存 `model`、`tools`、`system_prompt` 和 `config`。`name` 从 `config.name` 读取，`description` 从 `model.model_name` 派生，二者不重复存储。

## 4. 文件归属

- `src/athena/core/agent/agent.py`：保留现有执行算法，收敛 `AgentConfig`、`Agent` 字段和两个构造函数的接口。
- `src/athena/core/agent/provider.py`：让现有 `ResponsesProvider` 同时持有 model 名称与 client，不改变流式事件行为。
- `src/athena/core/agent/__init__.py`：公开导出 `create_code_agent`。
- `src/athena/core/agent/prompts/`：保存 code、data、plot 的现有 prompt 资源；工厂不负责按角色查找。
- `src/athena/code/output_specs.py`：保存代码执行产出约束；约束由环境或执行工具使用，不进入 Agent 状态。
- `src/athena/agents/`：消费者迁移且静态扫描为零后删除，不保留兼容别名。

`agent_tool_example.py` 继续作为最小公共用法示例。重复的 `athena.agents.demo_agent` 不再作为第二入口。

## 5. 不变范围

Task 2 不修改以下内部逻辑：

- `Agent.run`、采样循环、工具并发屏障和 provider；
- `AgentContext`、memory 注入和 `agent_runner`；
- App Server 的 `ThreadRuntime`、事件顺序、取消和中断；
- Rust core Agent 的实现与回退选择。

本次接口收敛也不引入 Environment 类、角色协议、Agent 适配器类、model 包装类、额外配置类型或结果 DTO。

## 6. 错误与兼容性

`create_code_agent` 沿用 `Agent` 的运行期错误，不捕获或改写 provider、工具或 App Server 异常。旧的 `athena.agents.create_agent(role, target_dir, engine)` 被明确移除，调用方必须迁移到新的显式组装方式。

## 7. 验收

- `create_code_agent` 返回 `athena.core.agent.Agent`。
- 公开签名依次为 `model`、`tools`、`system_prompt`、`config=None`，config 始终位于最后。
- model 对象同时持有模型标识和 client；工厂签名没有独立 client 参数。
- 分别调用三次会得到三个独立 Agent，且各自保留显式 prompt、tools 和 config name。
- `create_code_agent` 不接受 `role`，不创建额外持久属性。
- `AgentConfig` 仍是唯一 Agent 配置类型，只包含三个调优参数和 name。
- `Agent` 恰好保存 `model`、`tools`、`system_prompt` 和 `config` 四项。
- `agent_tool_example.py`、core Agent 单元测试和 App Server 测试保持通过。
- 生产、测试和示例代码不再导入 `athena.agents`。
- `src/athena/agents/` 在零导入证明后删除。
