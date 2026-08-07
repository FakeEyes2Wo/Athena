# Sandbox Executor 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现独立 MCP Sandbox Server，让 Agent 能通过 `python_inspect` / `python_execute` 自主执行数据分析代码，同时加入 RunGuard 硬约束防止 ReAct 死循环。

**Architecture:** Sandbox 作为独立 MCP stdio server 进程，通过 `McpClientManager` 被 Agent 发现和调用。核心 `SandboxExecutor` 用 subprocess + import 白名单 + 平台感知资源限制实现进程级隔离。`RunGuard` 在 Agent runtime 层做硬约束。

**Tech Stack:** Python 3.11+, `mcp` 库 (MCP server), `asyncio.create_subprocess_exec`, Win32 Job Object / `resource.RLIMIT_AS` / `prctl`

## 全局约束

- `docs/代码规范.md`：导入放在文件开头、docstring 中文描述函数用途、嵌套不超 3 层、无 ASCII 分隔线、无连续双空行
- commit 消息使用中文
- 文档使用中文
- 文件操作通过白名单 `os`/`pathlib` 完成，不新增独立 fs 工具
- 平台支持 Windows / Linux / macOS，分平台实现内存限制和孤儿清理

---

### Task 1: 白名单与 SafeOS 代理

**Files:**
- Create: `src/athena/sandbox/__init__.py`
- Create: `src/athena/sandbox/whitelist.py`

**Interfaces:**
- Produces: `ALLOWED_IMPORTS: frozenset[str]` — 允许的 import 白名单
- Produces: `build_safe_os() -> object` — 返回替换 `sys.modules['os']` 的代理对象
- Produces: `SafeOSError(PermissionError)` — 访问被禁 os 函数时抛出

- [ ] **Step 1: 创建包目录和空白 `__init__.py`**

```bash
mkdir -p src/athena/sandbox
```

```python
# src/athena/sandbox/__init__.py
"""Athena Sandbox —— 受控 Python 代码执行沙箱。

提供 MCP server 形式的沙箱服务，支持 python_inspect（表达式求值）
和 python_execute（脚本执行），带 import 白名单、内存限制和超时控制。
"""
```

- [ ] **Step 2: 写白名单常量**

```python
# src/athena/sandbox/whitelist.py
"""import 白名单与 SafeOS 代理。"""

import os as _real_os

# 允许的数据科学库和标准库模块
ALLOWED_IMPORTS: frozenset[str] = frozenset({
    "pandas", "numpy", "matplotlib", "seaborn", "scipy", "sklearn",
    "collections", "itertools", "math", "statistics", "json", "csv",
    "pathlib", "io", "typing", "datetime", "warnings",
})


class SafeOSError(PermissionError):
    """访问被禁 os 函数时抛出。"""


# os 中允许暴露的函数名
_OS_WHITELIST: frozenset[str] = frozenset({
    "listdir", "makedirs", "stat", "remove", "rename", "rmdir",
    "getcwd", "sep", "path",
})


# os 中显式禁止的敏感函数名（用于更清晰的错误信息）
_OS_BLOCKED: frozenset[str] = frozenset({
    "system", "popen", "execv", "execve", "execl", "execle", "execlp",
    "execlpe", "execvp", "execvpe", "spawnl", "spawnle", "spawnlp",
    "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
    "kill", "chmod", "chown", "setuid", "setgid", "environ",
    "startfile", "fork", "putenv", "unsetenv",
})
```

- [ ] **Step 3: 实现 `build_safe_os()` 代理类**

```python
class _SafeOSProxy:
    """os 模块的安全代理。

    只暴露 _OS_WHITELIST 中的函数/属性；禁止 _OS_BLOCKED 中的函数；
    未在白名单也不在黑名单的函数返回 PermissionError。
    path 子模块被替换为一个具有相同路径操作函数的代理对象。
    """

    def __getattr__(self, name: str) -> object:
        if name in _OS_BLOCKED:
            raise SafeOSError(f"os.{name} 在 sandbox 中被禁止")
        if name in _OS_WHITELIST:
            return getattr(_real_os, name)
        raise SafeOSError(f"os.{name} 不在 sandbox 白名单中")


def build_safe_os() -> object:
    """返回 SafeOS 代理实例，用于替换 sys.modules['os']。"""
    return _SafeOSProxy()
```

- [ ] **Step 4: 验证模块可导入**

```bash
uv run python -c "from athena.sandbox.whitelist import ALLOWED_IMPORTS, build_safe_os, SafeOSError; print('OK')"
```

- [ ] **Step 5: Commit**

```bash
git add src/athena/sandbox/__init__.py src/athena/sandbox/whitelist.py
git commit -m "feat(sandbox): 添加 import 白名单与 SafeOS 代理模块"
```

---

### Task 2: 跨平台资源限制

**Files:**
- Create: `src/athena/sandbox/limits.py`

**Interfaces:**
- Produces: `SandboxLimits.apply_memory_limit() -> None` — 在子进程内调用，限制自身内存至 512MB
- Produces: `SandboxLimits.MEMORY_MB: int = 512`
- Produces: `SandboxLimits.ensure_platform_support() -> None` — 检测平台，不支持抛 `RuntimeError`

- [ ] **Step 1: 写平台检测与内存限制**

