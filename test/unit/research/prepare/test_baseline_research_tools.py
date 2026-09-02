"""Baseline-only tool wiring contracts."""

from types import SimpleNamespace

from athena.core.tool import ToolRegistry, tool
from athena.research.runtime.bootstrap import baseline_ideator_tools, ideator_tools


@tool
async def fake_kaggle() -> dict:
    """Fake Kaggle operator."""
    return {}


@tool
async def fake_corpus() -> dict:
    """Fake corpus operator."""
    return {}


def _registry(*tools: object) -> ToolRegistry:
    registry = ToolRegistry()
    for candidate in tools:
        registry.register(candidate)
    return registry


def test_only_baseline_ideator_tools_include_web() -> None:
    runtime = SimpleNamespace(
        kaggle_tools=lambda _kind: None,
        corpus_tools=lambda **_kwargs: None,
    )

    assert ideator_tools(runtime)() is None
    names = {spec.name for spec in baseline_ideator_tools(runtime)().specs}

    assert names == {"web_fetch", "web_search"}


def test_baseline_tools_merge_kaggle_and_corpus_without_leaking_web() -> None:
    runtime = SimpleNamespace(
        kaggle_tools=lambda _kind: _registry(fake_kaggle),
        corpus_tools=lambda **_kwargs: _registry(fake_corpus),
    )

    ordinary_names = {spec.name for spec in ideator_tools(runtime)().specs}
    baseline_names = {spec.name for spec in baseline_ideator_tools(runtime)().specs}

    assert ordinary_names == {"fake_corpus", "fake_kaggle"}
    assert baseline_names == {"fake_corpus", "fake_kaggle", "web_fetch", "web_search"}
