"""Protocol contract test: lock Python / Rust / TS GUI method names in sync.

Reads the three layers' single sources of truth and asserts they agree, so a
rename on one side fails loudly here:

* ``GuiRequestHandler.SUPPORTED_METHODS`` (Python gateway, §3.2 of
  ``docs/athena-gui-design.md``).
* ``tauri::generate_handler![...]`` command names in ``lib.rs`` (Rust), mapped
  to Python method names via ``COMMAND_TO_METHOD``.
* ``dispatch``'s ``if method == / in`` routing literals (Python), so the
  constant table can never drift from the actual routing branches.

This is a pure static test — no runtime, no network.
"""

from __future__ import annotations

import re
from pathlib import Path

from gui_gateway.handler import SUPPORTED_METHODS

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB_RS = REPO_ROOT / "athena-gui" / "src-tauri" / "src" / "lib.rs"
HANDLER_PY = REPO_ROOT / "src" / "gui_gateway" / "handler.py"

# §3.2 权威方法总表（+ set_project_root，非 §3.2 但为 handler 扩展的切换项目目录）。
CANONICAL_METHODS: frozenset[str] = frozenset(
    {
        # A. 运行时控制
        "ping",
        "start",
        "start_task",
        "message",
        "pause",
        "resume",
        "stop",
        "start_search",
        "start_validation",
        "generate_report",
        # B. 状态与树
        "state_get",
        "tree_get",
        "tree_save",
        "tree_load",
        "sessions_list",
        "sessions_list_for",
        "session_switch",
        "session_delete",
        "eda_report",
        # C. 假设图与算法
        "hypothesis_graph",
        "graph_algorithms",
        "graph_algorithm",
        # D. 设置
        "settings_get",
        "settings_set",
        "set_project_root",
        # E. LLM I/O 轨迹
        "traces_list",
        "trace_get",
        # F. 实验管理
        "experiments_list",
        "experiment_get",
        "experiment_transition",
        "experiment_set_sota",
        # G. 人类交互
        "human_pending",
        "human_reply",
    }
)

# Rust 命令名（tauri 命令函数名）→ Python 方法名。这是唯一映射表；未来 TS 后端
# 复刻同一份方法表即可保持双后端契约一致。
COMMAND_TO_METHOD: dict[str, str] = {
    "message": "message",
    "start_search": "start_search",
    "pause_search": "pause",
    "resume_search": "resume",
    "stop_search": "stop",
    "start_validation": "start_validation",
    "generate_report": "generate_report",
    "tree_get": "tree_get",
    "tree_save": "tree_save",
    "tree_load": "tree_load",
    "sessions_list": "sessions_list",
    "sessions_list_for": "sessions_list_for",
    "session_switch": "session_switch",
    "session_delete": "session_delete",
    "eda_report": "eda_report",
    "settings_get": "settings_get",
    "settings_set": "settings_set",
    "set_project_root": "set_project_root",
    "traces_list": "traces_list",
    "trace_get": "trace_get",
    "hypothesis_graph": "hypothesis_graph",
    "graph_algorithms": "graph_algorithms",
    "graph_algorithm": "graph_algorithm",
    "experiments_list": "experiments_list",
    "experiment_get": "experiment_get",
    "experiment_transition": "experiment_transition",
    "experiment_set_sota": "experiment_set_sota",
    "state_get": "state_get",
    "human_pending": "human_pending",
    "human_reply": "human_reply",
}


def _rust_command_names() -> set[str]:
    """Extract ``commands::<mod>::<fn>`` names from ``generate_handler![...]``."""
    text = LIB_RS.read_text(encoding="utf-8")
    return set(re.findall(r"commands::\w+::(\w+)", text))


def _dispatch_route_literals() -> set[str]:
    """Extract the method names routed by ``dispatch``'s if/elif chain."""
    text = HANDLER_PY.read_text(encoding="utf-8")
    routed: set[str] = set()

    # `if method == "name":`
    routed.update(re.findall(r'if method == "([a-z_]+)"', text))
    # `if method in {"a", "b", "c"}:`
    for block in re.findall(r'if method in \{([^}]*)\}', text):
        routed.update(re.findall(r'"([a-z_]+)"', block))
    return routed


def test_supported_methods_match_canonical_protocol() -> None:
    """The Python gateway constant is exactly the §3.2 method table."""
    assert SUPPORTED_METHODS == CANONICAL_METHODS


def test_dispatch_routes_every_supported_method() -> None:
    """Every constant-table method has a real routing branch in ``dispatch``.

    Guards against adding a name to ``SUPPORTED_METHODS`` while forgetting the
    ``if method == ...`` branch (which would otherwise fall through to
    ``unsupported GUI method``).
    """
    assert _dispatch_route_literals() == SUPPORTED_METHODS


def test_rust_commands_map_to_supported_methods() -> None:
    """Every registered Tauri command maps to a gateway method, and vice versa."""
    commands = _rust_command_names()
    assert commands == set(COMMAND_TO_METHOD), (
        "lib.rs 命令与 COMMAND_TO_METHOD 表不一致；若新增/改名命令，请同步映射表"
    )
    mapped_methods = set(COMMAND_TO_METHOD.values())
    assert mapped_methods <= SUPPORTED_METHODS, (
        f"以下 Rust 命令映射的方法未被网关支持: {sorted(mapped_methods - SUPPORTED_METHODS)}"
    )
    # 仅 ping/start/start_task 为 WebSocket 专用（无 Tauri 命令），其余方法均应被命令覆盖。
    ws_only = {"ping", "start", "start_task"}
    assert SUPPORTED_METHODS - mapped_methods == ws_only


