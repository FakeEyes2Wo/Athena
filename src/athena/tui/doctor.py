"""配置自检 — ``python -m athena.tui --check``。

逐项确认 TUI 真正依赖的东西：环境变量、端点连通、模型可用、流式工具调用。
刻意复用 ``ResponsesProvider`` 而不是自己新建 client，测的就是 Agent 会走的那条路。
"""

import os
import time

from athena.core.agent.provider import ResponsesProvider
from athena.tui.runner import MODEL_ENV, AgentRuntime
from athena.tui.theme import Theme, load_theme

PROBE_PROMPT = "只回答两个字：收到"
PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "list_dir",
        "description": "List files and directories.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
}


def mask(secret: str) -> str:
    """只留头尾，中间打码 —— 自检输出可能被贴进 issue。"""
    if not secret:
        return "（未设置）"
    if len(secret) <= 12:
        return secret[:2] + "…"
    return f"{secret[:6]}…{secret[-4:]}（{len(secret)} 字符）"


class Doctor:
    """按顺序跑检查，任何一步失败都继续往下走，最后汇总。"""

    def __init__(self, theme: Theme | None = None) -> None:
        self.theme = theme or load_theme()
        self.failures = 0

    def report(self, ok: bool, label: str, detail: str = "") -> None:
        symbol = self.theme.symbols.ok if ok else self.theme.symbols.fail
        if not ok:
            self.failures += 1
        tail = f"  {detail}" if detail else ""
        print(f"  {symbol} {label}{tail}")

    def check_env(self) -> str:
        """返回解析出的模型名。"""
        print("环境变量")
        key = os.environ.get("OPENAI_API_KEY", "")
        base = os.environ.get("OPENAI_BASE_URL", "")
        self.report(bool(key), "OPENAI_API_KEY", mask(key))
        self.report(True, "OPENAI_BASE_URL", base or "（空，走 OpenAI 官方地址）")
        model = os.environ.get(MODEL_ENV, "")
        self.report(True, MODEL_ENV, model or "（未设置，用 profile 默认值）")
        return model

    def check_runtime(self) -> str:
        """确认 agent 能装配起来，返回它最终会用的模型名。"""
        print("Agent 装配")
        runtime = AgentRuntime()
        self.report(
            True,
            f"profile={runtime.profile}",
            f"model={runtime.model}  tools={runtime.tool_names}",
        )
        return runtime.model

    async def check_chat(self, model: str) -> None:
        print("端点连通")
        client = ResponsesProvider().client
        started = time.monotonic()
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": PROBE_PROMPT}],
                max_tokens=32,
            )
        except Exception as exc:
            self.report(False, "chat.completions", f"{type(exc).__name__}: {exc}")
            return
        elapsed = (time.monotonic() - started) * 1000
        text = (response.choices[0].message.content or "").strip()
        self.report(True, "chat.completions", f"{int(elapsed)}ms  回复={text!r}")

    async def check_streaming_tools(self, model: str) -> None:
        """Agent loop 完全依赖流式 tool_calls —— 端点不支持就等于不可用。"""
        print("流式工具调用")
        client = ResponsesProvider().client
        try:
            stream = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "列出当前目录，用工具"}],
                tools=[PROBE_TOOL],
                tool_choice="auto",
                stream=True,
                max_tokens=128,
            )
            saw_tool_call = False
            async for chunk in stream:
                for choice in chunk.choices or []:
                    if choice.delta.tool_calls:
                        saw_tool_call = True
        except Exception as exc:
            self.report(False, "stream + tools", f"{type(exc).__name__}: {exc}")
            return
        self.report(
            saw_tool_call,
            "stream + tools",
            "端点返回了 tool_calls" if saw_tool_call else "端点没有返回 tool_calls",
        )

    async def run(self) -> int:
        self.check_env()
        model = self.check_runtime()
        await self.check_chat(model)
        await self.check_streaming_tools(model)
        if self.failures:
            print(f"\n{self.failures} 项未通过。检查 .env 与 secrets/ 下的凭据。")
            return 1
        print("\n全部通过，可以直接 python -m athena.tui 启动。")
        return 0
