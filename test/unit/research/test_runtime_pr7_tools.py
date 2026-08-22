"""ResearchRuntime 的 PR7 工具装配（HF / MCP）测试。"""

import pytest

from test.unit._support import make_project

HF_NAMES = {
    "hf_dataset_search",
    "hf_dataset_download",
    "hf_model_search",
    "hf_model_download",
}


def _names(registry) -> set[str]:
    return {spec.name for spec in registry.specs}


@pytest.mark.asyncio
async def test_hf_tools_bound_to_project(tmp_path) -> None:
    rt = make_project(tmp_path)
    try:
        registry = rt.hf_tools()
        assert _names(registry) == HF_NAMES
        # 绑定到项目工作区而非 cwd：每个工具的落盘目录都在项目 pr7_tools 下
        pr7_root = str(tmp_path / "workspaces" / "pr7_tools")
        for name in HF_NAMES:
            assert str(registry.resolve(name).output_dir).startswith(pr7_root)
    finally:
        await rt.aclose()


@pytest.mark.asyncio
async def test_init_mcp_tools_noop_without_config(tmp_path) -> None:
    rt = make_project(tmp_path)
    try:
        await rt.init_mcp_tools()
        assert rt._mcp_registry is not None
        assert _names(rt._mcp_registry) == set()
        assert rt._mcp_managers == []
        await rt.init_mcp_tools()  # 幂等：二次调用不重连
        assert _names(rt._mcp_registry) == set()
    finally:
        await rt.aclose()


@pytest.mark.asyncio
async def test_agent_tools_merges_hf_when_kaggle_off(tmp_path) -> None:
    rt = make_project(tmp_path)
    try:
        registry = rt.agent_tools("evaluator")
        assert registry is not None
        assert _names(registry) == HF_NAMES
    finally:
        await rt.aclose()


@pytest.mark.asyncio
async def test_baseline_ideator_tools_is_lazy_and_includes_hf(tmp_path) -> None:
    rt = make_project(tmp_path)
    try:
        provider = rt.baseline_ideator_tools()
        assert callable(provider)
        registry = provider()
        assert registry is not None
        assert _names(registry) == HF_NAMES
    finally:
        await rt.aclose()
