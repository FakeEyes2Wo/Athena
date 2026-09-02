"""Canonical human request/reply contract tests."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from athena.core.human_request import (
    ChoiceReply,
    HumanChoice,
    HumanContractError,
    HumanRequest,
    SkipReply,
    TextReply,
    parse_human_reply,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "clarification"


def _base(**overrides) -> dict:
    payload = {
        "request_id": "req-1",
        "session_id": "s-1",
        "scope_id": "scope-1",
        "scope_kind": "clarification",
        "prompt": "Choose metric",
        "allow_custom": True,
        "allow_skip": True,
        "created_at": "2026-09-01T10:00:00Z",
        "expires_at": "2026-09-01T10:02:00Z",
    }
    payload.update(overrides)
    return payload


def test_request_rejects_four_choices_and_duplicate_values():
    base = _base()
    with pytest.raises(ValidationError):
        HumanRequest.model_validate(
            {
                **base,
                "choices": [
                    {"label": "A", "value": "a"},
                    {"label": "B", "value": "b"},
                    {"label": "C", "value": "c"},
                    {"label": "D", "value": "d"},
                ],
            }
        )
    with pytest.raises(ValidationError):
        HumanRequest.model_validate(
            {
                **base,
                "choices": [
                    {"label": "A", "value": "same"},
                    {"label": "B", "value": "same"},
                ],
            }
        )


def test_request_allows_two_choices_and_three_choices():
    two = HumanRequest.model_validate(
        _base(
            choices=[
                HumanChoice(label="F1", value="f1"),
                HumanChoice(label="AUC", value="auc"),
            ]
        )
    )
    three = HumanRequest.model_validate(
        _base(
            choices=[
                HumanChoice(label="A", value="a"),
                HumanChoice(label="B", value="b"),
                HumanChoice(label="C", value="c"),
            ]
        )
    )
    assert len(two.choices) == 2
    assert len(three.choices) == 3


def test_request_rejects_empty_choices_without_response_mode():
    with pytest.raises(ValidationError):
        HumanRequest.model_validate(
            _base(choices=[], allow_custom=False, allow_skip=False)
        )


def test_request_allows_empty_choices_with_custom_or_skip():
    custom = HumanRequest.model_validate(
        _base(choices=[], allow_custom=True, allow_skip=False)
    )
    skip = HumanRequest.model_validate(
        _base(choices=[], allow_custom=False, allow_skip=True)
    )
    assert custom.allow_custom is True
    assert skip.allow_skip is True


def test_request_rejects_invalid_expiration_order():
    with pytest.raises(ValidationError):
        HumanRequest.model_validate(
            _base(
                created_at="2026-09-01T10:02:00Z",
                expires_at="2026-09-01T10:01:00Z",
            )
        )


def test_legacy_answer_becomes_text_reply():
    assert parse_human_reply(None, legacy_answer="macro F1") == TextReply(
        kind="text", text="macro F1"
    )


def test_parse_human_reply_accepts_all_client_variants():
    assert parse_human_reply({"kind": "choice", "value": "f1"}) == ChoiceReply(
        kind="choice", value="f1"
    )
    assert parse_human_reply({"kind": "text", "text": "raw"}) == TextReply(
        kind="text", text="raw"
    )
    assert parse_human_reply({"kind": "skip"}) == SkipReply(kind="skip")


def test_parse_human_reply_rejects_payload_plus_legacy_and_server_outcomes():
    with pytest.raises(HumanContractError, match="ambiguous_reply"):
        parse_human_reply({"kind": "skip"}, legacy_answer="x")
    with pytest.raises(HumanContractError, match="invalid_reply"):
        parse_human_reply({"kind": "timeout"})
    with pytest.raises(HumanContractError, match="invalid_reply"):
        parse_human_reply({"kind": "cancelled"})


def test_fixture_valid_request_loads():
    payload = json.loads(
        (FIXTURES / "human_request_valid.json").read_text(encoding="utf-8")
    )
    request = HumanRequest.model_validate(payload)
    assert request.request_id == "req-1"


def test_fixture_reply_variants_load():
    replies = json.loads(
        (FIXTURES / "human_reply_valid.json").read_text(encoding="utf-8")
    )
    parsed = [parse_human_reply(item) for item in replies]
    assert [item.kind for item in parsed] == ["choice", "text", "skip"]


def test_fixture_invalid_cases_have_expected_error_codes():
    cases = json.loads(
        (FIXTURES / "human_request_invalid.json").read_text(encoding="utf-8")
    )
    with pytest.raises(ValidationError):
        HumanRequest.model_validate(cases[0]["payload"])
    with pytest.raises(ValidationError):
        HumanRequest.model_validate(cases[1]["payload"])
    with pytest.raises(ValidationError):
        HumanRequest.model_validate(cases[2]["payload"])
    assert {case["error_code"] for case in cases} == {
        "too_many_choices",
        "duplicate_choice_values",
        "no_response_mode",
    }
