import json
import tempfile
from pathlib import Path

from athena.tools.mcp.config import McpServerConfig, load_mcp_servers


def test_load_parses_servers_and_auth_env():
    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "mcp_servers.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "servers": [
                        {
                            "name": "kaggle",
                            "transport": "streamable_http",
                            "url": "https://www.kaggle.com/mcp",
                            "auth": {"type": "bearer", "env": "KAGGLE_API_TOKEN"},
                            "tool_prefix": "kaggle__",
                            "pinned_tools": [],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        servers = load_mcp_servers(cfg_path)
    assert len(servers) == 1
    s = servers[0]
    assert s.name == "kaggle"
    assert s.transport == "streamable_http"
    assert s.url == "https://www.kaggle.com/mcp"
    assert s.auth_env == "KAGGLE_API_TOKEN"
    assert s.tool_prefix == "kaggle__"
    assert s.pinned_tools == []


def test_load_missing_file_returns_empty():
    assert load_mcp_servers(Path("/nonexistent/mcp_servers.json")) == []


def test_default_prefix_is_name_double_underscore():
    assert McpServerConfig(name="kaggle").prefix == "kaggle__"
    assert McpServerConfig(name="hf", tool_prefix="hf_").prefix == "hf_"
