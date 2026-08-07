"""Test script and notebook execution."""

import os
import tempfile
import pytest
from athena.code.runner import run_script


@pytest.mark.asyncio
async def test_run_script_uses_the_active_python_environment(tmp_path) -> None:
    """Generated experiments inherit the environment that launched Athena."""
    script = tmp_path / "interpreter.py"
    script.write_text("import pandas\nprint('ACTIVE_ENV')\n", encoding="utf-8")

    result = await run_script(str(script), cwd=str(tmp_path), timeout_s=10)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ACTIVE_ENV"


@pytest.mark.asyncio
async def test_run_script_success():
    """Running a valid script returns stdout with returncode 0."""
    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "hello.py")
        with open(script, "w") as f:
            f.write("print('hello world')\n")
        result = await run_script(script, cwd=tmp, timeout_s=10)
        assert result.returncode == 0
        assert "hello world" in result.stdout


@pytest.mark.asyncio
async def test_run_script_stderr():
    """Script writing to stderr captures it separately."""
    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "err.py")
        with open(script, "w") as f:
            f.write("import sys; sys.stderr.write('bad'); sys.exit(1)\n")
        result = await run_script(script, cwd=tmp, timeout_s=10)
        assert result.returncode == 1
        assert "bad" in result.stderr


@pytest.mark.asyncio
async def test_run_script_timeout():
    """Script exceeding timeout returns TIMEOUT-like result."""
    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "sleepy.py")
        with open(script, "w") as f:
            f.write("import time; time.sleep(10)\n")
        result = await run_script(script, cwd=tmp, timeout_s=1)
        assert result.returncode != 0
        assert "timeout" in result.stderr.lower() or result.returncode == -1


@pytest.mark.asyncio
async def test_run_script_creates_files():
    """Script that writes files; those files are listed in result.files."""
    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "gen.py")
        with open(script, "w") as f:
            f.write("with open('out.txt', 'w') as fh: fh.write('data')\n")
        result = await run_script(script, cwd=tmp, timeout_s=10)
        assert result.returncode == 0
        assert any("out.txt" in f for f in result.files)
