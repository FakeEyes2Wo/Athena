"""Key-to-intent mapping for composer and local confirmation modes."""

from athena_tui.state import CONFIRMATION


def map_key(mode: str, key: str) -> tuple[str, object | None] | None:
    """Map one normalized key to a local TUI intent."""
    if mode == CONFIRMATION:
        if key == "y":
            return ("confirm", None)
        if key in {"n", "esc"}:
            return ("cancel", None)
        return None
    return {
        "pageup": ("scroll_up", None),
        "pagedown": ("scroll_down", None),
        "end": ("scroll_end", None),
        "?": ("open_overlay", None),
        "esc": ("close_overlay", None),
        "c-c": ("quit", None),
        "enter": ("submit", None),
        "c-j": ("insert_newline", None),
        "s-enter": ("insert_newline", None),
        "s-tab": ("toggle_mode", None),
    }.get(key)
