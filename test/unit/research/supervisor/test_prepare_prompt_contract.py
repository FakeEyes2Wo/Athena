"""PREPARE prompt dependency-location contract.

The deterministic runner (``ExecutionRuntime`` + ``PlanRunner``) executes a
manifest's bare ``python`` command with a PATH whose only project venv entry is
``$ATHENA_ENV_ROOT/.venv`` (see ``EnvironmentManager.build_env``). If the PREPARE
agent installs numpy/pandas/sklearn anywhere else — a nested workspace venv or
the ambient host venv — bare ``python`` resolves to a dependency-less interpreter
and PREPARE loops forever on ``ModuleNotFoundError`` (never reaching SEARCH).

The prompt is the single point of contract for where dependencies must live, so
it must name ``$ATHENA_ENV_ROOT`` and direct the manifest command to bare
``python`` rather than a nested/absolute venv executable.
"""

from athena.agents.prompt_agent import load_prompt


def test_prepare_prompt_directs_dependencies_into_env_root() -> None:
    prompt = load_prompt("prepare")
    assert "$ATHENA_ENV_ROOT" in prompt
    assert "uv add --project" in prompt
    assert "uv sync" in prompt


def test_prepare_prompt_requires_bare_python_manifest_command() -> None:
    prompt = load_prompt("prepare")
    # The manifest command must resolve through the environment-root venv.
    assert '"python"' in prompt
    assert "$ATHENA_ENV_ROOT/.venv" in prompt
