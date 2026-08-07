"""Tests for code backend contracts and optional adapter loading."""

import importlib

import pytest
from athena.code.backends.base import CodeBackend, GenerationResult, ExecutionOutput


def test_code_backend_is_abstract():
    """CodeBackend cannot be instantiated without concrete generate()."""
    with pytest.raises(TypeError):
        CodeBackend()  # type: ignore


def test_generation_result_defaults():
    """GenerationResult has sensible defaults."""
    r = GenerationResult()
    assert r.files_created == []
    assert r.files_modified == []
    assert r.output == ""


def test_execution_output_defaults():
    """ExecutionOutput has sensible defaults."""
    e = ExecutionOutput(stdout="ok", stderr="", returncode=0)
    assert e.returncode == 0
    assert e.files == []


@pytest.mark.parametrize(
    ("module_name", "class_name"),
    [("codex", "CodexBackend"), ("qoder", "QoderBackend")],
)
def test_optional_adapter_construction_reports_missing_sdk(
    monkeypatch, module_name: str, class_name: str
) -> None:
    """Adapter modules import safely, while construction reports a missing SDK."""
    adapter_module = importlib.import_module(
        f"athena.code.backends.{module_name}"
    )
    backend_type = getattr(adapter_module, class_name)

    def missing_sdk(name: str):
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(adapter_module.importlib, "import_module", missing_sdk)

    with pytest.raises(
        adapter_module.BackendUnavailableError,
        match=rf"^{module_name} backend unavailable$",
    ):
        backend_type.from_config({})


@pytest.mark.parametrize("name", ["codex", "qoder"])
def test_load_backend_routes_config_to_named_adapter(monkeypatch, name: str) -> None:
    """The backend factory routes config to the named adapter."""
    backends = importlib.import_module("athena.code.backends")

    def fake_adapter(config: dict[str, object]) -> tuple[str, dict[str, object]]:
        return name, config

    backend_type = getattr(backends, f"{name.title()}Backend")
    monkeypatch.setattr(backend_type, "from_config", staticmethod(fake_adapter))

    assert backends.load_backend(name, api_key="fake") == (
        name,
        {"api_key": "fake"},
    )
