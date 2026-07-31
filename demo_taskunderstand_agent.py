"""TaskUnderstandAgent 真实任务端到端测试。

使用真实 LLM API + HuggingFace Hub 在 Titanic 竞赛上测试
TaskUnderstandAgent 的完整 pipeline。

用法::

    # 默认运行（seeded 模式，绕过 Kaggle stub）
    python taskunderstand_agent_demo.py

    # 完全自主 ReAct 模式
    python taskunderstand_agent_demo.py --mode auto

    # 使用国内 HF 镜像（默认启用，无需手动指定）
   python taskunderstand_agent_demo.py --hf-endpoint https://hf-mirror.com

    # 其他参数
    python taskunderstand_agent_demo.py --max-turns 10 --competition "titanic"
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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

# ── Titanic 竞赛预注入上下文（用于 seeded 模式绕过 Kaggle MCP stub）────

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

Here is the competition metadata (ALREADY FETCHED — do NOT call any Kaggle tools):
{titanic_metadata}

CRITICAL RULES (follow strictly to avoid wasting turns on unavailable tools):
1. DO NOT call kaggle_competition_search, kaggle_discussion_search, or
   kaggle_dataset_download — these are NOT available yet.
2. DO NOT call code_execute — the sandbox is not ready. Skip training/inference.
3. USE THESE AVAILABLE TOOLS (they work via LLM):
   - hf_dataset_search / hf_dataset_download: search/download HuggingFace datasets
   - hf_model_search / hf_model_download: search/download HuggingFace models
   - data_analyze: generate an EDA report from the competition metadata
   - data_clean_code_gen: generate a data cleaning script
   - solution_design: design a solution plan with rubric
   - project_code_gen: generate complete project code
   - submission_build: build submission.csv

4. WORKING DIRECTORY: {work_dir}
   - ALL hf_dataset_download calls MUST use output_dir="{work_dir}/datasets"
   - ALL hf_model_download calls MUST use output_dir="{work_dir}/models"
   - ALL generated code MUST reference data paths under "{work_dir}"

5. START by calling hf_dataset_search, hf_model_search, and data_analyze
   in parallel, then proceed through the pipeline based on available results.
"""


