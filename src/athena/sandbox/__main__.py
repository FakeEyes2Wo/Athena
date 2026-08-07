"""python -m athena.sandbox 入口。

启动 stdio MCP server，等待 Agent 连接。
"""

import argparse

from athena.sandbox.lifecycle import ProcessLifecycle
from athena.sandbox.server import create_sandbox_server

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
