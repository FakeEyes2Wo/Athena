"""Run one Athena workflow without a TUI or interactive approval prompts.

直接构造 ``ResearchRuntime``（task seed -> PREPARE -> SEARCH -> VALIDATE），订阅
双事件打印进度，跑到终态后退出。不经过旧 CLI 的 ``dispatch`` 面（已被 A.2 移除）。
"""

import argparse
import asyncio
import sys
from pathlib import Path

from athena.core.agent import settings
from athena.research import ResearchRuntime


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
    return parser.parse_args(argv)


def _subscribe(runtime: ResearchRuntime) -> None:
    agent_buf: list[str] = []
    agent_plan: object | None = None

    def flush_agent() -> None:
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
    task_text = args.task
    if args.data:
        task_text = f"{task_text}\n数据集路径: {args.data}"
    runtime = ResearchRuntime(
        project_root=Path(args.project),
        model=settings.model_name(),
        task=task_text,
        auto_validate=True,
        search_limit=args.search_limit,
    )
    _subscribe(runtime)
    try:
        supervisor_task = await runtime.start()
        await supervisor_task
    except Exception as exc:
        print(f"RUN FAILED: {type(exc).__name__}: {exc}", flush=True)
        await runtime.aclose()
        return 1
    state = runtime.state
    print(f"TERMINAL: phase={state.phase} status={state.status}", flush=True)
    await runtime.aclose()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
