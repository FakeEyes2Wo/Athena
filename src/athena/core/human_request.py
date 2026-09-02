"""Canonical typed human request/reply contract.

This module is the single Python source of truth for the JSON shapes carried
between Athena, the GUI gateway, native Tauri, and the TypeScript web client.
The Python runtime uses these Pydantic models as the boundary contract; other
languages consume the same ``test/fixtures/clarification`` JSON fixtures.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

ScopeKind = Literal["clarification", "runtime"]

IdStr = Annotated[str, StringConstraints(min_length=1, max_length=128)]
PromptStr = Annotated[str, StringConstraints(min_length=1, max_length=4000)]
LabelStr = Annotated[str, StringConstraints(min_length=1, max_length=120)]
ValueStr = Annotated[str, StringConstraints(min_length=1, max_length=256)]
CustomText = Annotated[str, StringConstraints(min_length=1, max_length=4000)]


class HumanContractError(ValueError):
    """Typed validation/settlement error for the human request contract.

    The ``code`` attribute is stable and may be surfaced through JSON-RPC or
    native Tauri error envelopes without string parsing.
    """

    def __init__(self, code: str, message: str, **details: object) -> None:
        self.code = code
        self.details = details
        super().__init__(f"{code}: {message}")


class HumanChoice(BaseModel):
    """One selectable option in a :class:`HumanRequest`."""

    model_config = ConfigDict(extra="forbid", strict=True)

    label: LabelStr
    value: ValueStr


def validate_choices(choices: list[HumanChoice]) -> None:
    """Require 2 or 3 unique choices; shared by request and controller models."""
    if len(choices) not in (2, 3):
        raise ValueError("choices must contain exactly 2 or 3 entries")
    values = [choice.value for choice in choices]
    if len(values) != len(set(values)):
        raise ValueError("choice values must be unique")


class HumanRequest(BaseModel):
    """A user-facing question with explicit session/scope and response modes."""

    model_config = ConfigDict(extra="forbid")

    request_id: IdStr
    session_id: IdStr
    scope_id: IdStr
    scope_kind: ScopeKind
    prompt: PromptStr
    choices: list[HumanChoice] = Field(default_factory=list)
    allow_custom: bool
    allow_skip: bool
    created_at: datetime
    expires_at: datetime

    @field_validator("allow_custom", "allow_skip", mode="before")
    @classmethod
    def _bool_flags(cls, value: object) -> object:
        if not isinstance(value, bool):
            raise ValueError("allow_custom and allow_skip must be booleans")
        return value

    @model_validator(mode="after")
    def _validate_contract(self) -> "HumanRequest":
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")

        if self.choices:
            validate_choices(self.choices)
        elif not self.allow_custom and not self.allow_skip:
            raise ValueError(
                "at least one response mode is required when choices are empty"
            )
        return self

    def model_dump_for_transport(self) -> dict[str, object]:
        """Serialize with ISO-8601 datetimes for JSON/WebSocket/native Tauri."""
        return self.model_dump(mode="json")


class ChoiceReply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["choice"]
    value: ValueStr


class TextReply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["text"]
    text: CustomText


class SkipReply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["skip"]


HumanReply = Annotated[ChoiceReply | TextReply | SkipReply, Field(discriminator="kind")]
HumanOutcomeKind = Literal["choice", "text", "skip", "timeout", "cancelled"]

_REPLY_MODELS: dict[str, type[ChoiceReply | TextReply | SkipReply]] = {
    "choice": ChoiceReply,
    "text": TextReply,
    "skip": SkipReply,
}
_OUTCOME_TOOL_TEXT = {
    "skip": "Skipped by user",
    "timeout": "Timed out",
    "cancelled": "Cancelled",
}


class HumanOutcome(BaseModel):
    """Server-resolved outcome. ``timeout`` and ``cancelled`` are never client inputs."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    kind: HumanOutcomeKind
    value: str | None = None
    choice_label: str | None = None
    settled_at: datetime

    def to_tool_text(self) -> str:
        """Render the outcome at the agent-tool presentation boundary."""
        if self.kind in ("choice", "text"):
            return self.value or ""
        return _OUTCOME_TOOL_TEXT[self.kind]


def parse_human_reply(
    payload: object | None,
    legacy_answer: str | None = None,
) -> HumanReply:
    """Parse exactly one client reply variant.

    ``payload`` accepts a dict/JSON-ready value or an already-constructed
    ``HumanReply``. ``legacy_answer`` is the compatibility path used by the
    GUI handler for the old free-text ``answer`` parameter; it is never
    combined with a structured payload.
    """
    if payload is not None and legacy_answer is not None:
        raise HumanContractError(
            "ambiguous_reply",
            "structured payload and legacy answer cannot be combined",
        )
    if legacy_answer is not None:
        if not isinstance(legacy_answer, str):
            raise HumanContractError("invalid_reply", "legacy answer must be a string")
        try:
            return TextReply(kind="text", text=legacy_answer)
        except Exception as error:
            raise HumanContractError(
                "invalid_reply", "legacy answer is not valid text reply"
            ) from error
    if payload is None:
        raise HumanContractError("missing_reply", "a reply payload is required")

    if not isinstance(payload, (BaseModel, dict)):
        raise HumanContractError("invalid_reply", "reply payload must be an object")
    kind = (
        getattr(payload, "kind", None)
        if isinstance(payload, BaseModel)
        else payload.get("kind")
    )
    return _parse_reply_payload(payload, kind)


def _parse_reply_payload(payload: object, kind: object) -> HumanReply:
    model = _REPLY_MODELS.get(kind)  # type: ignore[arg-type]
    if model is None:
        raise HumanContractError(
            "invalid_reply",
            f"unsupported reply kind: {kind!r}; clients may submit only choice/text/skip",
        )
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        raise HumanContractError(
            "invalid_reply", f"invalid {kind!r} reply payload"
        ) from error


__all__ = [
    "ChoiceReply",
    "CustomText",
    "HumanChoice",
    "HumanContractError",
    "HumanOutcome",
    "HumanOutcomeKind",
    "HumanReply",
    "HumanRequest",
    "IdStr",
    "LabelStr",
    "PromptStr",
    "ScopeKind",
    "SkipReply",
    "TextReply",
    "ValueStr",
    "parse_human_reply",
    "validate_choices",
]
