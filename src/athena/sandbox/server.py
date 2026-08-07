"""Athena Sandbox MCP Server —— 通过 stdio 暴露 python_inspect/python_execute/sandbox_config。"""

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from athena.sandbox.executor import SandboxExecutor
from athena.sandbox.limits import SandboxLimits


def create_sandbox_server(work_root: str) -> FastMCP:
    """创建并配置 Sandbox MCP Server。

    Args:
        work_root: 沙箱工作根目录绝对路径

    Returns:
        配置完成的 FastMCP 实例，已注册 3 个工具
    """
    mcp = FastMCP("athena-sandbox")
    executor = SandboxExecutor(work_root=Path(work_root))

    @mcp.tool()
    async def python_inspect(expr: str, cwd: str = ".", timeout: int = 10) -> dict:
        """执行单行 Python 表达式并返回其值的摘要。

        用于快速探索数据：df.head(), df.describe(), len(df), os.listdir('.') 等。
        预置 pd (pandas) 和 np (numpy)。

        Args:
            expr: Python 表达式
            cwd: 工作目录（相对 work_root）
            timeout: 超时秒数，默认 10，最大 30
        """
        result = await executor.inspect(expr=expr, cwd=cwd, timeout=timeout)
        return {
            "ok": result.ok,
            "value": result.value,
            "type": result.type,
            "shape": result.shape,
            "columns": result.columns,
            "error": result.error,
            "traceback": result.traceback,
            "duration_ms": result.duration_ms,
        }

    @mcp.tool()
    async def python_execute(script: str, cwd: str = ".", timeout: int = 60) -> dict:
        """执行 Python 脚本并返回 stdout/stderr 和产出文件列表。

        用于数据清洗、特征工程、画图保存等需要多行代码的任务。
        预置 pd, np, plt (Agg 后端), sns, sklearn。

        Args:
            script: Python 脚本（代码块）
            cwd: 工作目录（相对 work_root）
            timeout: 超时秒数，默认 60，最大 120
        """
        result = await executor.execute(script=script, cwd=cwd, timeout=timeout)
        return {
            "ok": result.ok,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "output_files": result.output_files,
            "error": result.error,
            "traceback": result.traceback,
            "duration_ms": result.duration_ms,
            "memory_mb": result.memory_mb,
        }

    @mcp.tool()
    async def sandbox_config() -> dict:
        """查询 sandbox 配置：work_root 路径、允许的 import 列表、超时限制等。"""
        return {
            "work_root": str(executor.work_root),
            "whitelist": sorted(executor.allowed_imports),
            "timeout_max": {"inspect": 30, "execute": 120},
            "memory_limit_mb": SandboxLimits.MEMORY_MB,
        }

    return mcp
