"""Strict experiment manifest schema and workspace reader."""

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

_FORBIDDEN_EXECUTABLES = frozenset({"git", "git.exe"})
_MANIFEST_FIELDS = frozenset({"version", "commands", "outputs"})
_MAX_FIELD_NAME_CHARS = 40


def _validate_relative_path(path: str, label: str) -> None:
    """拒绝绝对路径、驱动器相对路径、空段与 ``..`` 逃逸。"""
    if os.path.isabs(path):
        raise ValueError(f"{label} path must be relative to the workspace")
    if os.path.splitdrive(path)[0]:
        raise ValueError(f"{label} path must be relative to the workspace")
    parts = path.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"{label} path escapes the workspace")


def _safe_field_name(part: object) -> str:
    """Bound one unknown manifest field name before returning it as feedback."""
    printable = "".join(char for char in str(part) if char.isprintable())
    return printable[:_MAX_FIELD_NAME_CHARS] or "<field>"


def _manifest_validation_summary(error: ValidationError) -> str:
    """Return actionable validation details without manifest input values."""
    summaries: list[str] = []
    for detail in error.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    ):
        extra_key = detail["type"] == "extra_forbidden"
        location = ".".join(
            (
                str(part)
                if isinstance(part, int)
                else (
                    part
                    if part in _MANIFEST_FIELDS
                    else _safe_field_name(part) if extra_key else "<field>"
                )
            )
            for part in detail["loc"]
        )
        message = " ".join(detail["msg"].split())
        summaries.append(f"{location}: {message}" if location else message)
    return "; ".join(summaries)[:1000]


class ExperimentManifest(BaseModel):
    """Workspace-root argv commands and declared prediction/report outputs."""

    model_config = ConfigDict(extra="forbid", strict=True)

    version: int
    commands: list[list[str]]
    outputs: dict[str, str]

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("only manifest version 1 is supported")
        return value

    @field_validator("commands", mode="before")
    @classmethod
    def _coerce_single_command(cls, value: object) -> object:
        if isinstance(value, list) and value and isinstance(value[0], str):
            return [value]
        return value

    @field_validator("commands")
    @classmethod
    def _validate_commands(cls, value: list[list[str]]) -> list[list[str]]:
        for argv in value:
            if not argv or any(not part.strip() for part in argv):
                raise ValueError("each command must be a non-empty argv array")
            executable = os.path.basename(argv[0].replace("\\", "/")).lower()
            if executable in _FORBIDDEN_EXECUTABLES:
                raise ValueError("manifest cannot run git commands")
        return value

    @field_validator("outputs")
    @classmethod
    def _validate_outputs(cls, value: dict[str, str]) -> dict[str, str]:
        if "predictions" not in value:
            raise ValueError("predictions output is mandatory")
        for key, rel in value.items():
            _validate_relative_path(rel, f"output {key}")
        return value


def read_experiment_manifest(root: Path) -> ExperimentManifest:
    """Parse and validate the workspace-root experiment.json."""
    path = root / "experiment.json"
    if not path.is_file():
        raise ValueError("experiment.json is missing")
    try:
        return ExperimentManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        raise ValueError(_manifest_validation_summary(exc)) from exc