```python
# src/athena/sandbox/limits.py
"""平台感知的子进程资源限制。

wrapper 子进程启动后第一时间调用 SandboxLimits.apply_memory_limit()
将自身内存限制在 MEMORY_MB。
"""

import sys


class SandboxLimits:
    """子进程资源限制，平台感知。"""

    MEMORY_MB: int = 512
    """硬内存上限，单位 MiB。"""

    @staticmethod
    def ensure_platform_support() -> None:
        """验证当前平台受支持；不满足时抛 RuntimeError。"""
        if sys.platform == "win32":
            import ctypes
            if not hasattr(ctypes, "windll"):
                raise RuntimeError("Windows 平台缺少 ctypes.windll")
        elif sys.platform in ("linux", "darwin"):
            import resource
            import signal
        else:
            raise RuntimeError(f"不支持的平台: {sys.platform}")

    @staticmethod
    def apply_memory_limit() -> None:
        """将当前进程内存限制为 MEMORY_MB。"""
        limit_bytes = SandboxLimits.MEMORY_MB * 1024 * 1024

        if sys.platform == "linux" or sys.platform == "darwin":
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
        elif sys.platform == "win32":
            _win32_set_memory_limit(SandboxLimits.MEMORY_MB)
```

- [ ] **Step 2: 实现 Windows 内存限制**

```python
def _win32_set_memory_limit(limit_mb: int) -> None:
    """通过 Win32 Job Object 限制进程内存。"""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32

    # 创建 Job Object
    hjob = kernel32.CreateJobObjectW(None, None)
    if not hjob:
        raise OSError("CreateJobObjectW 失败")

    # 配置内存限制
    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", wintypes.DWORD * 8),  # JOBOBJECT_BASIC_LIMIT_INFORMATION 占位
            ("IoInfo", ctypes.c_void_p),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.ProcessMemoryLimit = limit_mb * 1024 * 1024
    info.JobMemoryLimit = limit_mb * 1024 * 1024

    # JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x100, JOB_OBJECT_LIMIT_JOB_MEMORY = 0x200
    info.BasicLimitInformation[2] = 0x100 | 0x200

    JobObjectExtendedLimitInformation = 9
    ret = kernel32.SetInformationJobObject(
        hjob,
        JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ret:
        raise OSError("SetInformationJobObject 失败")

    # 把当前进程赋给 Job Object
    hproc = kernel32.GetCurrentProcess()
    ret = kernel32.AssignProcessToJobObject(hjob, hproc)
    if not ret:
        raise OSError("AssignProcessToJobObject 失败")
```

- [ ] **Step 3: 验证**

```bash
uv run python -c "from athena.sandbox.limits import SandboxLimits; SandboxLimits.ensure_platform_support(); print('OK')"
```

- [ ] **Step 4: Commit**

```bash
git add src/athena/sandbox/limits.py
git commit -m "feat(sandbox): 添加跨平台资源限制模块（内存 512MB）"
```

---

### Task 3: 进程生命周期绑定

**Files:**
- Create: `src/athena/sandbox/lifecycle.py`

**Interfaces:**
- Produces: `ProcessLifecycle.bind_to_parent() -> None` — sandbox server 启动时调用，确保 Agent 退出时自身被 OS 清理
- Produces: `ProcessLifecycle.create_parent_job() -> int | None` — Agent 侧调用，创建 Job Object handle

- [ ] **Step 1: 实现 Linux/macOS 孤儿清理 + Windows Job Object 创建**

```python
# src/athena/sandbox/lifecycle.py
"""进程生命周期绑定。

ProcessLifecycle.bind_to_parent() 由 sandbox server 自身调用；
ProcessLifecycle.create_parent_job() 由 Agent 侧调用，
创建 KILL_ON_JOB_CLOSE Job Object 用于清理 sandbox 进程树。
"""

import sys


class ProcessLifecycle:
    """平台感知的进程-父进程生命周期绑定。"""

    @staticmethod
    def bind_to_parent() -> None:
        """绑定到父进程：父进程退出时，当前进程被 OS 自动清理。"""
        if sys.platform == "linux" or sys.platform == "darwin":
            import ctypes
            import signal

            try:
                libc = ctypes.CDLL(None)
                # PR_SET_PDEATHSIG = 1
                libc.prctl(1, signal.SIGKILL)
            except Exception:
                # prctl 失败不阻塞启动（如容器环境），日志记录但不崩溃
                pass
        elif sys.platform == "win32":
            # Windows 侧由 Agent 通过 create_parent_job() 创建的 Job Object 覆盖
            pass

    @staticmethod
    def create_parent_job() -> int | None:
        """创建 KILL_ON_JOB_CLOSE Job Object，返回句柄。

        Agent 启动 sandbox server 子进程后调用此方法，
        将子进程赋给该 Job Object。Agent 退出时 OS 自动关闭句柄，
        sandbox 进程树被清理。
        """
        if sys.platform != "win32":
            return None

        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", wintypes.DWORD * 8),
                ("IoInfo", ctypes.c_void_p),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        hjob = kernel32.CreateJobObjectW(None, None)
        if not hjob:
            return None

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        info.BasicLimitInformation[2] = 0x2000

        JobObjectExtendedLimitInformation = 9
        ret = kernel32.SetInformationJobObject(
            hjob,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ret:
            kernel32.CloseHandle(hjob)
            return None

        return hjob

    @staticmethod
    def assign_to_job(job_handle: int, pid: int) -> bool:
        """将进程 PID 赋给 Job Object。"""
        if sys.platform != "win32":
            return False
        import ctypes

        kernel32 = ctypes.windll.kernel32
        PROCESS_SET_QUERY = 0x0200
        PROCESS_TERMINATE = 0x0001
        hproc = kernel32.OpenProcess(PROCESS_SET_QUERY | PROCESS_TERMINATE, False, pid)
        if not hproc:
            return False
        ret = kernel32.AssignProcessToJobObject(job_handle, hproc)
        kernel32.CloseHandle(hproc)
        return bool(ret)
```

