"""Run one Athena workflow without a TUI or interactive approval prompts.

直接构造 ``ResearchRuntime``（task seed -> PREPARE -> SEARCH -> VALIDATE），订阅
双事件打印进度，跑到终态后退出。不经过旧 CLI 的 ``dispatch`` 面（已被 A.2 移除）。
"""

import argparse
import asyncio
import codecs
import ctypes
import os
import sys
from pathlib import Path

from athena.core.agent import settings
from athena.research import ResearchRuntime


def _parent_console_encoding() -> str | None:
    """Return the Windows console output code page used by PowerShell pipes."""
    if sys.platform != "win32":
        return None
    try:
        code_page = int(ctypes.windll.kernel32.GetConsoleOutputCP())
        if code_page <= 0:
            return None
        encoding = "utf-8" if code_page == 65001 else f"cp{code_page}"
        codecs.lookup(encoding)
        return encoding
    except (AttributeError, LookupError, OSError, ValueError):
        return None


def _configure_utf8_stdio() -> None:
    """Make text output safe while matching the parent PowerShell code page.

    Windows PowerShell 5.1 decodes native-process pipes with its active code
    page, which may differ from Python's redirected ``sys.stdout.encoding``.
    Match the actual console code page and make unrepresentable characters
    non-fatal.  This supports both the default Chinese code page and sessions
    whose console was explicitly switched to UTF-8.
    """
    explicit_encoding: str | None = None
    requested = os.environ.get("PYTHONIOENCODING", "").partition(":")[0].strip()
    if requested:
        try:
            codecs.lookup(requested)
            explicit_encoding = requested
        except LookupError:
            explicit_encoding = None
    if explicit_encoding is None and os.environ.get("PYTHONUTF8", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        explicit_encoding = "utf-8"
    console_encoding = _parent_console_encoding()
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            encoding = (
                explicit_encoding
                or console_encoding
                or getattr(stream, "encoding", None)
            )
            reconfigure(encoding=encoding, errors="backslashreplace")
        except (OSError, ValueError):
            # Test runners and embedded hosts may expose a closed/fixed stream.
            # Their own capture layer remains responsible for encoding.
            continue


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
        help="SEARCH 尝试次数上限；本轮 SUPPORT2 验证使用 3",
    )
    parser.add_argument(
        "--ideator-count",
        type=int,
        choices=range(1, 9),
        default=3,
        help="每轮并行 Ideator 数量（1-8；默认 3）",
    )
    parser.add_argument(
        "--hypotheses-per-ideator",
        type=int,
        choices=range(1, 6),
        default=2,
        help="每个 Ideator 的候选目标数（1-5；默认 2）",
    )
    parser.add_argument(
        "--pro-reasoning",
        action="store_true",
        help=(
            "仅默认 SEARCH Ideator 与两类 Rubric 使用 MODEL_PRO，"
            "并开启 DeepSeek thinking"
        ),
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
    # 数据路径预检：拼错/缺失的 --data 应在跑 LLM 之前立刻失败，而非白烧一轮。
    if args.data and not Path(args.data).exists():
        print(f"error: data path does not exist: {args.data}", file=sys.stderr)
        return 2
    task_text = args.task
    if args.data:
        task_text = f"{task_text}\n数据集路径: {args.data}"
    reasoning_model = settings.pro_model_name() if args.pro_reasoning else None
    project_root = Path(args.project)
    resumed = (project_root / ".athena" / "state.json").is_file()
    runtime = ResearchRuntime(
        project_root=project_root,
        model=settings.model_name(),
        reasoning_model=reasoning_model,
        reasoning_thinking=args.pro_reasoning,
        task=task_text,
        auto_validate=True,
        search_limit=args.search_limit,
        ideator_count=args.ideator_count,
        hypotheses_per_ideator=args.hypotheses_per_ideator,
    )
    effective = {
        "search_limit": runtime.state.search_limit,
        "ideator_count": runtime.state.ideator_count,
        "hypotheses_per_ideator": runtime.state.hypotheses_per_ideator,
    }
    requested = {
        "search_limit": args.search_limit,
        "ideator_count": args.ideator_count,
        "hypotheses_per_ideator": args.hypotheses_per_ideator,
    }
    print(
        "[config] "
        f"mode={'resume' if resumed else 'fresh'} "
        f"default_model={settings.model_name()} "
        f"reasoning_model={reasoning_model or 'disabled'} "
        f"reasoning_thinking={'enabled' if args.pro_reasoning else 'disabled'} "
        f"requested_search={requested['search_limit']} "
        f"effective_search={effective['search_limit']} "
        f"requested_ideators={requested['ideator_count']}x"
        f"{requested['hypotheses_per_ideator']} "
        f"effective_ideators={effective['ideator_count']}x"
        f"{effective['hypotheses_per_ideator']}",
        flush=True,
    )
    if effective != requested:
        print(
            "CONFIG ERROR: persisted project settings do not match the requested "
            "SEARCH settings; use the persisted values or start a new project. "
            f"requested={requested} effective={effective}",
            file=sys.stderr,
            flush=True,
        )
        await runtime.aclose()
        return 2
    _subscribe(runtime)
    try:
        supervisor_task = await runtime.start()
        await supervisor_task
    except asyncio.CancelledError:
        stopped = False
        try:
            stopped = await runtime.message("/stop") == "STOPPED"
        except Exception as exc:
            print(
                f"STOP WARNING: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
        message = (
            "workflow state persisted as STOPPED"
            if stopped
            else "STOPPED state could not be confirmed"
        )
        print(f"INTERRUPTED: {message}", flush=True)
        await runtime.aclose()
        return 130
    except Exception as exc:
        print(f"RUN FAILED: {type(exc).__name__}: {exc}", flush=True)
        await runtime.aclose()
        return 1
    state = runtime.state
    print(f"TERMINAL: phase={state.phase} status={state.status}", flush=True)
    await runtime.aclose()
    return 0


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_stdio()
    args = _parse(sys.argv[1:] if argv is None else argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
