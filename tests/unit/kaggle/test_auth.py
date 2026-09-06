"""Kaggle 凭据解析与认证头构造的单元测试。"""

import base64

from athena.kaggle.auth import KaggleCredentials, resolve_credentials


def test_bearer_header() -> None:
    creds = KaggleCredentials(bearer_token="KGAT_abc")
    assert creds.auth_header() == "Bearer KGAT_abc"


def test_basic_header_encodes_username_and_key() -> None:
    creds = KaggleCredentials(username="u", key="k")
    expected = "Basic " + base64.b64encode(b"u:k").decode("ascii")
    assert creds.auth_header() == expected


def test_unconfigured_has_no_header() -> None:
    assert KaggleCredentials().auth_header() is None


def test_explicit_token_beats_access_token_file(tmp_path) -> None:
    token_file = tmp_path / "access_token"
    token_file.write_text("file_token\n", encoding="utf-8")
    creds = resolve_credentials(
        KaggleCredentials(bearer_token="env_token"), access_token_path=token_file
    )
    assert creds.bearer_token == "env_token"


def test_access_token_file_fallback(tmp_path) -> None:
    token_file = tmp_path / "access_token"
    token_file.write_text("  file_token  \n", encoding="utf-8")
    creds = resolve_credentials(access_token_path=token_file)
    assert creds.bearer_token == "file_token"


def test_config_file_credentials(tmp_path) -> None:
    config = tmp_path / "kaggle.json"
    config.write_text('{"username": "u", "key": "k"}', encoding="utf-8")
    creds = resolve_credentials(config_path=config)
    assert creds.username == "u"
    assert creds.key == "k"
    assert creds.auth_header() is not None


def test_missing_files_yield_unconfigured(tmp_path) -> None:
    creds = resolve_credentials(
        access_token_path=tmp_path / "nope", config_path=tmp_path / "none.json"
    )
    assert creds.auth_header() is None