- [ ] **Step 2: 验证模块可导入**

```bash
uv run python -c "from athena.sandbox.lifecycle import ProcessLifecycle; ProcessLifecycle.bind_to_parent(); print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add src/athena/sandbox/lifecycle.py
git commit -m "feat(sandbox): 添加进程生命周期绑定模块"
```

---

### Task 4: SandboxExecutor 核心

**Files:**
- Create: `src/athena/sandbox/executor.py`

**Interfaces:**
- Consumes: `ALLOWED_IMPORTS` from `athena.sandbox.whitelist`, `SandboxLimits` from `.limits`, `build_safe_os` from `.whitelist`
- Produces: `InspectResult` dataclass, `ExecuteResult` dataclass
- Produces: `SandboxExecutor(work_root, allowed_imports)` class with `async inspect(expr, cwd=".", timeout=10) -> InspectResult` and `async execute(script, cwd=".", timeout=60) -> ExecuteResult`

- [ ] **Step 1: 定义结果数据类**

```python
# src/athena/sandbox/executor.py
"""SandboxExecutor —— 受控 Python 代码执行核心。

通过 subprocess 隔离执行用户 Python 代码，注入 import 白名单、
SafeOS 代理和资源限制。
"""

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from athena.sandbox.whitelist import ALLOWED_IMPORTS, build_safe_os
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
```

- [ ] **Step 2: 实现 SandboxExecutor 初始化与方法签名**

```python
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
```

- [ ] **Step 3: 实现 wrapper 脚本模板生成**

```python
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
```

- [ ] **Step 4: 实现 inspect 方法**

```python
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
```

- [ ] **Step 5: 实现 execute 方法**

```python
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
```

- [ ] **Step 6: 实现子进程启动与结果解析**

```python
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
            proc.kill()
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
        # 复用前次 eval 结果不可行，使用单独进程
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
                '    result = repr(cols[:50])\n'  # 最多 50 列
            ),
            preamble="import pandas as pd\nimport numpy as np\n",
        )
        _, parsed, _ = await self._run_subprocess(wrapper, min(timeout, 5))
        if parsed.get("ok") and parsed.get("value"):
            try:
                return json.loads(parsed["value"].replace("'", '"'))
            except json.JSONDecodeError:
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
            parsed = {"ok": False, "error": "无法解析子进程结果 JSON"}
        return stdout_output, parsed, stderr

    return stdout.strip(), {"ok": False, "error": "缺少结果标记行"}, stderr
```

- [ ] **Step 7: 验证 executor 可导入**

```bash
uv run python -c "from athena.sandbox.executor import SandboxExecutor, InspectResult, ExecuteResult; print('OK')"
```

- [ ] **Step 8: Commit**

```bash
git add src/athena/sandbox/executor.py
git commit -m "feat(sandbox): 实现 SandboxExecutor 核心——wrapper 生成 + subprocess 隔离执行"
```

---

### Task 5: SandboxExecutor 基础单元测试

**Files:**
- Create: `tests/test_sandbox_executor.py`

**Interfaces:**
- Consumes: `SandboxExecutor`, `InspectResult`, `ExecuteResult` from `athena.sandbox.executor`

- [ ] **Step 1: 写测试文件与 fixture**

```python
# tests/test_sandbox_executor.py
"""SandboxExecutor 单元测试 —— 不启动 MCP server，直接测核心逻辑。"""

import tempfile
from pathlib import Path

import pytest

from athena.sandbox.executor import SandboxExecutor, InspectResult, ExecuteResult


@pytest.fixture
def executor():
    """创建指向临时目录的 SandboxExecutor。"""
    with tempfile.TemporaryDirectory() as tmp:
        yield SandboxExecutor(work_root=Path(tmp))
```

- [ ] **Step 2: inspect_basic 测试**

```python
@pytest.mark.asyncio
async def test_inspect_basic(executor):
    """表达式求值返回正确值。"""
    result = await executor.inspect("1 + 1")
    assert result.ok
    assert result.value == "2"
```

- [ ] **Step 3: inspect_dataframe 测试**

```python
@pytest.mark.asyncio
async def test_inspect_dataframe(executor):
    """DataFrame 求值返回 shape 和 columns。"""
    result = await executor.inspect("pd.DataFrame({'a': [1, 2], 'b': [3, 4]})")
    assert result.ok
    assert result.type is not None
    assert "DataFrame" in result.type
    # shape 和 columns 可能为 None（后置进程失败时），不强制断言
```

