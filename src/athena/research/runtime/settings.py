"""GUI-facing settings projection and whitelisted runtime updates."""

import os
from pathlib import Path
from typing import Any

from athena.execution.compute_config import parse_compute_config
from athena.execution.pool import GpuPool
from athena.research.supervisor.deps import resolve_validation_mode

MODEL_CONNECTION_ENV_VARS: dict[str, str] = {
    "provider": "LLM_PROVIDER",
    "base_url": "BASE_URL",
    "model_name": "MODEL_NAME",
    "llm_api_key": "LLM_API_KEY",
}
ALLOWED_MODEL_PROVIDERS = ("deepseek", "openai", "qwen")

INTEGER_SETTINGS: dict[str, tuple[int, int | None]] = {
    "concurrency": (1, None),
    "search_limit": (0, None),
    "ideator_count": (1, 8),
    "hypotheses_per_ideator": (1, 5),
    "experiment_timeout_s": (1, None),
}
POLICY_FLAGS = ("auto_validate", "skip_validate")
DURABLE_SETTINGS = frozenset({*INTEGER_SETTINGS, "handoff_sources", "data_root"})
SETTINGS_WHITELIST = frozenset(
    {
        *DURABLE_SETTINGS,
        *POLICY_FLAGS,
        "direction",
        "tolerance",
        "manual_mode",
        "ideation",
        "model_connection",
        "compute",
    }
)


def _mask_secret(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}{'*' * 8}{value[-4:]}"


