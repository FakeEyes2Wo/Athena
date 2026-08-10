"""Research workflow runtime and paper-research services."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from athena.research.runtime import ResearchMethod, ResearchRuntime

__all__ = [
    "ResearchMethod",
    "ResearchRuntime",
]


def __getattr__(name: str):
    if name in __all__:
        from athena.research import runtime  # 延迟导入避免循环依赖

        return getattr(runtime, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