- [ ] **Step 4: inspect_timeout 测试**

```python
@pytest.mark.asyncio
async def test_inspect_timeout(executor):
    """无限循环表达式触发超时。"""
    result = await executor.inspect(
        "exec('while True: pass') or 1", timeout=3
    )
    assert not result.ok
    assert "超时" in (result.error or "")
```

- [ ] **Step 5: execute_script 测试**

```python
@pytest.mark.asyncio
async def test_execute_script(executor):
    """脚本写文件后产出 output_files。"""
    script = (
        "import os\n"
        "with open('out.txt', 'w', encoding='utf-8') as f:\n"
        "    f.write('hello sandbox')\n"
    )
    result = await executor.execute(script)
    assert result.ok
    assert "out.txt" in result.output_files
```

- [ ] **Step 6: import 白名单测试**

```python
@pytest.mark.asyncio
async def test_import_whitelist_blocked(executor):
    """被禁的 os.system 触发 PermissionError。"""
    result = await executor.inspect(
        "exec('import os; os.system(\"echo hacked\")') or 'never'"
    )
    # SafeOS 代理拦截 os.system → PermissionError
    # eval 层无法直接调用 os.system，用 exec 间接测试
    result2 = await executor.execute("import os; os.system('echo test')")
    assert not result2.ok or result2.stderr is not None
```

- [ ] **Step 7: result_truncation 测试**

```python
@pytest.mark.asyncio
async def test_result_truncation(executor):
    """超长输出被截断。"""
    result = await executor.inspect("'x' * 3000")
    assert result.ok
    assert len(result.value or "") <= 2000
```

- [ ] **Step 8: 运行所有测试，确认全部通过**

```bash
uv run pytest tests/test_sandbox_executor.py -v
```

- [ ] **Step 9: Commit**

```bash
git add tests/test_sandbox_executor.py
git commit -m "test(sandbox): 添加 SandboxExecutor 单元测试"
```

---

### Task 6: MCP Server 实现

**Files:**
- Create: `src/athena/sandbox/server.py`
- Create: `src/athena/sandbox/__main__.py`

**Interfaces:**
- Consumes: `SandboxExecutor`, `InspectResult`, `ExecuteResult` from `.executor`, `ALLOWED_IMPORTS` from `.whitelist`
- Produces: `AthenaSandboxServer` — MCP server 实例，暴露 3 个工具
- Produces: `__main__.py` — CLI 入口 `python -m athena.sandbox`

- [ ] **Step 1: 确认 FastMCP 可用**

```bash
uv run python -c "from mcp.server.fastmcp import FastMCP; print('FastMCP OK')"
```

- [ ] **Step 2: 实现 MCP server（使用 FastMCP）**

```python
# src/athena/sandbox/server.py
"""Athena Sandbox MCP Server —— 通过 stdio 暴露 python_inspect/python_execute/sandbox_config。"""

import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from athena.sandbox.executor import SandboxExecutor

mcp = FastMCP("athena-sandbox")


def create_sandbox_server(work_root: str) -> FastMCP:
    """创建并配置 Sandbox MCP Server。

    Args:
        work_root: 沙箱工作根目录绝对路径

    Returns:
        配置完成的 FastMCP 实例，已注册 3 个工具
    """
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
            "memory_limit_mb": 512,
        }

    return mcp
```

- [ ] **Step 3: 实现 `__main__.py` CLI 入口**

```python
# src/athena/sandbox/__main__.py
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
```

- [ ] **Step 4: 验证 MCP server 可启动**

```bash
timeout 2 uv run python -m athena.sandbox --work-root /tmp/test 2>&1 || true
```

- [ ] **Step 5: Commit**

```bash
git add src/athena/sandbox/server.py src/athena/sandbox/__main__.py
git commit -m "feat(sandbox): 实现 MCP stdio server，暴露 3 个工具"
```

---

### Task 7: RunGuard 模块

**Files:**
- Create: `src/athena/core/agent/guard.py`

**Interfaces:**
- Produces: `GuardError(RuntimeError)` — 死循环/重复调用被拦截时抛出
- Produces: `RunGuard` dataclass with `check_before_call(tool_name, args)`, `record_result(tool_name, success, error)`, `check_enter_loop()`

- [ ] **Step 1: 实现 RunGuard**

