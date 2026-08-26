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
        rt = self._runtime
        return {
            "project_root": str(rt._root),
            "model": rt._model,
            "concurrency": rt.state.concurrency,
            "search_limit": rt.state.search_limit,
            "ideation": rt._ideation,
            "ideator_count": rt.state.ideator_count,
            "hypotheses_per_ideator": rt.state.hypotheses_per_ideator,
            "handoff_sources": rt.state.handoff_sources,
            "direction": rt._direction,
            "tolerance": rt._tolerance,
            "auto_validate": rt._auto_validate,
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
        session = getattr(self._runtime, "_session", None)
        compute = session.compute if session is not None else None
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

    async def apply(self, patch: dict[str, Any]) -> dict[str, Any]:
        rt = self._runtime
        allowed = SETTINGS_WHITELIST
        unknown = set(patch) - allowed
        if unknown:
            raise ValueError(f"unsupported settings fields: {sorted(unknown)}")
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
            if ideation != rt._ideation:
                rt._ideation = ideation
                rt._registry.unregister("ideator")
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
            rt._direction = direction
        if "tolerance" in patch:
            tolerance = patch["tolerance"]
            if not isinstance(tolerance, (int, float)) or tolerance < 0:
                raise ValueError("tolerance must be a number >= 0")
            rt._tolerance = float(tolerance)
        if "auto_validate" in patch:
            auto_validate = patch["auto_validate"]
            if not isinstance(auto_validate, bool):
                raise ValueError("auto_validate must be a bool")
            rt._auto_validate = auto_validate
        if "experiment_timeout_s" in patch:
            value = patch["experiment_timeout_s"]
            if not isinstance(value, int) or value < 1:
                raise ValueError("experiment_timeout_s must be an integer >= 1")
            rt.state.experiment_timeout_s = value
        if "data_root" in patch:
            raw = patch["data_root"]
            if raw in (None, ""):
                rt._session.data_root = None
                rt.state.data_root = None
            else:
                path = Path(str(raw)).resolve()
                if not path.is_dir():
                    raise ValueError(f"data_root does not exist: {path}")
                rt._session.data_root = path
                rt.state.data_root = str(path)
        if "compute" in patch:
            raw = patch["compute"]
            if not isinstance(raw, dict):
                raise ValueError("compute must be an object")
            new_compute = parse_compute_config(raw)
            rt._session.compute = new_compute
            if new_compute.remote:
                if rt._session.pool is None:
                    rt._session.pool = GpuPool(
                        list(new_compute.hosts),
                        placement=new_compute.placement,
                        store=rt._store,
                        dataset_root=rt._session.data_root,
                    )
            elif rt._session.pool is not None:
                await rt._session.pool.aclose()
                rt._session.pool = None
                rt._session.leases.clear()
        if "model_connection" in patch:
            raw = patch["model_connection"]
            if not isinstance(raw, dict):
                raise ValueError("model_connection must be an object")
            updates: dict[str, str] = {}
            for field, env_key in MODEL_CONNECTION_ENV_VARS.items():
                value = raw.get(field)
                if value is None:
                    continue
                if not isinstance(value, str):
                    raise ValueError(f"{field} must be a string")
                value = value.strip()
                if field == "provider":
                    if value and value not in ALLOWED_MODEL_PROVIDERS:
                        raise ValueError(
                            f"provider must be one of {', '.join(ALLOWED_MODEL_PROVIDERS)}"
                        )
                if value:
                    updates[env_key] = value
            if updates:
                _upsert_dotenv(Path(".env"), updates)
                os.environ.update(updates)
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
            rt.state.save(rt._state_path)
        return self.snapshot()
