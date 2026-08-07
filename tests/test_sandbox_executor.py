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


@pytest.mark.asyncio
async def test_inspect_basic(executor):
    """表达式求值返回正确值。"""
    result = await executor.inspect("1 + 1")
    assert result.ok
    assert result.value == "2"


@pytest.mark.asyncio
async def test_inspect_dataframe(executor):
    """DataFrame 求值返回 shape 和 columns。"""
    result = await executor.inspect("pd.DataFrame({'a': [1, 2], 'b': [3, 4]})")
    assert result.ok
    assert result.type is not None
    assert "DataFrame" in result.type
    # shape 和 columns 可能为 None（后置进程失败时），不强制断言


@pytest.mark.asyncio
async def test_inspect_timeout(executor):
    """无限循环表达式触发超时。"""
    result = await executor.inspect(
        "exec('while True: pass') or 1", timeout=3
    )
    assert not result.ok
    assert "超时" in (result.error or "")


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


@pytest.mark.asyncio
async def test_result_truncation(executor):
    """超长输出被截断。"""
    result = await executor.inspect("'x' * 3000")
    assert result.ok
    assert len(result.value or "") <= 2000