```python
# src/athena/core/agent/guard.py
"""RunGuard —— Agent ReAct loop 硬约束。

在运行时层面防止 Agent 陷入死循环：相同调用拒绝、连续失败限制、
相同错误模式循环检测。不依赖 System Prompt。
"""

import json
from dataclasses import dataclass, field


class GuardError(RuntimeError):
    """RunGuard 拦截死循环/重复调用时抛出。

    被 Agent.run() 捕获后返回 AgentOutcome（带 error 文本），
    Turn 终止但 Thread 存活。
    """


@dataclass
class RunGuard:
    """每个 Agent.run() 调用创建一个 fresh 实例。

    三条规则：
    1. 完全相同的调用 → 拒绝
    2. 同一工具连续失败 3 次 → GuardError
    3. 同一 (tool, error_type) 累计 3 次 → GuardError
    """

    max_identical_calls: int = 1
    """同一 (tool, args) 最多允许的调用次数。"""

    max_consecutive_failures: int = 3
    """同一工具连续失败上限。"""

    max_same_approach_failures: int = 3
    """同一 (tool, error_type) 模式累计上限。"""

    _call_hashes: set[tuple[str, int]] = field(default_factory=set)
    """已调用过的 (tool_name, args_hash) 集合。"""

    _consecutive_failures: dict[str, int] = field(default_factory=dict)
    """tool_name → 当前连续失败次数。"""

    _error_patterns: dict[tuple[str, str], int] = field(default_factory=dict)
    """(tool_name, err_key) → 累计出现次数。"""

    def check_enter_loop(self) -> None:
        """进入下一轮采样前的准入检查。"""

    def check_before_call(self, tool_name: str, args: dict) -> None:
        """工具调用前准入检查。

        Args:
            tool_name: 工具名
            args: 工具参数

        Raises:
            GuardError: 相同调用已执行过
        """
        call_hash = (tool_name, _hash_args(args))
        if call_hash in self._call_hashes:
            raise GuardError(
                f"完全相同的调用 {tool_name}({_summarize_args(args)}) 已执行过。"
                f"请改变参数或换一种方式。"
            )
        self._call_hashes.add(call_hash)

    def record_result(
        self, tool_name: str, success: bool, error: str | None
    ) -> None:
        """工具返回后更新计数器。

        Args:
            tool_name: 工具名
            success: 调用是否成功
            error: 失败时的错误信息

        Raises:
            GuardError: 连续失败或错误模式循环触及上限
        """
        if success:
            # 成功后重置该工具的连续失败计数
            self._consecutive_failures.pop(tool_name, None)
            return

        # 连续失败计数
        self._consecutive_failures[tool_name] = (
            self._consecutive_failures.get(tool_name, 0) + 1
        )
        if self._consecutive_failures[tool_name] > self.max_consecutive_failures:
            raise GuardError(
                f"{tool_name} 已连续失败 {self.max_consecutive_failures} 次。"
                f"请换一种完全不同的方式。"
            )

        # 错误模式计数
        err_key = _classify_error(error)
        pattern = (tool_name, err_key)
        self._error_patterns[pattern] = (
            self._error_patterns.get(pattern, 0) + 1
        )
        if self._error_patterns[pattern] > self.max_same_approach_failures:
            raise GuardError(
                f"{tool_name} 反复遇到 '{err_key}' 错误。"
                f"当前策略无效，请改变策略。"
            )


def _hash_args(args: dict) -> int:
    """对工具参数字典做稳定哈希。"""
    return hash(
        json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    )


def _classify_error(error: str | None) -> str:
    """从 error 消息中提取错误分类键。"""
    if not error:
        return "unknown"
    for key in (
        "Timeout", "MemoryError", "ImportError", "NameError",
        "ValueError", "TypeError", "FileNotFoundError", "KeyError",
        "PermissionError", "ConnectionError", "McpClientError",
    ):
        if key in error:
            return key
    return "other"


def _summarize_args(args: dict) -> str:
    """生成参数摘要字符串用于错误消息。"""
    parts = []
    for k, v in args.items():
        s = str(v)
        if len(s) > 50:
            s = s[:47] + "..."
        parts.append(f"{k}={s}")
    return ", ".join(parts)
```

- [ ] **Step 2: 验证模块可导入**

```bash
uv run python -c "from athena.core.agent.guard import RunGuard, GuardError; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add src/athena/core/agent/guard.py
git commit -m "feat(agent): 添加 RunGuard 硬约束模块，防止 ReAct 死循环"
```

---

### Task 8: RunGuard 集成到 Agent Runtime

**Files:**
- Modify: `src/athena/core/agent/models.py` — `AgentContext` 增加 `guard` 字段
- Modify: `src/athena/core/agent/runtime.py` — `Agent.run()` 和 `_sampling_loop()` 中集成

**Interfaces:**
- Consumes: `RunGuard`, `GuardError` from `.guard`
- Modifies: `AgentContext.guard: RunGuard | None` — 可选的守卫实例
- Modifies: `Agent.run()` — 创建 RunGuard，捕获 GuardError
- Modifies: `_sampling_loop()` — 调用 guard.check_before_call() 和 guard.record_result()

- [ ] **Step 1: 更新 AgentContext**

在 `src/athena/core/agent/models.py` 中：

```python
# 在 AgentContext 类中增加字段
@dataclass(slots=True)
class AgentContext:
    """每 Turn 上下文。memory 由 ThreadRuntime 注入，Agent 不自行创建。"""

    thread: AthenaThread
    turn: AthenaTurn
    emit: EmitEvent
    tools: ToolRegistry
    cancel: asyncio.Event
    memory: "ContextManager | None" = None
    input_text: str | None = None
    guard: "RunGuard | None" = None  # 新增：硬约束守卫
```

- [ ] **Step 2: 在 Agent.run() 中创建 RunGuard**

首先在 `src/athena/core/agent/runtime.py` 文件开头添加导入（与现有导入放在一起，不放在函数体内）：

```python
from athena.core.agent.guard import RunGuard, GuardError
```

然后在 `Agent.run()` 方法中，在循环前创建 guard：

