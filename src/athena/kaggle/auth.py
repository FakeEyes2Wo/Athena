"""Kaggle 凭据：新版 token 走 Bearer，旧式 username/key 走 Basic。"""

import base64
import json
from dataclasses import dataclass
from pathlib import Path

KAGGLE_TOKEN_ENV = "KAGGLE_API_TOKEN"
DEFAULT_ACCESS_TOKEN_PATH = Path("~/.kaggle/access_token")
DEFAULT_CONFIG_PATH = Path("~/.kaggle/kaggle.json")


@dataclass(frozen=True, slots=True)
class KaggleCredentials:
    bearer_token: str = ""
    username: str = ""
    key: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.bearer_token or (self.username and self.key))

    def auth_header(self) -> str | None:
        if self.bearer_token:
            return f"Bearer {self.bearer_token}"
        if self.username and self.key:
            raw = f"{self.username}:{self.key}"
            return "Basic " + base64.b64encode(raw.encode()).decode("ascii")
        return None


def _read_token_file(path: Path) -> str:
    expanded = path.expanduser()
    if not expanded.is_file():
        return ""
    return expanded.read_text(encoding="utf-8").strip()


def _read_config_credentials(path: Path) -> tuple[str, str]:
    expanded = path.expanduser()
    if not expanded.is_file():
        return "", ""
    try:
        payload = json.loads(expanded.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "", ""
    if not isinstance(payload, dict):
        return "", ""
    return str(payload.get("username", "")).strip(), str(payload.get("key", "")).strip()


def resolve_credentials(
    explicit_token: str = "",
    explicit_username: str = "",
    explicit_key: str = "",
    *,
    access_token_path: Path | None = None,
    config_path: Path | None = None,
) -> KaggleCredentials:
    """显式参数优先，其次回退磁盘文件。"""
    token = explicit_token.strip() or _read_token_file(
        access_token_path or DEFAULT_ACCESS_TOKEN_PATH
    )
    username = explicit_username.strip()
    key = explicit_key.strip()
    if not (username and key):
        file_username, file_key = _read_config_credentials(
            config_path or DEFAULT_CONFIG_PATH
        )
        username = username or file_username
        key = key or file_key
    return KaggleCredentials(bearer_token=token, username=username, key=key)
