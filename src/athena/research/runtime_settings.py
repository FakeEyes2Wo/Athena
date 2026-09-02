"""GUI-facing settings snapshot and whitelisted patch application.

Kept separate from ``ResearchRuntime`` so the composition root does not grow
with every settings field the GUI exposes.
"""

import os
from pathlib import Path
from typing import Any

from athena.execution.compute_config import parse_compute_config
from athena.execution.pool import GpuPool

MODEL_CONNECTION_ENV_VARS: dict[str, str] = {
    "provider": "LLM_PROVIDER",
    "base_url": "BASE_URL",
    "model_name": "MODEL_NAME",
    "llm_api_key": "LLM_API_KEY",
}
"""模型连接字段 → 环境变量名；provider 只允许 deepseek/openai/qwen。"""

ALLOWED_MODEL_PROVIDERS = ("deepseek", "openai", "qwen")

SETTINGS_WHITELIST: frozenset[str] = frozenset(
    {
        "concurrency",
        "search_limit",
        "direction",
        "tolerance",
        "auto_validate",
        "manual_mode",
        "ideation",
        "ideator_count",
        "hypotheses_per_ideator",
        "handoff_sources",
        "model_connection",
        "data_root",
        "experiment_timeout_s",
        "compute",
    }
)


def _mask_secret(value: str | None) -> str:
    """把密钥匿名化成 ``sk-********f8e3`` 形态，绝不回传明文。"""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}{'*' * 8}{value[-4:]}"


def _upsert_dotenv(path: Path, updates: dict[str, str]) -> None:
    """把 ``KEY=value`` 写入 ``.env``，保留注释与既有行；重复键覆盖。"""
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    updated: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                updated.add(key)
                continue
        out.append(line)
    for key, value in updates.items():
        if key not in updated:
            out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