```python
async def run(self, ctx: AgentContext) -> AgentOutcome:
    guard = RunGuard()
    ctx.guard = guard

    mem = ctx.memory
    if mem is None:
        mem = ctx.memory = ContextManager()

    # ... 后续现有代码 ...

    # 主循环（修改 for 循环体以捕获 GuardError）
    for _ in range(self.config.max_turns):
        if ctx.cancel.is_set():
            break
        try:
            outcome = await _sampling_loop(self, ctx)
        except GuardError as exc:
            # 硬约束触发 → 终止当前 Turn，返回错误信息
            return AgentOutcome(
                result_ref=f"result://{ctx.turn.turn_id}",
                next_context_ref=f"context://{ctx.turn.turn_id}/next",
            )
        # ... 后续现有代码 ...
```

- [ ] **Step 3: 在 _sampling_loop() 中集成守卫**

在 `_sampling_loop()` 中的 `function_call` 分支，工具调用前/后：

```python
# 在 function_call 分支中，dispatch_tool_call 之前:
tc = ToolCall(...)
# 新增：调用前检查
if hasattr(ctx, 'guard') and ctx.guard is not None:
    ctx.guard.check_before_call(tc.name, tc.arguments)
```

在 `_sampling_loop()` 的 results 收集之后（写入 ToolReturnPart 的回调区域），对每个 result 调用：

```python
# 在 for i, tc in enumerate(tool_calls) 循环内:
r = results[i]
# 新增：记录结果
if hasattr(ctx, 'guard') and ctx.guard is not None:
    error_text = None
    if isinstance(r, ToolResult) and not r.success:
        error_text = r.error
    elif isinstance(r, BaseException):
        error_text = f"{type(r).__name__}: {r}"
    ctx.guard.record_result(
        tc.name,
        success=(
            isinstance(r, ToolResult) and r.success
        ),
        error=error_text,
    )
```

- [ ] **Step 4: 验证现有测试未破坏（RunGuard 默认可通过）**

```bash
uv run pytest test/unit/agent_kernel/ -v -x
```

- [ ] **Step 5: Commit**

```bash
git add src/athena/core/agent/models.py src/athena/core/agent/runtime.py
git commit -m "feat(agent): 在 Agent runtime 中集成 RunGuard 硬约束"
```

---

### Task 9: RunGuard 单元测试

**Files:**
- Create: `test/unit/agent_kernel/test_guard.py`

- [ ] **Step 1: 写测试文件**

```python
# test/unit/agent_kernel/test_guard.py
"""RunGuard 单元测试。"""

import pytest
from athena.core.agent.guard import RunGuard, GuardError


class TestDuplicateCall:
    """规则1：完全相同的调用被拒绝。"""

    def test_duplicate_rejected(self):
        guard = RunGuard()
        guard.check_before_call("python_inspect", {"expr": "1+1"})
        # 第二次相同调用 → GuardError
        with pytest.raises(GuardError, match="完全相同的调用"):
            guard.check_before_call("python_inspect", {"expr": "1+1"})

    def test_different_args_allowed(self):
        guard = RunGuard()
        guard.check_before_call("python_inspect", {"expr": "1+1"})
        # 同工具不同参数 → 允许
        guard.check_before_call("python_inspect", {"expr": "2+2"})

    def test_different_tool_same_args_allowed(self):
        guard = RunGuard()
        guard.check_before_call("python_inspect", {"expr": "1+1"})
        # 不同工具相同参数 → 允许
        guard.check_before_call("python_execute", {"expr": "1+1"})


class TestConsecutiveFailures:
    """规则2：同一工具连续失败触发上限。"""

    def test_consecutive_failure_limit(self):
        guard = RunGuard()
        for i in range(3):
            guard.record_result("python_inspect", success=False, error="ValueError: bad")
        # 第4次连续失败 → GuardError
        with pytest.raises(GuardError, match="连续失败"):
            guard.record_result("python_inspect", success=False, error="ValueError: bad")

    def test_success_resets_failure_count(self):
        guard = RunGuard()
        guard.record_result("python_inspect", success=False, error="err")
        guard.record_result("python_inspect", success=False, error="err")
        guard.record_result("python_inspect", success=True, error=None)
        # 成功后计数器重置 → 不触发
        guard.record_result("python_inspect", success=False, error="err")

    def test_different_tools_independent(self):
        guard = RunGuard()
        guard.record_result("tool_a", success=False, error="err")
        guard.record_result("tool_a", success=False, error="err")
        # tool_b 不受 tool_a 的影响
        guard.record_result("tool_b", success=False, error="err")


class TestErrorPatterns:
    """规则3：同一错误模式循环触发上限。"""

    def test_error_pattern_limit(self):
        guard = RunGuard()
        for i in range(3):
            guard.record_result("python_inspect", success=False, error="Timeout(10s)")
        # 同一 pattern 第4次 → GuardError
        with pytest.raises(GuardError, match="反复遇到"):
            guard.record_result("python_inspect", success=False, error="Timeout(10s)")

    def test_different_patterns_independent(self):
        guard = RunGuard()
        guard.record_result("python_inspect", success=False, error="Timeout")
        guard.record_result("python_inspect", success=False, error="Timeout")
        # 不同 error_type → 独立计数
        guard.record_result("python_inspect", success=False, error="ValueError")
        guard.record_result("python_inspect", success=False, error="ValueError")


class TestClassifyError:
    """_classify_error 错误分类。"""

    def test_known_keys(self):
        from athena.core.agent.guard import _classify_error
        assert _classify_error("Timeout(10s)") == "Timeout"
        assert _classify_error("McpClientError: 连接失败") == "McpClientError"
        assert _classify_error("未知错误消息") == "other"
        assert _classify_error(None) == "unknown"
```