def _upsert_dotenv(path: Path, updates: dict[str, str]) -> None:
    """Update named values while retaining unrelated lines and comments."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    updated: set[str] = set()
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                output.append(f"{key}={updates[key]}")
                updated.add(key)
                continue
        output.append(line)
    output.extend(
        f"{key}={value}" for key, value in updates.items() if key not in updated
    )
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def _integer(name: str, value: Any, minimum: int, maximum: int | None) -> int:
    valid = isinstance(value, int) and value >= minimum
    if maximum is not None:
        valid = valid and value <= maximum
    if not valid:
        if maximum is None:
            requirement = f"an integer >= {minimum}"
        else:
            requirement = f"an integer between {minimum} and {maximum}"
        raise ValueError(f"{name} must be {requirement}")
    return value


def _compute_snapshot(runtime: Any) -> dict[str, Any]:
    compute = runtime.session.compute.config
    if compute is None:
        return {
            "mode": "local",
            "placement": "pack",
            "fallback": "never",
            "gpus_per_experiment": 1,
            "queue_timeout_s": None,
            "hosts": [],
        }
    return {
        "mode": compute.mode,
        "placement": compute.placement,
        "fallback": compute.fallback,
        "gpus_per_experiment": compute.gpus_per_experiment,
        "queue_timeout_s": compute.queue_timeout_s,
        "hosts": [
            {
                "name": host.name,
                "ssh": host.alias,
                "gpus": list(host.gpus) if host.gpus is not None else "auto",
                "max_leases": host.max_leases,
            }
            for host in compute.hosts
        ],
    }


def snapshot(runtime: Any) -> dict[str, Any]:
    """Return the current GUI-facing settings projection."""
    state = runtime.state
    options = runtime.session.options
    return {
        "project_root": str(runtime.root),
        "model": runtime.model,
        "concurrency": state.concurrency,
        "search_limit": state.search_limit,
        "ideation": runtime.ideation,
        "ideator_count": state.ideator_count,
        "hypotheses_per_ideator": state.hypotheses_per_ideator,
        "handoff_sources": state.handoff_sources,
        "direction": runtime.direction,
        "tolerance": options.tolerance,
        "auto_validate": options.auto_validate,
        "skip_validate": options.skip_validate,
        "manual_mode": state.manual_mode,
        "phase": state.phase,
        "status": state.status,
        "data_root": state.data_root,
        "experiment_timeout_s": state.experiment_timeout_s,
        "compute": _compute_snapshot(runtime),
        "model_connection": {
            "provider": os.environ.get("LLM_PROVIDER") or "deepseek",
            "base_url": os.environ.get("BASE_URL") or "",
            "model_name": os.environ.get("MODEL_NAME") or "",
            "llm_api_key": _mask_secret(
                os.environ.get("LLM_API_KEY")
                or os.environ.get("DEEPSEEK_API_KEY")
                or os.environ.get("OPENAI_API_KEY")
            ),
        },
    }


def _apply_research_settings(runtime: Any, patch: dict[str, Any]) -> None:
    state = runtime.state
    for name in ("concurrency", "search_limit"):
        if name in patch:
            minimum, maximum = INTEGER_SETTINGS[name]
            setattr(state, name, _integer(name, patch[name], minimum, maximum))

    if "ideation" in patch:
        ideation = patch["ideation"]
        if ideation not in {"ideageneration", "baseline", "debate"}:
            raise ValueError(
                "ideation must be 'ideageneration', 'baseline', or 'debate'"
            )
        if ideation != runtime.ideation:
            runtime.session.options.ideation = ideation
            runtime.registry.unregister("ideator")

    for name in ("ideator_count", "hypotheses_per_ideator"):
        if name in patch:
            minimum, maximum = INTEGER_SETTINGS[name]
            setattr(state, name, _integer(name, patch[name], minimum, maximum))

    if "handoff_sources" in patch:
        sources = patch["handoff_sources"]
        if not isinstance(sources, list) or any(
            source not in {"kaggle", "literature"} for source in sources
        ):
            raise ValueError(
                "handoff_sources must be a list containing only "
                "'kaggle' and 'literature'"
            )
        state.handoff_sources = sources

    if "experiment_timeout_s" in patch:
        minimum, maximum = INTEGER_SETTINGS["experiment_timeout_s"]
        state.experiment_timeout_s = _integer(
            "experiment_timeout_s",
            patch["experiment_timeout_s"],
            minimum,
            maximum,
        )


async def _apply_policy_settings(runtime: Any, patch: dict[str, Any]) -> None:
    state = runtime.state
    options = runtime.session.options
    supervisor = runtime.supervisor

    if "manual_mode" in patch:
        manual = patch["manual_mode"]
        if not isinstance(manual, bool):
            raise ValueError("manual_mode must be a bool")
        if manual != state.manual_mode:
            await runtime.message("/manual" if manual else "/auto")

    if "direction" in patch:
        direction = patch["direction"]
        if direction not in {"maximize", "minimize"}:
            raise ValueError("direction must be 'maximize' or 'minimize'")
        options.direction = direction
        supervisor.configure_options(direction=direction)

    if "tolerance" in patch:
        tolerance = patch["tolerance"]
        if not isinstance(tolerance, (int, float)) or tolerance < 0:
            raise ValueError("tolerance must be a number >= 0")
        options.tolerance = float(tolerance)
        supervisor.configure_options(tolerance=options.tolerance)

    for name in POLICY_FLAGS:
        if name not in patch:
            continue
        value = patch[name]
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be a bool")  # noqa: TRY004
        setattr(options, name, value)
    if any(name in patch for name in POLICY_FLAGS):
        supervisor.configure_options(
            validation_mode=resolve_validation_mode(
                options.auto_validate,
                options.skip_validate,
            )
        )


async def _apply_compute_settings(runtime: Any, patch: dict[str, Any]) -> None:
    compute = runtime.session.compute
    if "data_root" in patch:
        raw = patch["data_root"]
        if raw in (None, ""):
            compute.data_root = None
            runtime.state.data_root = None
        else:
            path = Path(str(raw)).resolve()
            if not path.is_dir():
                raise ValueError(f"data_root does not exist: {path}")
            compute.data_root = path
            runtime.state.data_root = str(path)

    if "compute" not in patch:
        return
    raw = patch["compute"]
    if not isinstance(raw, dict):
        raise ValueError("compute must be an object")  # noqa: TRY004
    config = parse_compute_config(raw)
    compute.config = config
    if config.remote and compute.pool is None:
        compute.pool = GpuPool(
            list(config.hosts),
            placement=config.placement,
            store=runtime.store,
            dataset_root=compute.data_root,
        )
    elif not config.remote and compute.pool is not None:
        await compute.pool.aclose()
        compute.pool = None
        compute.leases.clear()


def _apply_model_connection(patch: dict[str, Any]) -> None:
    if "model_connection" not in patch:
        return
    raw = patch["model_connection"]
    if not isinstance(raw, dict):
        raise ValueError("model_connection must be an object")  # noqa: TRY004

    updates: dict[str, str] = {}
    for field, env_key in MODEL_CONNECTION_ENV_VARS.items():
        value = raw.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a string")  # noqa: TRY004
        value = value.strip()
        if field == "provider" and value and value not in ALLOWED_MODEL_PROVIDERS:
            raise ValueError(
                f"provider must be one of {', '.join(ALLOWED_MODEL_PROVIDERS)}"
            )
        if value:
            updates[env_key] = value
    if updates:
        _upsert_dotenv(Path(".env"), updates)
        os.environ.update(updates)


async def apply(runtime: Any, patch: dict[str, Any]) -> dict[str, Any]:
    """Validate and apply supported fields, then return their current projection."""
    unknown = set(patch) - SETTINGS_WHITELIST
    if unknown:
        raise ValueError(f"unsupported settings fields: {sorted(unknown)}")

    _apply_research_settings(runtime, patch)
    await _apply_policy_settings(runtime, patch)
    await _apply_compute_settings(runtime, patch)
    _apply_model_connection(patch)
    if DURABLE_SETTINGS.intersection(patch):
        runtime.state.save(runtime.state_path)
    return snapshot(runtime)
