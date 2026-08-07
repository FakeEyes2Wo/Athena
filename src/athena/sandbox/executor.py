"""SandboxExecutor —— 受控 Python 代码执行核心。

通过 subprocess 隔离执行用户 Python 代码，注入 import 白名单、
SafeOS 代理和资源限制。
"""

import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from athena.sandbox.whitelist import ALLOWED_IMPORTS
from athena.sandbox.limits import SandboxLimits

@dataclass(slots=True)
class InspectResult:
    """python_inspect 的返回结构。"""

    ok: bool
    value: str | None = None
    type: str | None = None
    shape: str | None = None
    columns: list[str] | None = None
    error: str | None = None
    traceback: str | None = None
    duration_ms: int = 0

@dataclass(slots=True)
class ExecuteResult:
    """python_execute 的返回结构。"""

    ok: bool
    stdout: str = ""
    stderr: str | None = None
    output_files: list[str] = field(default_factory=list)
    error: str | None = None
    traceback: str | None = None
    duration_ms: int = 0
    memory_mb: float | None = None

class SandboxExecutor:
    """受控 Python 代码执行器。

    每次调用 inspect/execute 启动全新子进程，默认无状态。
    工作目录限制在 work_root 内，不可穿越。
    """

    def __init__(self, work_root: Path, allowed_imports: frozenset[str] | None = None) -> None:
        self.work_root = work_root.resolve()
        self.allowed_imports = (
            allowed_imports if allowed_imports is not None else ALLOWED_IMPORTS
        )
        self._running: set[asyncio.subprocess.Process] = set()
        """正在运行的子进程集合，shutdown 时统一清理。"""

    def _resolve_cwd(self, cwd: str) -> Path:
        """解析并验证工作目录，拒绝 .. 穿越。"""
        resolved = (self.work_root / cwd).resolve()
        try:
            resolved.relative_to(self.work_root)
        except ValueError:
            raise ValueError(f"cwd 不允许穿越 work_root: {cwd}")
        return resolved

    def _build_wrapper(self, *, inject: str, preamble: str) -> str:
        """生成带有隔离注入的 wrapper script。"""
        allowed_str = "{" + ", ".join(repr(x) for x in sorted(self.allowed_imports)) + "}"
        safe_os_code = (
            "from athena.sandbox.whitelist import build_safe_os\n"
            "sys.modules['os'] = build_safe_os()\n"
        )

        return f'''\
# -*- coding: utf-8 -*-
import sys, json

# 0. 资源限制
from athena.sandbox.limits import SandboxLimits
SandboxLimits.apply_memory_limit()

# 1. Import 白名单
import builtins as __builtins__
_ALLOWED = {allowed_str}
_orig_import = __builtins__.__import__
def _safe_import(name, g=None, l=None, f=None, level=0):
    top = name.split(".")[0]
    if top not in _ALLOWED:
        raise ImportError(f"{{name!r}} is not in the sandbox allowlist")
    return _orig_import(name, g, l, f, level)
__builtins__.__import__ = _safe_import

# 2. SafeOS 代理
{safe_os_code}

# 3. 用户 preamble
import os as _sandbox_os
{preamble}

# 4. 执行用户代码
try:
{inject}
    _ok = True
except Exception as _e:
    import traceback
    _ok = False
    _error = repr(_e)
    _tb = traceback.format_exc()

# 5. 输出标记行
_marker_start = "___SANDBOX_RESULT_START___"
_marker_end = "___SANDBOX_RESULT_END___"
if _ok:
    _output = {{"ok": True, "value": repr(result)[:2000] if 'result' in dir() else None}}
else:
    _output = {{"ok": False, "error": _error, "traceback": _tb[-800:] if _tb else None}}
print(_marker_start, flush=True)
print(json.dumps(_output, ensure_ascii=False), flush=True)
print(_marker_end, flush=True)
'''

    async def inspect(
        self, expr: str, cwd: str = ".", timeout: int = 10
    ) -> InspectResult:
        """执行单行 Python 表达式并返回摘要。

        预置 pd/np，使用 eval 仅求值不允许定义函数/类。
        """
        cwd_path = self._resolve_cwd(cwd)
        timeout = min(max(timeout, 1), 30)

        preamble = (
            "import pandas as pd\n"
            "import numpy as np\n"
        )
        inject = (
            '    _sandbox_os.chdir(' + repr(str(cwd_path)) + ')\n'
            '    result = eval(' + repr(expr) + ', {"__builtins__": __builtins__, "pd": pd, "np": np}, {})\n'
        )

        wrapper = self._build_wrapper(inject=inject, preamble=preamble)
        t0 = time.perf_counter()
        stdout, parsed, stderr = await self._run_subprocess(wrapper, timeout)
        dt_ms = int((time.perf_counter() - t0) * 1000)

        if not parsed.get("ok"):
            return InspectResult(
                ok=False,
                error=parsed.get("error", "Unknown error"),
                traceback=parsed.get("traceback"),
                duration_ms=dt_ms,
            )

        value = parsed.get("value", "")
        result = InspectResult(ok=True, value=value, duration_ms=dt_ms)

        # 在后置进程中提取 type/shape/columns（如果 value 非空且是 DataFrame/ndarray）
        if value and value != "None":
            result.type = await self._infer_type(expr, cwd_path, timeout)
            if result.type and "DataFrame" in result.type:
                result.shape = await self._infer_shape(expr, cwd_path, timeout)
                result.columns = await self._infer_columns(expr, cwd_path, timeout)

        return result

    async def execute(
        self, script: str, cwd: str = ".", timeout: int = 60
    ) -> ExecuteResult:
        """执行 Python 脚本并返回 stdout/stderr 和产出文件。

        预置 pd/np/plt/sns/sklearn。matplotlib 使用 Agg 后端。
        """
        cwd_path = self._resolve_cwd(cwd)
        timeout = min(max(timeout, 1), 120)

        # 记录执行前的文件快照
        before_files = set()
        for f in cwd_path.rglob("*"):
            if f.is_file():
                before_files.add(f.resolve())

        preamble = (
            "import pandas as pd\n"
            "import numpy as np\n"
            "import matplotlib\n"
            "matplotlib.use('Agg')\n"
            "import matplotlib.pyplot as plt\n"
            "import seaborn as sns\n"
            "import sklearn\n"
            "_user_globals = {\n"
            "    '__builtins__': __builtins__,\n"
            "    'pd': pd, 'np': np,\n"
            "    'plt': plt, 'sns': sns, 'sklearn': sklearn,\n"
            "}\n"
        )
        inject = (
            '    _sandbox_os.chdir(' + repr(str(cwd_path)) + ')\n'
            '    compiled = compile(' + repr(script) + ', "<sandbox>", "exec")\n'
            '    exec(compiled, _user_globals)\n'
            '    result = _user_globals.get("_result", repr(_user_globals.get("result", "<no explicit result>")))\n'
        )

        wrapper = self._build_wrapper(inject=inject, preamble=preamble)
        t0 = time.perf_counter()
        stdout, parsed, stderr = await self._run_subprocess(wrapper, timeout)
        dt_ms = int((time.perf_counter() - t0) * 1000)

        # 计算新增/修改文件
        after_files = set()
        if cwd_path.exists():
            for f in cwd_path.rglob("*"):
                if f.is_file():
                    after_files.add(f.resolve())
        new_files = [
            str(f.relative_to(self.work_root))
            for f in (after_files - before_files)
        ]

        if not parsed.get("ok"):
            return ExecuteResult(
                ok=False,
                stdout=stdout,
                stderr=stderr,
                error=parsed.get("error"),
                traceback=parsed.get("traceback"),
                duration_ms=dt_ms,
            )

        return ExecuteResult(
            ok=True,
            stdout=stdout[:5000],
            stderr=stderr,
            output_files=new_files,
            duration_ms=dt_ms,
        )

    async def _run_subprocess(
        self, wrapper: str, timeout: int
    ) -> tuple[str, dict, str | None]:
        """启动子进程执行 wrapper script，返回 (stdout, parsed_json, stderr)。"""
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", wrapper,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.work_root),
        )
        self._running.add(proc)
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            # 子进程执行超时 → 强制终止并返回超时错误
            try:
                proc.kill()
            except ProcessLookupError:
                # 子进程在超时与 kill 之间已退出
                pass
            await proc.wait()
            return "", {"ok": False, "error": f"超时({timeout}s)"}, None
        finally:
            self._running.discard(proc)

        return _parse_subprocess_output(stdout_b, stderr_b)

    async def shutdown(self) -> None:
        """终止所有运行中的子进程。"""
        for proc in list(self._running):
            try:
                proc.kill()
            except Exception:
                pass
        self._running.clear()

    async def _infer_type(
        self, expr: str, cwd_path: Path, timeout: int
    ) -> str | None:
        """后置进程获取表达式结果类型名。"""
        wrapper = self._build_wrapper(
            inject=(
                '    _sandbox_os.chdir(' + repr(str(cwd_path)) + ')\n'
                '    result = str(type(eval(' + repr(expr) + ', {"__builtins__": __builtins__, "pd": pd, "np": np}, {})))\n'
            ),
            preamble="import pandas as pd\nimport numpy as np\n",
        )
        _, parsed, _ = await self._run_subprocess(wrapper, timeout)
        return parsed.get("value") if parsed.get("ok") else None

    async def _infer_shape(
        self, expr: str, cwd_path: Path, timeout: int
    ) -> str | None:
        """后置进程获取 DataFrame/ndarray 的 shape。"""
        wrapper = self._build_wrapper(
            inject=(
                '    _sandbox_os.chdir(' + repr(str(cwd_path)) + ')\n'
                '    _tmp = eval(' + repr(expr) + ', {"__builtins__": __builtins__, "pd": pd, "np": np}, {})\n'
                '    result = repr(getattr(_tmp, "shape", ""))\n'
            ),
            preamble="import pandas as pd\nimport numpy as np\n",
        )
        _, parsed, _ = await self._run_subprocess(wrapper, min(timeout, 5))
        return parsed.get("value") if parsed.get("ok") else None

    async def _infer_columns(
        self, expr: str, cwd_path: Path, timeout: int
    ) -> list[str] | None:
        """后置进程获取 DataFrame 的列名列表。"""
        wrapper = self._build_wrapper(
            inject=(
                '    _sandbox_os.chdir(' + repr(str(cwd_path)) + ')\n'
                '    _tmp = eval(' + repr(expr) + ', {"__builtins__": __builtins__, "pd": pd, "np": np}, {})\n'
                '    cols = list(_tmp.columns) if hasattr(_tmp, "columns") else []\n'
                '    result = repr(cols[:50])\n'
            ),
            preamble="import pandas as pd\nimport numpy as np\n",
        )
        _, parsed, _ = await self._run_subprocess(wrapper, min(timeout, 5))
        if parsed.get("ok") and parsed.get("value"):
            try:
                return json.loads(parsed["value"].replace("'", '"'))
            except json.JSONDecodeError:
                # JSON 解析失败（列名含特殊字符）→ 返回 None
                return None
        return None

def _parse_subprocess_output(
    stdout_b: bytes, stderr_b: bytes
) -> tuple[str, dict, str | None]:
    """解析子进程输出，提取 ___SANDBOX_RESULT_START___/___END___ 之间的 JSON。"""
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else None

    marker_start = "___SANDBOX_RESULT_START___"
    marker_end = "___SANDBOX_RESULT_END___"

    if marker_start in stdout and marker_end in stdout:
        head, _, tail = stdout.partition(marker_start)
        json_str, _, after_marker = tail.partition(marker_end)
        stdout_output = (head + after_marker).strip()
        try:
            parsed = json.loads(json_str.strip().split("\n")[0])
        except json.JSONDecodeError:
            # JSON 解析失败（子进程输出格式异常）→ 回退为错误标记
            parsed = {"ok": False, "error": "无法解析子进程结果 JSON"}
        return stdout_output, parsed, stderr

    return stdout.strip(), {"ok": False, "error": "缺少结果标记行"}, stderr