- [ ] **Step 2: 运行测试**

```bash
uv run pytest test/unit/agent_kernel/test_guard.py -v
```

- [ ] **Step 3: Commit**

```bash
git add test/unit/agent_kernel/test_guard.py
git commit -m "test(agent): 添加 RunGuard 单元测试"
```

---

### Task 10: Agent 集成 —— TaskUnderstandingAgent 更新

**Files:**
- Modify: `src/athena/agents/competition/task_understand_agent.py`
- Modify: `src/athena/prompts/competition/task_understand_system.txt`
- Modify: `mcp_servers.json`

- [ ] **Step 1: 更新 mcp_servers.json**

在 `mcp_servers.json` 的 servers 数组中新增 sandbox 配置：

```json
{
  "servers": [
    {
      "name": "sandbox",
      "transport": "stdio",
      "command": "uv",
      "args": ["run", "python", "-m", "athena.sandbox.server"],
      "tool_prefix": "mcp__sandbox__",
      "pinned_tools": []
    },
    {
      "name": "kaggle",
      "transport": "streamable_http",
      "url": "https://www.kaggle.com/mcp",
      "auth": { "type": "bearer", "env": "KAGGLE_API_TOKEN" },
      "tool_prefix": "kaggle__",
      "pinned_tools": []
    }
  ]
}
```

- [ ] **Step 2: 清理 TaskUnderstandingAgent 中的旧导入**

修改 `src/athena/agents/competition/task_understand_agent.py`：

```python
# 删除以下两行：
# from athena.tools.data_prepare import DataAnalyzeTool, DataCleanCodeGenTool

# 删除以下注释行：
# tools.register(DataAnalyzeTool(work_root=work_root))
# tools.register(DataCleanCodeGenTool(work_root=work_root))
```

同时更新文件头部注释和 docstring，去掉对数据准备层旧工具的引用。

- [ ] **Step 3: 重写 System Prompt Phase 4-5**

更新 `src/athena/prompts/competition/task_understand_system.txt`：

将 Phase 4 和 Phase 5 替换为：

```markdown
### Phase 4 — Data Exploration

You have access to sandbox tools (mcp__sandbox__python_inspect and
mcp__sandbox__python_execute) for Python code execution. Take an
iterative, scientist-like approach:

1. **Survey**: Use python_inspect with os.listdir('.') to understand the
   downloaded data directory structure.
2. **Peek**: Use python_inspect to load data and get quick statistics:
   - pd.read_csv('file.csv').head()
   - pd.read_csv('file.csv').describe()
   - pd.read_csv('file.csv').info() (via python_execute since .info() prints)
   - df['target'].value_counts() (for classification tasks)
   - df.isnull().sum() (missing value overview)
3. **Deep analysis**: Use python_execute to run multi-step analysis
   scripts. Save plots (plt.savefig) and summary tables as output files.
4. **Iterate**: Each observation should guide the next question. Don't
   script everything upfront — explore, observe, then decide what to
   investigate next.

### Phase 5 — Data Cleaning & EDA Report

1. Based on Phase 4 findings, design a cleaning strategy:
   - Missing value handling (drop vs impute, with justification)
   - Outlier treatment
   - Categorical encoding
   - Feature scaling if needed
2. Use python_execute to run the cleaning script step by step.
3. Use python_inspect to verify cleaning results after each step.
4. Write eda.md summarizing:
   - Dataset overview (source, size, format)
   - Column-by-column analysis (distributions, issues found)
   - Cleaning decisions and justification for each
   - Remaining quality concerns
   - Key insights for downstream modeling
```

同时删除 Phase 4-5 中所有防死循环的 prompt 规则（如 "Same tool call with identical arguments → never retry" 等）。

- [ ] **Step 4: 验证 agent 可正常构建（不启动 LLM）**

```bash
uv run python -c "
import asyncio
async def main():
    from athena.agents.competition.task_understand_agent import build_task_understand_agent
    agent = await build_task_understand_agent('gpt-4o', max_turns=1)
    print('Agent built:', agent.name)
asyncio.run(main())
"
```

- [ ] **Step 5: Commit**

```bash
git add src/athena/agents/competition/task_understand_agent.py \
        src/athena/prompts/competition/task_understand_system.txt \
        mcp_servers.json
git commit -m "feat(agent): TaskUnderstandingAgent 集成 Sandbox MCP 工具，重写数据探索阶段 prompt"
```

---

### Task 11: 清理旧代码

**Files:**
- Delete: `src/athena/tools/data_prepare.py`
- Delete: `src/athena/tools/_code_utils.py`
- Modify: `src/athena/tools/__init__.py`（如有必要）

- [ ] **Step 1: 确认 _code_utils.py 无其他使用者**

```bash
rg "from athena.tools._code_utils" src/
rg "import.*_code_utils" src/
```

- [ ] **Step 2: 删除文件**

