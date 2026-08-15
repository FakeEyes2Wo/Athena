"""GUI-facing service layer over the research runtime.

Exposes graph algorithms, settings, LLM I/O traces, and experiment management
as plain JSON-friendly methods, consumed by ``gui_gateway``.
"""

from athena.gui.service import GuiService

__all__ = ["GuiService"]