def _build_seeded_prompt(work_dir: str) -> str:
    """构建 seeded 模式提示词，注入工作目录路径。"""
    return _TITANIC_SEEDED_PROMPT.format(
        titanic_metadata=json.dumps(_TITANIC_TASK_METADATA, ensure_ascii=False, indent=2),
        work_dir=work_dir,
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
        print(f"   ✅ HF Hub 可达 (endpoint={hf_endpoint}, latency={elapsed:.1f}s)", flush=True)
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
            print(f"\n{'─' * 60}")
            print(f"🔧 [TOOL START] {name}")
            print(f"{'─' * 60}", flush=True)

        elif kind == "tool/end":
            name = _parse_tool_name(event_ref)
            print(f"   ✅ [TOOL OK] {name}")
            print(f"{'─' * 60}\n", flush=True)

        elif kind == "tool/error":
            name = _parse_tool_name(event_ref)
            print(f"   ❌ [TOOL FAIL] {name}")
            print(f"{'─' * 60}\n", flush=True)
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


def _save_generated_artifacts(ctx: AgentContext, work_dir: Path) -> list[Path]:
    """从 Agent 对话记忆中提取生成产物，写入 work_dir/code/ 目录。

    支持的产物类型：
    - project_code_gen → 解析多文件代码块，按文件名保存
    - data_clean_code_gen → 保存为 clean_script.py
    - solution_design → 保存为 solution_plan.json
    - submission_build → 保存为 format_script.py
    """
    import re

    mem = ctx.memory
    if mem is None:
        return []

    saved: list[Path] = []

    for msg in mem.items:
        for p in getattr(msg, "parts", []):
            if getattr(p, "part_kind", None) != "tool-return":
                continue
            content = str(getattr(p, "content", ""))
            tool_call_id = getattr(p, "tool_call_id", "")

            # 找到对应的 tool-call 来确定工具名
            tool_name = ""
            for m2 in mem.items:
                for p2 in getattr(m2, "parts", []):
                    if getattr(p2, "part_kind", None) == "tool-call":
                        if getattr(p2, "tool_call_id", "") == tool_call_id:
                            tool_name = getattr(p2, "tool_name", "")
                            break
                if tool_name:
                    break

            if not tool_name:
                continue

            try:
                data = json.loads(content) if not content.startswith("[ERROR") else None
            except json.JSONDecodeError:
                data = None

            if tool_name == "project_code_gen" and data and "code" in data:
                # 解析 LLM 生成的多文件代码块
                code_text = str(data["code"])
                _extract_code_files(code_text, work_dir / "code", saved)

            elif tool_name == "data_clean_code_gen" and data and "clean_script" in data:
                path = work_dir / "code" / "clean_script.py"
                path.write_text(str(data["clean_script"]), encoding="utf-8")
                saved.append(path)

            elif tool_name == "solution_design" and data and "solution_plan" in data:
                path = work_dir / "code" / "solution_plan.json"
                path.write_text(
                    json.dumps(data["solution_plan"], ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                saved.append(path)

            elif tool_name == "submission_build" and data and "format_script" in data:
                path = work_dir / "code" / "format_script.py"
                path.write_text(str(data["format_script"]), encoding="utf-8")
                saved.append(path)

    return saved


def _extract_code_files(code_text: str, out_dir: Path, saved: list[Path]) -> None:
    """从 LLM 生成的代码文本中提取  ``## filename`` 或 ````py ... ``` 块。"""
    import re

    # 模式 1: ## filename.py 后跟 ``` 代码块
    pattern1 = re.compile(
        r'##\s*(\S+\.(?:py|yaml|yml))\s*\n\s*```(?:python|yaml)?\s*\n(.*?)```',
        re.DOTALL,
    )
    for match in pattern1.finditer(code_text):
        fname = match.group(1)
        code = match.group(2).strip()
        path = out_dir / fname
        path.write_text(code + "\n", encoding="utf-8")
        saved.append(path)

    # 模式 2: 裸 ```python / ```yaml 代码块（无显式文件名时按序号命名）
    if not saved:
        pattern2 = re.compile(
            r'```(?:python|yaml)?\s*\n(.*?)```',
            re.DOTALL,
        )
        blocks = pattern2.findall(code_text)
        for i, block in enumerate(blocks):
            # 跳过太短的块（可能是内联示例）
            if len(block.strip()) < 50:
                continue
            ext = "yaml" if "yaml" in code_text[: code_text.index(block)].rsplit("```", 1)[0] else "py"
            path = out_dir / f"generated_{i + 1}.{ext}"
            path.write_text(block.strip() + "\n", encoding="utf-8")
            saved.append(path)


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

    # 创建工作目录结构
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "datasets").mkdir(exist_ok=True)
    (work_dir / "models").mkdir(exist_ok=True)
    (work_dir / "code").mkdir(exist_ok=True)

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
        agent = build_task_understand_agent(
            model=model,
            client=client,
            max_turns=max_turns,
        )
        print(f"✅ Agent 构建完成，已注册 {len(agent.config.tools)} 个工具\n")
    except Exception as exc:
        print(f"\n❌ Agent 构建失败: {exc}")
        return 1

    # ── 4. 构建上下文和提示词 ──
    if mode == "seeded":
        user_prompt = _build_seeded_prompt(str(work_dir))
    else:
        user_prompt = (
            f"I want to compete in the Kaggle competition: {competition}. "
            f"Please follow your pipeline: search competition info, "
            f"download data, search for augmentation datasets and models, "
            f"analyze data, design a solution, generate code, and build a submission. "
            f"If any tool returns an error, skip it and continue with what you have. "
            f"Do NOT retry a failed tool more than once."
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

    # ── 7. 从记忆中提取生成产物并落盘 ──
    _save_generated_artifacts(ctx, work_dir)

    # ── 8. 保存产物 ──
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

    # ── 9. 控制台报告 ──
    print(f"\n📊 工具调用统计:")
    if tool_stats:
        for name, stats in sorted(tool_stats.items()):
            s, f = stats["success"], stats["failed"]
            icon = "✅" if f == 0 else ("⚠️" if s > 0 else "❌")
            print(f"   {icon} {name}: {s} success, {f} failed")
    else:
        print("   (无工具调用记录)")

    print(f"\n📁 工作目录产物: {work_dir}")
    for category in ["datasets", "models", "code"]:
        cat_dir = work_dir / category
        if cat_dir.exists():
            files = list(cat_dir.iterdir())
            if files:
                print(f"   {category}/: {len(files)} 个文件")
                for f in sorted(files):
                    size_kb = f.stat().st_size / 1024
                    print(f"      - {f.name} ({size_kb:.1f} KB)")
            else:
                print(f"   {category}/: (空)")

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
        default="seeded",
        help=(
            '运行模式: "seeded" 绕过 Kaggle stub 预注入竞赛信息（默认），'
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
