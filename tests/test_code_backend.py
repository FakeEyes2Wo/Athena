"""Tests for code backend contracts and real adapter integration."""

import importlib
from pathlib import Path
from types import SimpleNamespace

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


def test_qoder_adapter_construction_reports_missing_sdk(monkeypatch) -> None:
    """Qoder imports safely while construction reports a missing SDK."""
    adapter_module = importlib.import_module("athena.code.backends.qoder")

    def missing_sdk(name: str):
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(adapter_module.importlib, "import_module", missing_sdk)

    with pytest.raises(
        adapter_module.BackendUnavailableError,
        match=r"^qoder backend unavailable$",
    ):
        adapter_module.QoderBackend.from_config({})


@pytest.mark.asyncio
async def test_codex_backend_invokes_cli_and_reports_changed_files(tmp_path) -> None:
    """Codex CLI executes in the target worktree and reports real file changes."""
    from athena.code.backends.codex import CodexBackend

    calls = []

    async def runner(command, *, cwd, timeout_s):
        calls.append((command, cwd, timeout_s))
        (Path(cwd) / "run_experiment.py").write_text("print('ok')\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="generated", stderr="")

    result = await CodexBackend(runner=runner).generate(
        prompt="Create run_experiment.py",
        target_dir=str(tmp_path),
        previous_outputs=[],
        history=[],
    )

    command, cwd, timeout_s = calls[0]
    assert command[:5] == (
        "codex",
        "exec",
        "--sandbox",
        "workspace-write",
        "--ephemeral",
    )
    assert command[-3:] == ("-C", str(tmp_path), "Create run_experiment.py")
    assert cwd == str(tmp_path)
    assert timeout_s == 600
    assert result.files_created == ["run_experiment.py"]
    assert result.files_modified == []
    assert result.output == "generated"


@pytest.mark.asyncio
async def test_codex_backend_rejects_cli_failure(tmp_path) -> None:
    """A non-zero Codex process cannot be represented as successful generation."""
    from athena.code.backends import BackendUnavailableError
    from athena.code.backends.codex import CodexBackend

    async def runner(command, *, cwd, timeout_s):
        return SimpleNamespace(returncode=2, stdout="", stderr="auth failed")

    with pytest.raises(BackendUnavailableError, match="auth failed"):
        await CodexBackend(runner=runner).generate(
            prompt="Create run_experiment.py",
            target_dir=str(tmp_path),
            previous_outputs=[],
            history=[],
        )


@pytest.mark.asyncio
async def test_qoder_backend_uses_restricted_sdk_options(tmp_path) -> None:
    """Qoder receives a writable cwd while shell execution remains disabled."""
    from athena.code.backends.qoder import QoderBackend

    observed = {}

    class TextBlock:
        def __init__(self, text):
            self.text = text

    class AssistantMessage:
        def __init__(self, content):
            self.content = content

    class Options:
        def __init__(self, **kwargs):
            observed["options"] = kwargs

    async def query(*, prompt, options):
        observed["prompt"] = prompt
        (tmp_path / "run_experiment.py").write_text("print('ok')\n", encoding="utf-8")
        yield AssistantMessage([TextBlock("generated")])

    sdk = SimpleNamespace(
        AssistantMessage=AssistantMessage,
        TextBlock=TextBlock,
        QoderAgentOptions=Options,
        qodercli_auth=lambda: "local-auth",
        access_token_from_env=lambda: "pat-auth",
        query=query,
    )

    result = await QoderBackend(sdk=sdk, environ={}).generate(
        prompt="Create run_experiment.py",
        target_dir=str(tmp_path),
        previous_outputs=[],
        history=[],
    )

    assert observed["prompt"] == "Create run_experiment.py"
    assert observed["options"] == {
        "auth": "local-auth",
        "cwd": tmp_path,
        "allowed_tools": ["Read", "Edit"],
        "disallowed_tools": ["Bash"],
        "permission_mode": "acceptEdits",
    }
    assert result.files_created == ["run_experiment.py"]
    assert result.output == "generated"


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
