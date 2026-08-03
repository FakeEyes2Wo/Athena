"""TaskUnderstandAgent 真实任务端到端测试。

使用真实 LLM API + HuggingFace Hub 在 Titanic 竞赛上测试
TaskUnderstandAgent 的完整 pipeline。

所有工具在 execute() 阶段直接将产物写入 {work_dir}/{tool_name}/ 子目录，
不再依赖对话历史提取。

用法::

    # 默认运行（seeded 模式，绕过 MCP 外部工具）
    python demo_taskunderstand_agent.py

    # 后面的先不要尝试，没测试过
    # 完全自主 ReAct 模式
    python demo_taskunderstand_agent.py --mode auto

    # 使用国内 HF 镜像（默认启用，无需手动指定）
    python demo_taskunderstand_agent.py --hf-endpoint https://hf-mirror.com

    # 其他参数
    python demo_taskunderstand_agent.py --max-turns 10 --competition "titanic"
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI

from athena.agents.competition.task_understand_agent import (
    build_task_understand_agent,
)
from athena.core.agent.agent import AgentContext
from athena.core.schemas import AthenaThread, AthenaTurn

# ── 项目根目录 & 默认值 ────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_HF_ENDPOINT = "https://hf-mirror.com"  # 国内镜像，无需翻墙
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "demo_output"
DEFAULT_WORK_DIR = PROJECT_ROOT / "demo_work"  # 数据集、模型、生成代码的存放目录

# ── Titanic 竞赛预注入上下文（用于 seeded 模式绕过 MCP 外部工具）────

_TITANIC_TASK_METADATA: dict = {
    "task_type": "binary_classification",
    "data_type": "tabular",
    "target_vars": ["Survived"],
    "primary_metric": {"name": "accuracy", "direction": "maximize"},
    "constraints": [
        "submission.csv must contain PassengerId and Survived columns",
        "Survived must be 0 or 1",
    ],
    "source_url": "https://www.kaggle.com/c/titanic",
    "description_text": (
        "Titanic: Machine Learning from Disaster. "
        "Predict survival on the Titanic using passenger data "
        "(age, sex, class, fare, etc.). Binary classification."
    ),
}

_TITANIC_SEEDED_PROMPT = """\
I want to participate in the Titanic competition on Kaggle.
Here is the competition metadata:
{titanic_metadata}
CRITICAL RULES (follow strictly to avoid wasting turns on unavailable tools):
2. DO NOT call code_execute — the sandbox is not ready. Skip training/inference

EXECUTION STRATEGY:
- acquire data first, then analyze, then design, then code.
- Do NOT batch tools from different phases in the same turn. For example,
  do not call data_analyze together with hf_dataset_search — wait until
  you know whether data was found and downloaded.
- After each phase, review the results before deciding the next step.
  If a data search returns nothing, you may still proceed with what you
  have, but do not fabricate references to non-existent data.

