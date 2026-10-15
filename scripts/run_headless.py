"""Run one Athena workflow without a TUI or interactive approval prompts.

直接构造 ``ResearchRuntime``（task seed -> PREPARE -> SEARCH -> VALIDATE），订阅
双事件打印进度，跑到终态后退出。不经过旧 CLI 的 ``dispatch`` 面（已被 A.2 移除）。
"""

import argparse
import asyncio
import sys
import traceback
from pathlib import Path

from athena.core.agent import settings
from athena.core.project_lock import ProjectBusyError, project_lock
from athena.research import ResearchRuntime
from athena.research.config import (
    ProviderConfig,
    ResearchOptions,
    ResearchPolicy,
    RuntimeDependencies,
    SearchLimits,
    TaskConfig,
)


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="Athena-headless")
    parser.add_argument(
        "--project", required=True, help="项目根目录（.athena 状态/工件）"
    )
    parser.add_argument("--task", required=True, help="研究任务（自然语言）")
    parser.add_argument("--data", help="输入数据路径；给出时并入任务文本")
    parser.add_argument(
        "--search-limit",
        type=int,
        default=10,
        help="SEARCH 尝试次数上限；加速验证可调小（如 2）",
    )
    args = parser.parse_args(argv)
    if args.search_limit < 0:
        parser.error("--search-limit must be non-negative")
    return args


def _subscribe(runtime: ResearchRuntime) -> None:
    agent_buf: list[str] = []
    agent_plan: object | None = None

    def flush_agent() -> None:
        """Print one bounded agent message before another event takes the console."""
        nonlocal agent_buf, agent_plan
        if agent_buf:
            head = "".join(agent_buf)
            print(
                f"[agent] {head[:400]}..." if len(head) > 400 else f"[agent] {head}",
                flush=True,
            )
        agent_buf = []
        agent_plan = None

    def on_event(kind: str, payload: dict) -> None:
        """Render canonical state changes and coalesce consecutive agent fragments."""
        nonlocal agent_buf, agent_plan
        if kind == "output":
            source = payload.get("source", "?")
            channel = payload.get("channel", "text")
            text = payload.get("text", "")
            if source == "agent" and channel == "text":
                plan = payload.get("plan")
                if plan != agent_plan:
                    flush_agent()
                    agent_plan = plan
                agent_buf.append(text)
                return
            # 非 agent 文本事件前，flush 已累积的 agent 消息，避免逐词碎片。
            flush_agent()
            if text:
                head = text if len(text) <= 400 else text[:400] + "..."
                print(f"[{source}/{channel}] {head}", flush=True)
        elif kind == "state":
            flush_agent()
            print(
                f"[state] phase={payload.get('phase')} status={payload.get('status')}",
                flush=True,
            )

    runtime.subscribe(on_event)


async def _run(args: argparse.Namespace) -> int:
    # 数据路径预检：拼错/缺失的 --data 应在跑 LLM 之前立刻失败，而非白烧一轮。
    if args.data and not Path(args.data).exists():
        print(f"error: data path does not exist: {args.data}", file=sys.stderr)
        return 2
    task_text = args.task
    if args.data:
        task_text = f"{task_text}\n数据集路径: {Path(args.data).resolve()}"
    # This entry point obeys the same single-writer rule as Athena-cli run.
    with project_lock(Path(args.project) / ".athena"):
        runtime = ResearchRuntime(
            project_root=Path(args.project),
            research=ResearchOptions(
                task=TaskConfig(
                    text=task_text, confirmation_gate=False, auto_confirm=True
                ),
                search=SearchLimits(search_limit=args.search_limit),
                policy=ResearchPolicy(auto_validate=True),
            ),
            dependencies=RuntimeDependencies(
                provider=ProviderConfig(model=settings.model_name())
            ),
        )
        try:
            _subscribe(runtime)
            supervisor_task = await runtime.start()
            await supervisor_task
            state = runtime.state
            print(f"TERMINAL: phase={state.phase} status={state.status}", flush=True)
            return 0 if state.phase == "COMPLETED" and state.status != "FAILED" else 1
        except Exception as exc:  # noqa: BLE001 - process boundary reports failures
            print(f"RUN FAILED: {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()
            return 1
        finally:
            # Cancellation also joins runtime cleanup before releasing the lock.
            await runtime.aclose()


def main(argv: list[str] | None = None) -> int:
    """Run a noninteractive task and return failure unless research completed."""
    args = _parse(sys.argv[1:] if argv is None else argv)
    try:
        return asyncio.run(_run(args))
    except ProjectBusyError as exc:
        # A live CLI/GUI process already owns the project; no new runtime was started.
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
