"""Tests for composer and local confirmation key mapping."""

from athena_tui.keybindings import map_key
from athena_tui.state import COMPOSER, CONFIRMATION


def test_composer_maps_navigation_input_and_quit() -> None:
    assert map_key(COMPOSER, "pageup") == ("scroll_up", None)
    assert map_key(COMPOSER, "enter") == ("submit", None)
    assert map_key(COMPOSER, "c-j") == ("insert_newline", None)
    assert map_key(COMPOSER, "s-enter") == ("insert_newline", None)
    assert map_key(COMPOSER, "c-c") == ("quit", None)


def test_composer_toggles_manual_mode_with_shift_tab() -> None:
    assert map_key(COMPOSER, "s-tab") == ("toggle_mode", None)


def test_confirmation_accepts_only_local_decision_keys() -> None:
    assert map_key(CONFIRMATION, "y") == ("confirm", None)
    assert map_key(CONFIRMATION, "n") == ("cancel", None)
    assert map_key(CONFIRMATION, "enter") is None
