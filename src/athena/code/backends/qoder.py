"""Optional Qoder code-generation adapter."""

import importlib
import os
from collections.abc import Mapping
from pathlib import Path

from athena.code.backends import BackendUnavailableError
from athena.code.backends.base import (
    CodeBackend,
    generation_result,
    snapshot_files,
)
from athena.code.types import ExecutionOutput, GenerationResult


class QoderBackend(CodeBackend):
    def __init__(
        self,
        *,
        sdk: object,
        environ: Mapping[str, str] | None = None,
        cli_path: str | None = None,
        model: str | None = None,
    ) -> None:
        self._sdk = sdk
        self._environ = environ if environ is not None else os.environ
        self._cli_path = cli_path
        self._model = model

    @classmethod
    def from_config(cls, config: dict[str, object]) -> "QoderBackend":
        try:
            sdk = importlib.import_module("qoder_agent_sdk")
        except ModuleNotFoundError as error:
            raise BackendUnavailableError("qoder backend unavailable") from error
        return cls(
            sdk=sdk,
            environ=config.get("environ"),  # type: ignore[arg-type]
            cli_path=(str(config["cli_path"]) if config.get("cli_path") else None),
            model=(str(config["model"]) if config.get("model") else None),
        )

    async def generate(
        self,
        prompt: str,
        target_dir: str,
        previous_outputs: list[ExecutionOutput],
        history: list[dict],
    ) -> GenerationResult:
        del previous_outputs, history
        before = snapshot_files(target_dir)
        sdk = self._sdk
        if self._environ.get("QODER_PERSONAL_ACCESS_TOKEN"):
            auth = sdk.access_token_from_env()
        else:
            auth = sdk.qodercli_auth()
        options_kwargs = {
            "auth": auth,
            "cwd": Path(target_dir),
            "allowed_tools": ["Read", "Edit"],
            "disallowed_tools": ["Bash"],
            "permission_mode": "acceptEdits",
        }
        if self._cli_path is not None:
            options_kwargs["cli_path"] = self._cli_path
        if self._model is not None:
            options_kwargs["model"] = self._model
        options = sdk.QoderAgentOptions(**options_kwargs)
        output: list[str] = []
        try:
            async for message in sdk.query(prompt=prompt, options=options):
                if not isinstance(message, sdk.AssistantMessage):
                    continue
                output.extend(
                    block.text
                    for block in message.content
                    if isinstance(block, sdk.TextBlock)
                )
        except Exception as exc:
            raise BackendUnavailableError(
                f"qoder backend failed: {type(exc).__name__}: {exc}"
            ) from exc
        return generation_result(
            before,
            snapshot_files(target_dir),
            output="\n".join(output).strip(),
        )
