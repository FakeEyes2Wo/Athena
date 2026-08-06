"""Script and notebook execution in subprocess."""

import asyncio
import os
import subprocess

from athena.code.types import ExecutionOutput


async def run_script(
    script_path: str, cwd: str, timeout_s: int = 600
) -> ExecutionOutput:
    """Execute a Python script via subprocess. Capture stdout/stderr/files."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "python",
            script_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        files = _list_new_files(cwd)
        return ExecutionOutput(
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            returncode=proc.returncode or 0,
            files=files,
        )
    except asyncio.TimeoutError:
        # 脚本执行超时 → 终止进程并返回 TIMEOUT 错误
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            # 终止/等待子进程失败（进程可能已退出）→ 吞掉，超时结果已确定
            pass
        return ExecutionOutput(
            stdout="",
            stderr=f"TIMEOUT: script exceeded {timeout_s}s",
            returncode=-1,
            files=[],
        )
    except Exception as exc:
        return ExecutionOutput(
            stdout="",
            stderr=str(exc),
            returncode=-1,
            files=[],
        )


def _list_new_files(cwd: str) -> list[str]:
    """列出 cwd 下的所有文件（相对路径）。"""
    result = []
    for root, dirs, files in os.walk(cwd):
        for f in files:
            full = os.path.join(root, f)
            result.append(os.path.relpath(full, cwd))
    return result


if __name__ == "__main__":
    import tempfile

    async def _demo():
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, "hello.py")
            with open(script, "w") as f:
                f.write("print('hello world')\n")
            result = await run_script(script, cwd=tmp, timeout_s=10)
            print(
                f"Script result: stdout={result.stdout.strip()}, returncode={result.returncode}"
            )

    asyncio.run(_demo())