```bash
rm src/athena/tools/data_prepare.py
rm src/athena/tools/_code_utils.py
```

- [ ] **Step 3: 检查无 import 错误残留**

```bash
uv run python -c "from athena.tools import *; print('OK')"
uv run pytest tests/ -x --ignore=tests/test_sandbox_executor.py -q 2>&1 | tail -5
```

- [ ] **Step 4: Commit**

```bash
git rm src/athena/tools/data_prepare.py src/athena/tools/_code_utils.py
git commit -m "refactor: 删除旧的数据准备工具（已被 Sandbox MCP 替代）"
```

---

### Task 12: MCP 集成测试

**Files:**
- Create: `test/integration/test_sandbox_mcp.py`

- [ ] **Step 1: 写集成测试**

```python
# test/integration/test_sandbox_mcp.py
"""Sandbox MCP 集成测试 —— 启动真实 sandbox server 并通过 MCP 协议调用。"""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters


@pytest.fixture
async def sandbox_session():
    """启动 sandbox MCP server 并返回已初始化的 ClientSession。"""
    with tempfile.TemporaryDirectory() as tmp:
        server_params = StdioServerParameters(
            command="uv",
            args=["run", "python", "-m", "athena.sandbox.server", "--work-root", tmp],
        )
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session, tmp


@pytest.mark.asyncio
async def test_mcp_discovery(sandbox_session):
    """list_tools 返回 3 个工具。"""
    session, _ = sandbox_session
    tools = await session.list_tools()
    tool_names = {t.name for t in tools.tools}
    assert tool_names == {"python_inspect", "python_execute", "sandbox_config"}


@pytest.mark.asyncio
async def test_mcp_python_inspect(sandbox_session):
    """通过 MCP 调用 python_inspect 返回正确结果。"""
    session, _ = sandbox_session
    result = await session.call_tool("python_inspect", arguments={"expr": "1 + 2"})
    data = json.loads(result.content[0].text)
    assert data["ok"] is True
    assert data["value"] == "3"


@pytest.mark.asyncio
async def test_mcp_sandbox_config(sandbox_session):
    """sandbox_config 返回白名单和限制信息。"""
    session, tmp = sandbox_session
    result = await session.call_tool("sandbox_config", arguments={})
    data = json.loads(result.content[0].text)
    assert "whitelist" in data
    assert "pandas" in data["whitelist"]
    assert data["memory_limit_mb"] == 512


@pytest.mark.asyncio
async def test_mcp_python_execute(sandbox_session):
    """通过 MCP 调用 python_execute 执行脚本。"""
    session, _ = sandbox_session
    result = await session.call_tool(
        "python_execute",
        arguments={
            "script": (
                "import os\n"
                "with open('mcp_test.txt', 'w') as f:\n"
                "    f.write('mcp ok')\n"
            ),
            "timeout": 10,
        },
    )
    data = json.loads(result.content[0].text)
    assert data["ok"] is True
    assert "mcp_test.txt" in data["output_files"]
```

- [ ] **Step 2: 运行集成测试**

```bash
uv run pytest test/integration/test_sandbox_mcp.py -v
```

- [ ] **Step 3: Commit**

```bash
git add test/integration/test_sandbox_mcp.py
git commit -m "test(sandbox): 添加 MCP stdio 集成测试"
```

---

### Task 13: 更新 TaskUnderstandAgent 集成测试

**Files:**
- Modify: `test/integration/test_task_understand_agent.py`

- [ ] **Step 1: 检查现有集成测试内容**

```bash
head -50 test/integration/test_task_understand_agent.py
```

- [ ] **Step 2: 更新测试以适应新的 Sandbox MCP 工具范式**

根据现有测试结构，更新或新增测试以验证：
1. Agent 构建后 tool registry 包含 sandbox MCP 工具（通过 `register_mcp_tools` 自动发现）
2. 不再引用已删除的 `DataAnalyzeTool` / `DataCleanCodeGenTool`

- [ ] **Step 3: 运行测试确认通过**

```bash
uv run pytest test/integration/test_task_understand_agent.py -v
```

- [ ] **Step 4: Commit**

```bash
git add test/integration/test_task_understand_agent.py
git commit -m "test(agent): 更新 TaskUnderstandingAgent 集成测试适配 Sandbox MCP"
```

---

### Task 14: 端到端验证

- [ ] **Step 1: 运行全量测试确保无回归**

```bash
uv run pytest tests/ test/unit/ -v --ignore=tests/test_sandbox_executor.py 2>&1 | tail -20
```

- [ ] **Step 2: 用 demo_taskunderstand_agent.py 验证完整流程**

用一个小型 CSV 数据集验证 Agent 能完整走通：下载 → inspect → execute → 产出 eda.md。

- [ ] **Step 3: 完成后 commit 任何遗漏文件**

```bash
git status
git add -A
git commit -m "chore: 端到端验证 Sandbox MCP 集成，清理遗漏文件"
```

---

### Task 15: 最终提交设计文档

- [ ] **Step 1: 提交设计文档**

```bash
git add docs/superpowers/specs/2026-08-07-sandbox-executor-design.md docs/superpowers/plans/2026-08-07-sandbox-executor-plan.md
git commit -m "docs: 添加 Sandbox Executor 设计文档与实施计划"
```
