"""DeepSeek 代码生成后端 — 让 CodeEngine 用 DeepSeek 驱动 generate→execute→iterate。

实现方式：用 prompt 驱动 Agent（DeepSeek ReAct + 通用 read/write/bash/pwsh 工具，
cwd=target_dir）在目标目录写模型代码；通过文件系统快照 diff 计算
files_created/files_modified，REPORT.md 内容作为 GenerationResult.output。
"""

import asyncio
from pathlib import Path

from athena.agents.tools.generic_tools import generic_tool_registry
from athena.code.backends.base import (
    CodeBackend,
    generation_result,
    render_backend_prompt,
    snapshot_files,
)
from athena.code.types import ExecutionOutput, GenerationResult
from athena.core.agent import settings
from athena.core.agent.models import AgentContext
from athena.core.agent.provider import ResponsesProvider
from athena.core.agent.runtime import Agent
from athena.core.agent.types import AgentMessage
from athena.core.thread_models import AthenaThread, AthenaTurn

_PROMPT_DIR = Path(__file__).resolve().parents[2] / "core" / "agent" / "prompts"
_DEFAULT_SYSTEM = (_PROMPT_DIR / "code_agent.md").read_text(encoding="utf-8")


async def _noop_emit(_kind: str, _ref: str, _data: dict | None = None) -> None:
    pass


class DeepSeekCodeBackend(CodeBackend):
    """用 DeepSeek ReAct Agent 在 target_dir 生成/迭代实验代码。"""

    def __init__(
        self,
        *,
        model: str | None = None,
        client: object | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self._model = model or settings.model_name()
        self._client = settings.get_client() if client is None else client
        self._system = system_prompt or _DEFAULT_SYSTEM

    async def generate(
        self,
        prompt: str,
        target_dir: str,
        previous_outputs: list[ExecutionOutput],
        history: list[dict],
    ) -> GenerationResult:
        """跑一次 DeepSeek ReAct：在 target_dir 写代码并执行迭代。

        前后文件快照之差即 files_created/files_modified；REPORT.md 文本作为
        ``output``（CodeEngine 的反馈与终止启发依赖它）。
        """
        before = snapshot_files(target_dir)
        full_prompt = render_backend_prompt(prompt, previous_outputs, history)
        agent = Agent(
            ResponsesProvider(self._model, client=self._client),
            generic_tool_registry(Path(target_dir)),
            self._system,
        )
        ctx = AgentContext(
            thread=AthenaThread(
                thread_id="codegen",
                session_id="codegen",
                status="running",
                context_ref="ctx://codegen",
            ),
            turn=AthenaTurn(
                turn_id="codegen-turn",
                thread_id="codegen",
                request_ref="ctx://codegen",
                status="running",
            ),
            emit=_noop_emit,
            tools=agent.tools,
            cancel=asyncio.Event(),
            input_text=full_prompt,
            messages=[AgentMessage(source="user", content=full_prompt)],
        )
        outcome = await agent.run(ctx)
        after = snapshot_files(target_dir)
        report = Path(target_dir) / "REPORT.md"
        output = (
            report.read_text(encoding="utf-8")
            if report.is_file()
            else f"generated: {outcome.result_ref}"
        )
        return generation_result(before, after, output=output)
