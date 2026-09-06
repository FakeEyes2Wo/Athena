"""Baseline ideator-only web research tool coverage."""

from types import SimpleNamespace

from athena.core.tool import ToolRegistry, tool
from athena.research.runtime.bootstrap import baseline_ideator_tools, ideator_tools


@tool
async def kaggle_lookup() -> dict:
    """Return a deterministic Kaggle lookup result."""
    return {}


@tool
async def paper_read() -> dict:
    """Return a deterministic paper read result."""
    return {}


def test_only_baseline_ideator_tools_include_web() -> None:
    runtime = SimpleNamespace(
        kaggle_tools=lambda _kind: None,
        corpus_tools=lambda **_kwargs: None,
    )

    assert ideator_tools(runtime)() is None
    names = {spec.name for spec in baseline_ideator_tools(runtime)().specs}

    assert names == {"web_fetch", "web_search"}


def test_baseline_ideator_tools_merge_web_with_existing_ideator_tools() -> None:
    kaggle = ToolRegistry()
    kaggle.register(kaggle_lookup)
    corpus = ToolRegistry()
    corpus.register(paper_read)
    runtime = SimpleNamespace(
        kaggle_tools=lambda _kind: kaggle,
        corpus_tools=lambda **_kwargs: corpus,
    )

    ordinary_names = {spec.name for spec in ideator_tools(runtime)().specs}
    baseline_names = {spec.name for spec in baseline_ideator_tools(runtime)().specs}

    assert ordinary_names == {"kaggle_lookup", "paper_read"}
    assert baseline_names == {"kaggle_lookup", "paper_read", "web_fetch", "web_search"}
