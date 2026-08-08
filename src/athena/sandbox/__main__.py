"""python -m athena.sandbox 入口。

启动 stdio MCP server，等待 Agent 连接。
"""

import argparse
import logging
import sys

# 强制 stderr 使用 UTF-8 编码，避免 Windows GBK 终端中文乱码
sys.stderr.reconfigure(encoding="utf-8")

from athena.sandbox.lifecycle import ProcessLifecycle
from athena.sandbox.server import create_sandbox_server

# 配置日志输出到 stderr（MCP stdio transport 中 stdout 被 JSON-RPC 占用）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-30s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
# 抑制 MCP 内部协议日志噪音（只显示 WARNING 及以上）
logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.WARNING)
logging.getLogger("mcp.client.stdio").setLevel(logging.WARNING)

def main() -> None:
    """解析 CLI 参数并启动 MCP stdio server。"""
    parser = argparse.ArgumentParser(description="Athena Sandbox MCP Server")
    parser.add_argument(
        "--work-root",
        default=".",
        help="沙箱工作根目录（默认当前目录）",
    )
    args = parser.parse_args()

    # 父进程退出时自身被清理
    ProcessLifecycle.bind_to_parent()

    mcp = create_sandbox_server(work_root=args.work_root)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