"""


def _build_seeded_prompt() -> str:
    """构建 seeded 模式提示词，预注入 Titanic 竞赛元数据。"""
    return _TITANIC_SEEDED_PROMPT.format(
        titanic_metadata=json.dumps(_TITANIC_TASK_METADATA, ensure_ascii=False, indent=2),
    )

# ── 工具函数 ────────────────────────────────────────────────────────────


def _parse_tool_name(event_ref: str) -> str:
    """从 event_ref 中提取工具名。

    event_ref 格式: ``ev:<turn_id>:<tool_name>:begin`` 或 ``ev:<turn_id>:<tool_name>:end``
    """
    if ":" not in event_ref:
        return event_ref
    parts = event_ref.split(":")
    # 倒数第一个是 begin/end/error，倒数第二个是工具名
    if len(parts) >= 2:
        return parts[-2] if parts[-1] in ("begin", "end", "error") else parts[-1]
    return parts[-1]


def load_api_config() -> tuple[str, str, str]:
    """加载 LLM API 配置。

    优先级：环境变量 > .env 文件 > 默认值。
    支持 DEEPSEEK_* 和 OPENAI_* 两套命名。
    """
    load_dotenv(PROJECT_ROOT / ".env")

    api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
    base_url = (
        os.getenv("DEEPSEEK_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or DEFAULT_BASE_URL
    )
    model = os.getenv("DEEPSEEK_MODEL") or os.getenv("OPENAI_MODEL") or DEFAULT_MODEL

    # 同时设置 OPENAI_* 环境变量，供 single_turn_chat 和 ResponsesProvider 使用
    if api_key and not os.getenv("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = api_key
    if base_url and not os.getenv("OPENAI_BASE_URL"):
        os.environ["OPENAI_BASE_URL"] = base_url
    if model and not os.getenv("OPENAI_MODEL"):
        os.environ["OPENAI_MODEL"] = model

    return model, base_url, api_key


def create_client(base_url: str, api_key: str) -> AsyncOpenAI:
    """创建 OpenAI 兼容的异步客户端。"""
    return AsyncOpenAI(api_key=api_key, base_url=base_url)


# ── 启动诊断 ────────────────────────────────────────────────────────────


async def run_diagnostics(
    model: str, base_url: str, api_key: str, hf_endpoint: str
) -> dict:
    """在正式运行前检测各组件连通性。"""
    results: dict[str, dict] = {}

    # 1. LLM API 连通性
    print("🔍 检测 LLM API 连通性...", flush=True)
    try:
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        t0 = time.monotonic()
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=5,
            ),
            timeout=15.0,
        )
        elapsed = time.monotonic() - t0
        results["llm_api"] = {
            "ok": True,
            "latency_s": round(elapsed, 2),
            "model": model,
        }
        print(f"   ✅ LLM API 可达 (latency={elapsed:.1f}s)", flush=True)
    except Exception as exc:
        results["llm_api"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        print(f"   ❌ LLM API 不可达: {exc}", flush=True)

    # 2. HuggingFace Hub 连通性
    print("🔍 检测 HuggingFace Hub 连通性...", flush=True)
    try:
        from huggingface_hub import HfApi

        hf = HfApi(endpoint=hf_endpoint)
        t0 = time.monotonic()
        ds = await asyncio.wait_for(
            asyncio.to_thread(lambda: list(hf.list_datasets(limit=1))),
            timeout=15.0,
        )
        elapsed = time.monotonic() - t0
        results["hf_hub"] = {
            "ok": True,
            "latency_s": round(elapsed, 2),
            "endpoint": hf_endpoint,
            "sample_count": len(ds),
        }
        print(f"✅ HF Hub 可达 (endpoint={hf_endpoint}, latency={elapsed:.1f}s)", flush=True)
    except Exception as exc:
        results["hf_hub"] = {
            "ok": False,
            "endpoint": hf_endpoint,
            "error": f"{type(exc).__name__}: {exc}",
        }
        print(f"   ❌ HF Hub 不可达: {exc}", flush=True)

    return results


# ── 事件输出 ────────────────────────────────────────────────────────────


def emit_factory(output_dir: Path):
    """创建 emit 闭包，将事件打印到控制台并写入日志文件。"""
    log_path = output_dir / "agent_events.jsonl"
    output_dir.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 — 脚本结束时关闭

    # 收集每个 turn 的完整 LLM 文本，用于最终报告
    turn_texts: dict[str, list[str]] = {}
    # 收集工具调用的错误信息（从 emit event_ref 推断）
    tool_errors: list[dict] = []

    async def emit(kind: str, event_ref: str, data: dict | None = None) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        record = {"ts": ts, "kind": kind, "ref": event_ref, "data": data}
        log_fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        log_fh.flush()

        if kind == "agent/text_delta" and data:
            delta = data.get("delta", "")
            turn_texts.setdefault(event_ref, []).append(delta)
            print(delta, end="", flush=True)

        elif kind == "tool/begin":
            name = _parse_tool_name(event_ref)
            print("\n",'-'*20)
            print(f"\n🔧 [TOOL START] {name}")

        elif kind == "tool/end":
            name = _parse_tool_name(event_ref)
            print(f"✅ [TOOL OK] {name}")

        elif kind == "tool/error":
            name = _parse_tool_name(event_ref)
            print(f"❌ [TOOL FAIL] {name}")
            print('-'*20)
            tool_errors.append({"tool": name, "ref": event_ref, "ts": ts})

    return emit, log_fh, turn_texts, tool_errors


# ── 内存转储（用于诊断工具错误详情）────────────────────────────────────


def dump_memory_diagnostic(ctx: AgentContext, output_dir: Path) -> Path:
    """将 Agent 的对话记忆（包括工具返回的错误详情）写入诊断文件。"""
    diag_path = output_dir / "memory_dump.jsonl"
    mem = ctx.memory
    if mem is None:
        diag_path.write_text("(no memory — agent did not run)\n", encoding="utf-8")
        return diag_path

    with open(diag_path, "w", encoding="utf-8") as fh:
        for i, msg in enumerate(mem.items):
            # 序列化 PydanticAI ModelMessage
            kind = type(msg).__name__
            parts_info = []
            for p in getattr(msg, "parts", []):
                pk = getattr(p, "part_kind", "?")
                if pk == "tool-return":
                    content = str(getattr(p, "content", ""))[:500]
                    tool_call_id = getattr(p, "tool_call_id", "")
                    parts_info.append({
                        "kind": pk,
                        "tool_call_id": tool_call_id,
                        "content_preview": content,
                    })
                elif pk == "tool-call":
                    parts_info.append({
                        "kind": pk,
                        "tool_name": getattr(p, "tool_name", ""),
                        "args": str(getattr(p, "args", ""))[:300],
                    })
                elif pk in ("system-prompt", "user-prompt", "text"):
                    c = str(getattr(p, "content", ""))
                    parts_info.append({
                        "kind": pk,
                        "content": c[:500],
                    })
                else:
                    parts_info.append({"kind": pk})
            record = {"index": i, "type": kind, "parts": parts_info}
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    return diag_path


# ── 主流程 ───────────────────────────────────────────────────────────────


async def run_demo(
    *,
    mode: str = "seeded",
    competition: str = "titanic",
    max_turns: int = 15,
    hf_endpoint: str = DEFAULT_HF_ENDPOINT,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    work_dir: Path = DEFAULT_WORK_DIR,
    skip_diagnostics: bool = False,
) -> int:
    """运行 TaskUnderstandAgent 真实任务测试。"""
    # ── 1. 加载配置 ──
    model, base_url, api_key = load_api_config()
    if not api_key:
        print("=" * 70)
        print("❌ 未找到 API key。")
        print()
        print("   方式 A — 创建 .env 文件（推荐）：")
        print("     echo DEEPSEEK_API_KEY=sk-your-key > .env")
        print("     echo DEEPSEEK_BASE_URL=https://api.deepseek.com >> .env")
        print()
        print("   方式 B — 设置环境变量：")
        print("     set DEEPSEEK_API_KEY=sk-your-key")
        print("=" * 70)
        return 1

    # 配置 HF 镜像端点（解决国内访问 huggingface.co 的问题）
    os.environ["HF_ENDPOINT"] = hf_endpoint

    # 创建工作目录
    work_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("🧪 TaskUnderstandAgent 真实任务测试")
    print("=" * 70)
    print(f"  Model:        {model}")
    print(f"  Base URL:     {base_url}")
    print(f"  Competition:  {competition}")
    print(f"  Mode:         {mode}")
    print(f"  Max Turns:    {max_turns}")
    print(f"  HF Endpoint:  {hf_endpoint}")
    print(f"  Work Dir:     {work_dir}")
    print(f"  Output Dir:   {output_dir}")
    print("=" * 70)

    # ── 2. 启动诊断 ──
    if not skip_diagnostics:
        diag = await run_diagnostics(model, base_url, api_key, hf_endpoint)
        llm_ok = diag.get("llm_api", {}).get("ok", False)
        hf_ok = diag.get("hf_hub", {}).get("ok", False)
        if not llm_ok:
            print("\n⚠️  LLM API 不可达，工具调用将全部失败。请检查 API key 和网络。")
        if not hf_ok:
            print(
                f"\n⚠️  HF Hub 不可达 (endpoint={hf_endpoint})。"
                f" 尝试其他镜像: --hf-endpoint https://hf-mirror.com"
            )
        print()

    # ── 3. 创建 Agent ──
    client = create_client(base_url, api_key)
    emit, log_fh, turn_texts, tool_errors = emit_factory(output_dir)

    try:
        agent = await build_task_understand_agent(
            model=model,
            client=client,
            work_root=str(work_dir),
            max_turns=max_turns,
        )
        print(f"✅ Agent 构建完成，已注册 {len(agent.config.tools)} 个工具（产物目录: {work_dir}）\n")
    except Exception as exc:
        print(f"\n❌ Agent 构建失败: {exc}")
        return 1

    # ── 4. 构建上下文和提示词 ──
    if mode == "seeded":
        user_prompt = _build_seeded_prompt()
        print(f"these are user's propmt, print for debug:\n{user_prompt}")
    else:
        user_prompt = (
            f"I want to compete in the Kaggle competition: {competition}. "
            f"Please follow your pipeline: search competition info, "
            f"download data, search for augmentation datasets and models, "
            # f"analyze data, design a solution, generate code, and build a submission. "
            # f"If any tool returns an error, skip it and continue with what you have. "
            # f"Do NOT retry a failed tool more than once."
        )

    thread = AthenaThread(
        thread_id=f"demo-{competition}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}",
        session_id="demo-session",
        status="running",
        context_ref="context://demo",
    )
    turn = AthenaTurn(
        turn_id=f"turn-{thread.thread_id}",
        thread_id=thread.thread_id,
        request_ref=user_prompt,
        status="running",
    )
    ctx = AgentContext(
        thread=thread,
        turn=turn,
        emit=emit,
        tools=agent.config.tools,
        cancel=asyncio.Event(),
    )

    # ── 5. 运行 Agent ──
    print(f"{'=' * 70}")
    print(f"🚀 开始执行 Agent（mode={mode}）...")
    print(f"{'=' * 70}\n")
    t_start = time.monotonic()
    print(f"📝 用户提示词:\n{user_prompt}\n")

    try:
        outcome = await agent.run(ctx)
    except asyncio.CancelledError:
        print("\n⚠️  Agent 被取消")
        return 1
    except Exception as exc:
        print(f"\n❌ Agent 运行异常: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        return 1

    elapsed = time.monotonic() - t_start
    print(f"\n{'=' * 70}")
    print(f"🏁 Agent 执行完成 (耗时 {elapsed:.0f}s)")
    print(f"   result_ref:       {outcome.result_ref}")
    print(f"   next_context_ref: {outcome.next_context_ref}")
    print(f"{'=' * 70}")

    # ── 6. 工具调用统计 ──
    # 从 event log 中统计
    event_path = output_dir / "agent_events.jsonl"
    tool_stats: dict[str, dict] = {}
    if event_path.exists():
        for line in event_path.read_text(encoding="utf-8").strip().split("\n"):
            try:
                rec = json.loads(line)
                kind = rec.get("kind", "")
                ref = rec.get("ref", "")
                if kind in ("tool/end", "tool/error"):
                    name = _parse_tool_name(ref)
                    if name not in tool_stats:
                        tool_stats[name] = {"success": 0, "failed": 0}
                    if kind == "tool/end":
                        tool_stats[name]["success"] += 1
                    else:
                        tool_stats[name]["failed"] += 1
            except json.JSONDecodeError:
                pass

    # ── 7. 保存产物 ──
    # 对话记忆诊断（包含工具错误的完整信息）
    memory_dump_path = dump_memory_diagnostic(ctx, output_dir)

    # 运行摘要
    summary = {
        "competition": competition,
        "mode": mode,
        "model": model,
        "base_url": base_url,
        "hf_endpoint": hf_endpoint,
        "max_turns": max_turns,
        "elapsed_s": round(elapsed, 1),
        "result_ref": outcome.result_ref,
        "next_context_ref": outcome.next_context_ref,
        "thread_id": thread.thread_id,
        "turn_id": turn.turn_id,
        "tool_stats": {
            name: {
                "success": s["success"],
                "failed": s["failed"],
                "total": s["success"] + s["failed"],
            }
            for name, s in sorted(tool_stats.items())
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    summary_path = output_dir / "run_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 8. 控制台报告 ──
    print(f"\n📊 工具调用统计:")
    if tool_stats:
        for name, stats in sorted(tool_stats.items()):
            s, f = stats["success"], stats["failed"]
            icon = "✅" if f == 0 else ("⚠️" if s > 0 else "❌")
            print(f"   {icon} {name}: {s} success, {f} failed")
    else:
        print("   (无工具调用记录)")

    print(f"\n📁 工具产物目录: {work_dir}")
    # 扫描各工具子目录（每个工具写入 {work_dir}/{tool_name}/）
    if work_dir.exists():
        for tool_dir in sorted(work_dir.iterdir()):
            if not tool_dir.is_dir():
                continue
            files = list(tool_dir.iterdir())
            if files:
                print(f"   {tool_dir.name}/: {len(files)} 个文件")
                for f in sorted(files)[:10]:  # 最多显示 10 个
                    if f.is_file():
                        size_kb = f.stat().st_size / 1024
                        print(f"      - {f.name} ({size_kb:.1f} KB)")
                    elif f.is_dir():
                        print(f"      - {f.name}/ (目录)")
                if len(files) > 10:
                    print(f"      ... 及其他 {len(files) - 10} 个条目")
            else:
                print(f"   {tool_dir.name}/: (空)")

    print(f"\n📄 日志文件:")
    print(f"   运行摘要:   {summary_path}")
    print(f"   事件日志:   {event_path}")
    print(f"   内存诊断:   {memory_dump_path}")
    print(f"   查看工具错误详情: cat {memory_dump_path} | grep tool-return")

    log_fh.close()
    return 0


# ── CLI 入口 ─────────────────────────────────────────────────────────────


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="TaskUnderstandAgent 真实任务端到端测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
示例:
    python taskunderstand_agent_demo.py
    python taskunderstand_agent_demo.py --mode auto
    python taskunderstand_agent_demo.py --max-turns 10
    python taskunderstand_agent_demo.py --hf-endpoint https://hf-mirror.com

配置要求:
    项目根目录需要有 .env 文件，包含 DEEPSEEK_API_KEY。
        """,
    )
    parser.add_argument(
        "--mode",
        choices=["auto", "seeded"],
        default="auto",
        help=(
            '运行模式: "seeded" 绕过 MCP 外部工具，预注入竞赛信息（默认），'
            '"auto" 完全自主 ReAct 循环'
        ),
    )
    parser.add_argument(
        "--competition",
        type=str,
        default="titanic",
        help='竞赛名称或 URL（默认: "titanic"）',
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=15,
        help="Agent ReAct loop 最大轮次（默认: 15）",
    )
    parser.add_argument(
        "--hf-endpoint",
        type=str,
        default=DEFAULT_HF_ENDPOINT,
        help=f"HuggingFace Hub 端点（默认: {DEFAULT_HF_ENDPOINT}，国内可用镜像）",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(DEFAULT_OUTPUT_DIR),
        help="产物输出目录",
    )
    parser.add_argument(
        "--work-dir",
        type=str,
        default=str(DEFAULT_WORK_DIR),
        help=f"工作目录：数据集、模型、生成代码存放位置（默认: {DEFAULT_WORK_DIR}）",
    )
    parser.add_argument(
        "--skip-diagnostics",
        action="store_true",
        help="跳过启动前的连通性诊断",
    )

    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    work_dir = Path(args.work_dir)

    exit_code = asyncio.run(
        run_demo(
            mode=args.mode,
            competition=args.competition,
            max_turns=args.max_turns,
            hf_endpoint=args.hf_endpoint,
            output_dir=output_dir,
            work_dir=work_dir,
            skip_diagnostics=args.skip_diagnostics,
        )
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
