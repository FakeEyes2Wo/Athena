"""PREPARE prompt dependency-location contract.

The deterministic runner (``ExecutionRuntime`` + ``PlanRunner``) executes a
manifest's bare ``python`` command with a PATH whose only project venv entry is
``$ATHENA_ENV_ROOT/.venv`` (see ``EnvironmentManager.build_env``). If the PREPARE
agent installs numpy/pandas/sklearn anywhere else — a nested workspace venv or
the ambient host venv — bare ``python`` resolves to a dependency-less interpreter
and PREPARE loops forever on ``ModuleNotFoundError`` (never reaching SEARCH).

The prompt is the single point of contract for where dependencies must live, so
it must name the environment root and direct the manifest command to bare
``python`` rather than a nested/absolute venv executable. It must **not** spell
the variable in one shell's syntax: ``$ATHENA_ENV_ROOT`` expands to the empty
string under PowerShell, and ``uv add --project ""`` then fails. The shell-correct
form is rendered by ``ExecutionRuntime.env_ref`` into the Runtime block, so the
prompt points there instead of hardcoding a syntax.
"""

from athena.agents.prompt_agent import load_prompt


def test_prepare_prompt_directs_dependencies_into_env_root() -> None:
    prompt = load_prompt("prepare")
    # 意图不变（依赖装进共享环境根），但不再钉住 POSIX 字面量：PowerShell 下
    # `$ATHENA_ENV_ROOT` 展开成空串，`uv add --project ""` 直接报错。正确形式由
    # runtime_summary 的 env_ref() 按 shell 渲染，提示词只指向那里。
    assert "$ATHENA_ENV_ROOT" not in prompt
    assert "environment root" in prompt
    assert "Runtime" in prompt
    assert "uv add --project" in prompt
    assert "uv sync" in prompt


def test_prepare_prompt_requires_bare_python_manifest_command() -> None:
    prompt = load_prompt("prepare")
    # The manifest command must resolve through the environment-root venv.
    assert '"python"' in prompt
    assert "environment root's `.venv`" in prompt


def test_prepare_prompt_treats_baseline_files_as_read_only_authority_mirrors() -> None:
    prompt = load_prompt("prepare")

    for filename in (
        "BASELINE_RESEARCH.json",
        "BASELINE_RESEARCH_VERIFICATION.json",
        "BASELINE_DESIGN.md",
    ):
        assert filename in prompt
    assert "read-only local audit mirrors" in prompt
    assert "external baseline authority generation" in prompt
    assert "Never create, rewrite, overwrite, or delete" in prompt


def test_prepare_prompt_does_not_present_v1_research_fields_as_current_contract() -> (
    None
):
    prompt = load_prompt("prepare")

    for retired_field in (
        "recommended_strategy",
        "search_queries",
    ):
        assert retired_field not in prompt
    for forbidden_authority_detail in (
        "BASELINE_AUTHORITY_URL",
        "BASELINE_AUTHORITY_TOKEN",
        "expected_generation",
        "authority storage path",
        "citation threshold parameter",
    ):
        assert forbidden_authority_detail not in prompt
