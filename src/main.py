"""Athena supervisor-driven workflow 入口（supervisor_design §3.1）。

驱动 ResearchRuntime：TASK_CONFIGURE → RUN → 附着轮询 STATUS，出现 HumanRequest
时读取命令行回答后 HUMAN_REPLY，直到 phase=COMPLETED 或 status=CANCELLED。
附着期间实时打印每个 Agent 的对话消息（读取 ``.athena/sessions/*.jsonl``
rollout 增量）。App Server 不参与本入口。

接口沿用 supervisor 契约：

    uv run python src/main.py run --project <project_root> --data <input_path> \
        --task <intent> --mode interactive|auto [--kfold required|auto|disabled]
    uv run python src/main.py status --project <project_root>
    uv run python src/main.py pause|resume|stop --project <project_root>
    uv run python src/main.py requests --project <project_root>
    uv run python src/main.py reply --project <project_root> --request <id> --answer <text>

无参数直接运行（``uv run python src/main.py``）时，默认补全为
``run --project <临时目录> --data examples/titanic --task <固定 intent>``
（supervisor_design §10 使用仓库自带 Titanic 数据集与临时唯一项目根）。显式
提供的参数一律保留，不被默认值覆盖。``--project`` 缺省用一次性临时目录，避免
写入仓库或被禁止的 ``examples/titanic-run``。
"""

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from athena.cli import _build_parser, _execution_of, _runtime

_DEFAULT_DATA = "examples/titanic"
_DEFAULT_TASK = (
    "discover a valid supervised prediction task and build the best model "
    "supported by the data"
)
_COMMANDS = frozenset({"run", "status", "pause", "resume", "stop", "requests", "reply"})

# 每个 agent 的 rollout 文件读取偏移：{agent_id -> 已打印的行数}
_offsets: dict[str, int] = {}


def _run_argv(argv: list[str]) -> list[str]:
    """run 未给 --project/--data/--task 时注入默认值，未给子命令时补 run。

    只处理 ``run`` 子命令；其他子命令（status/pause/...）原样返回。命令行里
    显式提供过（``--data x`` 或 ``--data=x``）时不覆盖用户输入。
    """
    if not argv or argv[0] not in _COMMANDS:
        if argv and argv[0] in {"-h", "--help"}:
            return argv
        argv = ["run", *argv]
    if argv[0] != "run":
        return argv
    has_project = any(
        arg == "--project" or arg.startswith("--project=") for arg in argv
    )
    has_data = any(arg == "--data" or arg.startswith("--data=") for arg in argv)
    has_task = any(arg == "--task" or arg.startswith("--task=") for arg in argv)
    extra: list[str] = []
    if not has_project:
        extra += ["--project", tempfile.mkdtemp(prefix="athena-")]
    if not has_data:
        extra += ["--data", _DEFAULT_DATA]
    if not has_task:
        extra += ["--task", _DEFAULT_TASK]
    return argv + extra


# ---- 打印 Agent 消息（读取 rollout JSONL 增量） ----


def _render_message(msg: dict) -> str:
    """把一条 pydantic_ai ModelMessage dict 渲染为可读文本。"""
    kind = msg.get("kind", "")
    parts = msg.get("parts") or []
    lines: list[str] = []
    for part in parts:
        pk = part.get("part_kind", "")
        if pk == "user-prompt":
            lines.append(f"  user> {part.get('content', '')}")
        elif pk == "text":
            lines.append(f"  agent> {part.get('content', '')}")
        elif pk == "tool-call":
            args = part.get("args", "")
            if isinstance(args, dict):
                args = json.dumps(args, ensure_ascii=False)
            lines.append(f"  tool  → {part.get('tool_name', '')}({args})")
        elif pk == "tool-return":
            content = part.get("content", "")
            lines.append(f"  tool  ← {str(content)[:300]}")
    return "\n".join(lines)


def _print_new_messages(project_root: Path) -> None:
    """读取每个 agent 的 rollout 文件，打印自上次轮询以来的新消息。"""
    sessions_dir = project_root / ".athena" / "sessions"
    if not sessions_dir.is_dir():
        return
    for path in sorted(sessions_dir.glob("*.jsonl")):
        agent_id = path.stem
        offset = _offsets.get(agent_id, 0)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines[offset:]:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg_list = record.get("msg")
            if not isinstance(msg_list, list):
                continue
            for msg in msg_list:
                rendered = _render_message(msg) if isinstance(msg, dict) else ""
                if rendered:
                    print(f"[{agent_id[:8]}] {rendered}", flush=True)
        _offsets[agent_id] = len(lines)


async def _cmd_run(args: argparse.Namespace) -> int:
    runtime = _runtime(args.project)
    project_root = Path(args.project)
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
        last_state: tuple[str, str, object] | None = None
        while True:
            status = await runtime.dispatch("STATUS", {})
            execution = _execution_of(status)
            phase = execution.get("phase", "IDLE") if execution else "IDLE"
            exec_status = execution.get("status", "-") if execution else "-"
            version = status.get("state_version")
            state = (phase, exec_status, version)
            if state != last_state:
                print(
                    f"phase={phase} status={exec_status} " f"version={version}",
                    flush=True,
                )
                last_state = state
            _print_new_messages(project_root)
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


async def _dispatch_command(args: argparse.Namespace) -> int:
    if args.command == "run":
        return await _cmd_run(args)
    from athena.cli import _dispatch_command as cli_dispatch

    return await cli_dispatch(args)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    argv = _run_argv(list(argv))
    parser = _build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_dispatch_command(args))


if __name__ == "__main__":
    raise SystemExit(main())
