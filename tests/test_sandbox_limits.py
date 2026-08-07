"""SandboxLimits 资源限制模块的单元测试。"""

import sys

import pytest

from athena.sandbox.limits import SandboxLimits


def test_memory_mb_default():
    # 默认硬内存上限为 2048 MiB（需容纳 pandas/numpy/matplotlib 等数据科学栈）
    assert SandboxLimits.MEMORY_MB == 2048


def test_ensure_platform_support_current_platform():
    # 当前平台（win32 / linux / darwin）应通过校验
    SandboxLimits.ensure_platform_support()


def test_ensure_platform_support_unsupported(monkeypatch):
    monkeypatch.setattr(sys, "platform", "plan9")
    with pytest.raises(RuntimeError):
        SandboxLimits.ensure_platform_support()
