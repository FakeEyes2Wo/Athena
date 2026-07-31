"""HF 工具真实环境测试 —— 直接调用 hf_dataset_search 和 hf_model_search。

用法::

    D:/Softwares/Miniconnda/envs/athena/python.exe hf_demo.py
    D:/Softwares/Miniconnda/envs/athena/python.exe hf_demo.py --endpoint https://hf-mirror.com
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

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_HF_ENDPOINT = "https://hf-mirror.com"

load_dotenv(PROJECT_ROOT / ".env")
os.environ.setdefault("HF_ENDPOINT", DEFAULT_HF_ENDPOINT)


async def _noop_emit(_kind: str, _ref: str, _data: dict | None = None) -> None:
    """空的 async emit 函数，满足 ToolContext.emit 的 Awaitable 类型要求。"""
    pass


async def test_hf_dataset_search(endpoint: str) -> dict:
    """测试 hf_dataset_search 工具。"""
    from athena.tools.hf_dataset import HFDatasetSearchTool
    from athena.core.tool_types import ToolContext

    tool = HFDatasetSearchTool()
    ctx = ToolContext(
        "hf_dataset_search", "test-ds-search",
        emit=_noop_emit,
        cancel=asyncio.Event(),
    )

    print("🔍 搜索 HuggingFace 数据集")
    t0 = time.monotonic()
    result = await tool.ainvoke(ctx, task_keywords="titanic", modality="tabular", n_results=5)
    elapsed = time.monotonic() - t0

    if result.success and result.data:
        ds_list = result.data.get("datasets", [])
        print(f"   ✅ 成功! 找到 {len(ds_list)} 个数据集 (耗时 {elapsed:.1f}s)")
        for ds in ds_list[:5]:
            print(f"      - {ds.get('id', '?')}: {str(ds.get('description', ''))[:80]}")
    else:
        print(f"   ❌ 失败: {result.error} (耗时 {elapsed:.1f}s)")

    return {"tool": "hf_dataset_search", "success": result.success, "elapsed_s": round(elapsed, 1), "count": len(result.data.get("datasets", [])) if result.data else 0, "error": result.error}


async def test_hf_model_search(endpoint: str) -> dict:
    """测试 hf_model_search 工具。"""
    from athena.tools.hf_model import HFModelSearchTool
    from athena.core.tool_types import ToolContext

    tool = HFModelSearchTool()
    ctx = ToolContext(
        "hf_model_search", "test-model-search",
        emit=_noop_emit,
        cancel=asyncio.Event(),
    )

    print("🔍 搜索 HuggingFace 模型: task='tabular classification', arch='xgboost'")
    t0 = time.monotonic()
    result = await tool.ainvoke(ctx, task_type="tabular_classification", modality="tabular", architecture_hint="xgboost", n_results=5)
    elapsed = time.monotonic() - t0

    if result.success and result.data:
        models = result.data.get("models", [])
        print(f"   ✅ 成功! 找到 {len(models)} 个模型 (耗时 {elapsed:.1f}s)")
        for m in models[:5]:
            print(f"      - {m.get('id', '?')}: pipeline={m.get('pipeline_tag', '?')}, downloads={m.get('downloads', 0)}")
    else:
        print(f"   ❌ 失败: {result.error} (耗时 {elapsed:.1f}s)")

    return {"tool": "hf_model_search", "success": result.success, "elapsed_s": round(elapsed, 1), "count": len(result.data.get("models", [])) if result.data else 0, "error": result.error}


async def run_tests(endpoint: str) -> int:
    print("=" * 60)
    print("🧪 HF 工具真实环境测试")
    print(f"   HF Endpoint: {endpoint}")
    print(f"   时间:         {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)
    print()

    # 1) 连通性检查
    print("📡 检查 HF Hub 连通性...")
    try:
        from huggingface_hub import HfApi
        hf = HfApi(endpoint=endpoint)
        t0 = time.monotonic()
        ds = await asyncio.wait_for(
            asyncio.to_thread(lambda: list(hf.list_datasets(limit=1))),
            timeout=15.0,
        )
        elapsed = time.monotonic() - t0
        print(f"   ✅ HF Hub 可达 (latency={elapsed:.1f}s, {len(ds)} datasets returned)")
    except Exception as exc:
        print(f"   ❌ HF Hub 不可达: {exc}")
        print(f"   请尝试其他端点: --endpoint https://huggingface.co (需要代理)")
        return 1

    print()

    # 2) 测试工具
    results = []
    results.append(await test_hf_dataset_search(endpoint))
    print()
    results.append(await test_hf_model_search(endpoint))

    # 3) 总结
    print()
    print("=" * 60)
    print("📊 测试结果:")
    for r in results:
        icon = "✅" if r["success"] else "❌"
        print(f"   {icon} {r['tool']}: {r['count']} results, {r['elapsed_s']}s")
        if r["error"]:
            print(f"      error: {r['error'][:200]}")
    print("=" * 60)

    all_ok = all(r["success"] for r in results)
    return 0 if all_ok else 1


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="HF 工具真实环境测试")
    parser.add_argument(
        "--endpoint",
        type=str,
        default=os.getenv("HF_ENDPOINT", DEFAULT_HF_ENDPOINT),
        help=f"HF Hub 端点 (默认: {DEFAULT_HF_ENDPOINT})",
    )
    args = parser.parse_args()

    os.environ["HF_ENDPOINT"] = args.endpoint
    sys.exit(asyncio.run(run_tests(args.endpoint)))


if __name__ == "__main__":
    main()
