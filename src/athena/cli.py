"""Athena-cli — 唯一受支持的通用命令行入口（supervisor_design §3.1）。

通过 ``uv run Athena-cli <command> --project <project_root> ...`` 调用，直接构造
``ResearchRuntime``，不经过、不修改 ``src/athena/app_server/``。CLI 只转换参数、
调用公开方法并渲染结果，不拥有阶段判断、重试、数据语义或项目事实。

# TODO(supervisor-ui): Athena-cli 与 ResearchRuntime 控制/事件合同稳定后，基于同一 RUN、STATUS、HumanRequest 和事件 API 增加 TUI/GUI 适配器；不得复制 Supervisor 状态机或编排逻辑，App Server 集成另行设计。
"""

import argparse
import asyncio
from pathlib import Path
from typing import Any, cast

from athena.core.agent import settings
from athena.research import ResearchRuntime


def _runtime(project_root: str) -> ResearchRuntime:
    """构造并返回绑定到项目根的 ResearchRuntime（打开 supervisor.db）。

    自动注册 LLM worker（model 必填，无回退）。
    """
    return ResearchRuntime(project_root=Path(project_root), model=settings.model_name())


def _execution_of(result: dict[str, object]) -> dict[str, Any]:
    """取出 STATUS 快照中的 execution 子对象。"""
    execution = result.get("execution", {})
    return execution if isinstance(execution, dict) else {}


async def _cmd_run(args: argparse.Namespace) -> int:
    runtime = _runtime(args.project)
    try:
        result = await runtime.dispatch(
            "TASK_CONFIGURE",
            {
                "interaction_mode": args.mode,
                "kfold_policy": args.kfold,
                "task": args.task or "",
                "data_path": args.data or "",
            },
        )
        print(f"configured phase={result['phase']} mode={result['interaction_mode']}")
        run = await runtime.dispatch("RUN", {})
        print(
            f"execution={run['execution_id']} status={run['status']} "
            f"phase={run['phase']}"
        )
        # 附着（首版 CLI 必须附着到 terminal）：持续显示稳定状态；
        # 出现 HumanRequest 时读取命令行输入后 HUMAN_REPLY
        while True:
            status = await runtime.dispatch("STATUS", {})
            execution = _execution_of(status)
            phase = execution.get("phase", "IDLE") if execution else "IDLE"
            exec_status = execution.get("status", "-") if execution else "-"
            print(
                f"phase={phase} status={exec_status} "
                f"version={status.get('state_version')}"
            )
            request = status.get("human_request")
            if isinstance(request, dict) and request.get("request_id"):
                print(
                    f"[request {request['request_id']}] {request.get('question', '')}"
                )
                for i, opt in enumerate(request.get("options") or []):
                    print(f"  {i + 1}. {opt}")
                answer = await asyncio.to_thread(input, "answer> ")
                await runtime.dispatch(
                    "HUMAN_REPLY",
                    {"request_id": request["request_id"], "answer": answer},
                )
                continue
            if execution and execution.get("status") == "CANCELLED":
                return 0
            if execution and execution.get("phase") == "COMPLETED":
                return 0
            await asyncio.sleep(2)
    finally:
        await runtime.aclose()


async def _cmd_status(args: argparse.Namespace) -> int:
    runtime = _runtime(args.project)
    try:
        status = await runtime.dispatch("STATUS", {})
        execution = _execution_of(status)
        print(
            f"phase={execution['phase']} status={execution['status']} "
            f"id={execution['id']} version={status.get('state_version')}"
        )
        return 0
    finally:
        await runtime.aclose()


async def _cmd_pause_resume_stop(args: argparse.Namespace) -> int:
    runtime = _runtime(args.project)
    try:
        result = await runtime.dispatch(args.command.upper(), {})
        print(f"status={result.get('status')}")
        return 0
    finally:
        await runtime.aclose()


async def _cmd_requests(args: argparse.Namespace) -> int:
    runtime = _runtime(args.project)
    try:
        result = await runtime.dispatch("REQUESTS_GET", {})
        requests = result.get("requests", [])
        for request in requests if isinstance(requests, list) else []:
            item = cast("dict[str, object]", request)
            print(
                f"{item['request_id']} [{item['reason_code']}] "
                f"{item['question']} options={item['options']}"
            )
        return 0
    finally:
        await runtime.aclose()


async def _cmd_reply(args: argparse.Namespace) -> int:
    runtime = _runtime(args.project)
    try:
        result = await runtime.dispatch(
            "HUMAN_REPLY",
            {"request_id": args.request, "answer": args.answer},
        )
        print(f"answered={result['answered']}")
        return 0
    finally:
        await runtime.aclose()


async def _dispatch_command(args: argparse.Namespace) -> int:
    handler = {
        "run": _cmd_run,
        "status": _cmd_status,
        "pause": _cmd_pause_resume_stop,
        "resume": _cmd_pause_resume_stop,
        "stop": _cmd_pause_resume_stop,
        "requests": _cmd_requests,
        "reply": _cmd_reply,
    }[args.command]
    return await handler(args)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="Athena-cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--project", required=True, help="项目根目录")
    for name in ("status", "pause", "resume", "stop", "requests"):
        subparsers.add_parser(name).add_argument(
            "--project", required=True, help="项目根目录"
        )
    reply = subparsers.add_parser("reply")
    reply.add_argument("--project", required=True, help="项目根目录")
    run.add_argument("--data", help="输入数据路径")
    run.add_argument("--task", help="intent 描述（自然语言）")
    run.add_argument(
        "--mode",
        choices=["interactive", "auto"],
        default="interactive",
        help="交互模式",
    )
    run.add_argument(
        "--kfold",
        choices=["required", "auto", "disabled"],
        default="auto",
        help="K-fold 策略",
    )
    reply.add_argument("--request", required=True, help="request_id")
    reply.add_argument("--answer", required=True, help="回答内容")
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    return asyncio.run(_dispatch_command(args))


if __name__ == "__main__":
    raise SystemExit(main())
