"""探测后端模型真正接受的 ``max_tokens`` 上限，以及它实际肯写多长。

为什么需要这个：``AgentConfig.max_tokens`` 原本写死 4096，写文件的 Agent 一旦
生成超过这个长度的 ``write_file`` 实参，工具调用的 JSON 会被从中间切断，参数解析
失败。调大是对的，但**不能瞎调**——配得比模型支持的还大，网关会直接 400，整轮跑不起来。

用法（在 Athena/ 下，.env 已配好）::

    uv run python scripts/probe_max_tokens.py
    uv run python scripts/probe_max_tokens.py --model qwen3.7-plus

输出两个数：

1. ``accepted`` —— 网关愿意接受的最大 ``max_tokens`` 参数值（二分探得）。
   这是能往 ``config.toml`` 的 ``[llm].max_tokens`` 里填的上界。
2. ``emitted`` —— 模型被要求一直写时，单次响应实际吐出的 token 数。
   真正决定"一次能写多大文件"的是这个数，通常远小于 ``accepted``。

参数值合法不代表模型写得出那么长。按 ``emitted`` 来估计单次 write_file 的安全体量：
JSON 转义会让换行、引号、反斜杠各多占一个字符，中文按 UTF-8 计费更贵，所以
**留一半余量**是合理的。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from athena.core.agent import settings

# 二分上界：目前公开模型的输出上限都在这之下；调高只会多花几次 400。
CEILING = 131_072
FLOOR = 1_024


async def _accepts(client, model: str, value: int) -> bool:
    """网关是否接受这个 max_tokens 参数值（只看是否 400，不看内容）。"""
    try:
        await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=value,
        )
    except Exception as exc:  # noqa: BLE001 - 探测脚本，任何拒绝都算不接受
        text = f"{type(exc).__name__}: {exc}".lower()
        if "max_tokens" in text or "400" in text or "invalid" in text:
            return False
        raise
    return True


async def _emitted(client, model: str, cap: int) -> int:
    """让模型一直写，数它单次响应实际吐了多少 token。"""
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": (
                    "Write a long, detailed technical report in Markdown about "
                    "time-series feature engineering. Include many sections and "
                    "tables. Do not stop early; keep writing until you are cut off."
                ),
            }
        ],
        max_tokens=cap,
    )
    usage = getattr(resp, "usage", None)
    return int(getattr(usage, "completion_tokens", 0) or 0)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=None, help="默认取 settings.model_name()")
    args = parser.parse_args()

    model = args.model or settings.model_name()
    client = settings.get_client()
    print(f"model    : {model}")
    print(f"base_url : {settings.base_url()}")
    print(f"当前配置 : max_tokens = {settings.max_tokens()}")
    print()

    if not await _accepts(client, model, FLOOR):
        print(f"连 {FLOOR} 都不接受——先检查网关和模型名是不是对的。")
        return 1

    lo, hi = FLOOR, CEILING
    if await _accepts(client, model, hi):
        lo = hi
    else:
        # 不变式：lo 一定接受，hi 一定不接受。
        while hi - lo > 256:
            mid = (lo + hi) // 2
            if await _accepts(client, model, mid):
                lo = mid
            else:
                hi = mid
            print(f"  二分中… 接受 {lo}，拒绝 {hi}")
    print(f"\naccepted : {lo}  （config.toml [llm].max_tokens 的上界）")

    emitted = await _emitted(client, model, lo)
    print(f"emitted  : {emitted}  （单次响应实际写出的 token）")

    if emitted:
        safe = emitted // 2
        print(
            f"\n建议：[llm].max_tokens 配 {min(lo, emitted * 2)} 左右；"
            f"单次 write_file 的内容控制在约 {safe} token 以内，"
            "更长的文件分多次追加写。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