class SettingsController:
    """Read/write runtime settings for the GUI."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def snapshot(self) -> dict[str, Any]:
        """Return the current GUI-facing settings projection."""
        rt = self._runtime
        return {
            "project_root": str(rt.root),
            "model": rt.model,
            "concurrency": rt.state.concurrency,
            "search_limit": rt.state.search_limit,
            "ideation": rt.ideation,
            "ideator_count": rt.state.ideator_count,
            "hypotheses_per_ideator": rt.state.hypotheses_per_ideator,
            "handoff_sources": rt.state.handoff_sources,
            "direction": rt.direction,
            "tolerance": rt.session.options.tolerance,
            "auto_validate": rt.session.options.auto_validate,
            "manual_mode": rt.state.manual_mode,
            "phase": rt.state.phase,
            "status": rt.state.status,
            "data_root": rt.state.data_root,
            "experiment_timeout_s": rt.state.experiment_timeout_s,
            "compute": self._compute_settings(),
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

    def _compute_settings(self) -> dict[str, Any]:
        compute = self._runtime.session.compute.config
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

    def _apply_research_settings(self, patch: dict[str, Any]) -> None:
        """Apply search shape, ideation, handoff, and timeout settings."""
        rt = self._runtime
        if "concurrency" in patch:
            concurrency = patch["concurrency"]
            if not isinstance(concurrency, int) or concurrency < 1:
                raise ValueError("concurrency must be an integer >= 1")
            rt.state.concurrency = concurrency
        if "search_limit" in patch:
            search_limit = patch["search_limit"]
            if not isinstance(search_limit, int) or search_limit < 0:
                raise ValueError("search_limit must be an integer >= 0")
            rt.state.search_limit = search_limit
        if "ideation" in patch:
            ideation = patch["ideation"]
            if ideation not in {"ideageneration", "baseline", "debate"}:
                raise ValueError(
                    "ideation must be 'ideageneration', 'baseline', or 'debate'"
                )
            if ideation != rt.ideation:
                rt.session.options.ideation = ideation
                rt.registry.unregister("ideator")
        if "ideator_count" in patch:
            value = patch["ideator_count"]
            if not isinstance(value, int) or value < 1 or value > 8:
                raise ValueError("ideator_count must be an integer between 1 and 8")
            rt.state.ideator_count = value
        if "hypotheses_per_ideator" in patch:
            value = patch["hypotheses_per_ideator"]
            if not isinstance(value, int) or value < 1 or value > 5:
                raise ValueError(
                    "hypotheses_per_ideator must be an integer between 1 and 5"
                )
            rt.state.hypotheses_per_ideator = value
        if "handoff_sources" in patch:
            value = patch["handoff_sources"]
            if not isinstance(value, list) or any(
                source not in {"kaggle", "literature"} for source in value
            ):
                raise ValueError(
                    "handoff_sources must be a list containing only "
                    "'kaggle' and 'literature'"
                )
            rt.state.handoff_sources = value
        if "experiment_timeout_s" in patch:
            value = patch["experiment_timeout_s"]
            if not isinstance(value, int) or value < 1:
                raise ValueError("experiment_timeout_s must be an integer >= 1")
            rt.state.experiment_timeout_s = value

    async def _apply_policy_settings(self, patch: dict[str, Any]) -> None:
        """Apply manual control and Supervisor search policy settings."""
        rt = self._runtime
        if "manual_mode" in patch:
            manual = patch["manual_mode"]
            if not isinstance(manual, bool):
                raise ValueError("manual_mode must be a bool")
            if manual != rt.state.manual_mode:
                await rt.message("/manual" if manual else "/auto")
        if "direction" in patch:
            direction = patch["direction"]
            if direction not in {"maximize", "minimize"}:
                raise ValueError("direction must be 'maximize' or 'minimize'")
            rt.session.options.direction = direction
            rt.supervisor.configure_options(direction=direction)
        if "tolerance" in patch:
            tolerance = patch["tolerance"]
            if not isinstance(tolerance, (int, float)) or tolerance < 0:
                raise ValueError("tolerance must be a number >= 0")
            rt.session.options.tolerance = float(tolerance)
            rt.supervisor.configure_options(tolerance=float(tolerance))
        if "auto_validate" in patch:
            auto_validate = patch["auto_validate"]
            if not isinstance(auto_validate, bool):
                raise ValueError("auto_validate must be a bool")
            rt.session.options.auto_validate = auto_validate
            rt.supervisor.configure_options(auto_validate=auto_validate)

    async def _apply_compute_settings(self, patch: dict[str, Any]) -> None:
        """Apply data-root and local/remote compute settings."""
        rt = self._runtime
        if "data_root" in patch:
            raw = patch["data_root"]
            if raw in (None, ""):
                rt.session.compute.data_root = None
                rt.state.data_root = None
            else:
                path = Path(str(raw)).resolve()
                if not path.is_dir():
                    raise ValueError(f"data_root does not exist: {path}")
                rt.session.compute.data_root = path
                rt.state.data_root = str(path)
        if "compute" in patch:
            raw = patch["compute"]
            if not isinstance(raw, dict):
                raise ValueError("compute must be an object")
            new_compute = parse_compute_config(raw)
            rt.session.compute.config = new_compute
            if new_compute.remote:
                if rt.session.compute.pool is None:
                    rt.session.compute.pool = GpuPool(
                        list(new_compute.hosts),
                        placement=new_compute.placement,
                        store=rt.store,
                        dataset_root=rt.session.compute.data_root,
                    )
            elif rt.session.compute.pool is not None:
                await rt.session.compute.pool.aclose()
                rt.session.compute.pool = None
                rt.session.compute.leases.clear()

    def _apply_model_connection(self, patch: dict[str, Any]) -> None:
        """Validate and persist model connection environment settings."""
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

    async def apply(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Validate and apply the supported runtime settings fields."""
        unknown = set(patch) - SETTINGS_WHITELIST
        if unknown:
            raise ValueError(f"unsupported settings fields: {sorted(unknown)}")

        # Apply independent setting domains before one durable state write.
        self._apply_research_settings(patch)
        await self._apply_policy_settings(patch)
        await self._apply_compute_settings(patch)
        self._apply_model_connection(patch)
        if any(
            field in patch
            for field in (
                "concurrency",
                "search_limit",
                "ideator_count",
                "hypotheses_per_ideator",
                "handoff_sources",
                "experiment_timeout_s",
                "data_root",
            )
        ):
            self._runtime.state.save(self._runtime.state_path)
        return self.snapshot()
