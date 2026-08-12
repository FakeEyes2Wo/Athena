"""Command-line adapter for Athena's output/state-only research runtime."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from athena.core.agent import settings
from athena.research import ResearchRuntime


def _runtime(project_root: str, **options: Any) -> ResearchRuntime:
    """Build the public research composition root for one project."""
    return ResearchRuntime(
        project_root=Path(project_root),
        model=settings.model_name(),
        **options,
    )


def _runtime_options(args: argparse.Namespace) -> dict[str, object]:
    """Translate run arguments into supported ``ResearchRuntime`` options."""
    task_lines = [args.task or "", f"Dataset path: {args.data}"]
    optional_context = (
        ("Target", args.target),
        ("Task type", args.task_type),
        ("Data type", args.data_type),
        ("Primary metric", args.metric),
        ("K-fold policy", args.kfold if args.kfold != "auto" else None),
    )
    task_lines.extend(f"{label}: {value}" for label, value in optional_context if value)
    return {
        "task": "\n".join(line for line in task_lines if line),
        "search_limit": args.max_search_experiments or 10,
        "auto_validate": args.mode == "auto",
        "direction": args.direction or "maximize",
    }


def _render_event(kind: str, payload: dict[str, object]) -> None:
    if kind == "output":
        source = payload.get("source", "runtime")
        text = payload.get("text", "")
        if text:
            print(f"{source}> {text}", flush=True)
        return
    if kind == "state":
        print(
            f"phase={payload.get('phase', '-')} "
            f"status={payload.get('status', '-')}",
            flush=True,
        )
        return
    raise ValueError(f"unsupported runtime event kind: {kind}")


async def _cmd_run(args: argparse.Namespace) -> int:
    runtime = _runtime(args.project, **_runtime_options(args))
    terminal = asyncio.Event()
    exit_code = 0

    def receive(kind: str, payload: dict[str, object]) -> None:
        nonlocal exit_code
        _render_event(kind, payload)
        if kind != "state":
            return
        status = payload.get("status")
        phase = payload.get("phase")
        if status == "STOPPED" or phase == "COMPLETED":
            terminal.set()
        elif status == "FAILED":
            exit_code = 1
            terminal.set()

    subscription_id = runtime.subscribe(receive)
    try:
        await runtime.start()
        await terminal.wait()
        return exit_code
    finally:
        runtime.unsubscribe(subscription_id)
        await runtime.aclose()


async def _cmd_status(args: argparse.Namespace) -> int:
    runtime = _runtime(args.project)
    snapshot: dict[str, object] = {}

    def receive(kind: str, payload: dict[str, object]) -> None:
        if kind == "state":
            snapshot.update(payload)

    subscription_id = runtime.subscribe(receive)
    try:
        print(json.dumps(snapshot, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        runtime.unsubscribe(subscription_id)
        await runtime.aclose()


async def _cmd_control(args: argparse.Namespace) -> int:
    runtime = _runtime(args.project)
    try:
        status = await runtime.message(f"/{args.command}")
        print(f"status={status}")
        return 0
    finally:
        await runtime.aclose()


async def _dispatch_command(args: argparse.Namespace) -> int:
    if args.command == "run":
        return await _cmd_run(args)
    if args.command == "status":
        return await _cmd_status(args)
    return await _cmd_control(args)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="Athena-cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--project", required=True, help="project root")
    run.add_argument("--data", required=True, help="input dataset path")
    run.add_argument("--task", help="research intent")
    run.add_argument("--target", help="supervised target column")
    run.add_argument("--task-type", help="task type, for example classification")
    run.add_argument("--data-type", help="data type, for example tabular")
    run.add_argument("--metric", help="primary evaluation metric")
    run.add_argument(
        "--direction", choices=["maximize", "minimize"], help="metric direction"
    )
    run.add_argument("--mode", choices=["interactive", "auto"], default="interactive")
    run.add_argument(
        "--kfold", choices=["required", "auto", "disabled"], default="auto"
    )
    run.add_argument(
        "--max-search-experiments", type=int, help="maximum SEARCH attempts"
    )
    for name in ("status", "pause", "resume", "stop"):
        subparsers.add_parser(name).add_argument(
            "--project", required=True, help="project root"
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    return asyncio.run(_dispatch_command(args))


if __name__ == "__main__":
    raise SystemExit(main())
