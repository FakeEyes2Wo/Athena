"""Autonomous Supervisor state persistence tests."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from athena.research.supervisor.plans import PlanState
from athena.research.supervisor.state import ResearchState, _core_digest

_TRUSTED_REF = "sha256:" + "b" * 64
_CONTEXT_REF = "sha256:" + "d" * 64


def _search_state() -> ResearchState:
    return ResearchState(
        status="RUNNING",
        phase="SEARCH",
        search_limit=10,
        concurrency=4,
        plans={
            "hyp_vit": PlanState(
                kind="SEARCH",
                context_ref=_CONTEXT_REF,
                turns_used=2,
                turn_limit=12,
                patience=4,
                stale_rounds=1,
                best_ref=_TRUSTED_REF,
            )
        },
    )


def test_search_plan_round_trips_with_exact_durable_fields(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = _search_state()

    saved_path = state.save(path)

    assert saved_path == path
    assert ResearchState.load(path) == state
    assert set(json.loads(path.read_text(encoding="utf-8"))["plans"]["hyp_vit"]) == {
        "kind",
        "context_ref",
        "turns_used",
        "turn_limit",
        "patience",
        "stale_rounds",
        "best_ref",
    }


@pytest.mark.parametrize(
    ("plan_id", "kind"), [("prepare", "PREPARE"), ("validate", "VALIDATE")]
)
def test_non_search_plans_omit_search_only_fields(
    tmp_path: Path, plan_id: str, kind: str
) -> None:
    path = tmp_path / "state.json"
    state = ResearchState(
        status="RUNNING",
        phase=kind,
        search_limit=10,
        concurrency=4,
        plans={
            plan_id: PlanState(
                kind=kind,
                context_ref=_CONTEXT_REF,
                turns_used=0,
                turn_limit=12,
            )
        },
    )

    state.save(path)

    assert set(json.loads(path.read_text(encoding="utf-8"))["plans"][plan_id]) == {
        "kind",
        "context_ref",
        "turns_used",
        "turn_limit",
    }


@pytest.mark.parametrize(
    ("plan_id", "kind"),
    [
        ("prepare", "SEARCH"),
        ("validate", "SEARCH"),
        ("hyp_vit", "PREPARE"),
        ("hyp_vit", "VALIDATE"),
    ],
)
def test_plan_map_keys_must_match_plan_kind(plan_id: str, kind: str) -> None:
    plan_payload = {
        "kind": kind,
        "context_ref": _CONTEXT_REF,
        "turns_used": 0,
        "turn_limit": 12,
    }
    if kind == "SEARCH":
        plan_payload["patience"] = 4

    with pytest.raises(ValidationError, match="plan key"):
        ResearchState(
            status="RUNNING",
            phase="SEARCH",
            search_limit=10,
            concurrency=4,
            plans={plan_id: PlanState.model_validate(plan_payload)},
        )


@pytest.mark.parametrize(("field", "value"), [("search_limit", -1), ("concurrency", 0)])
def test_research_state_rejects_invalid_limits(field: str, value: int) -> None:
    payload = {
        "status": "RUNNING",
        "phase": "SEARCH",
        "search_limit": 10,
        "concurrency": 4,
        "plans": {},
        field: value,
    }

    with pytest.raises(ValidationError):
        ResearchState.model_validate(payload)


@pytest.mark.parametrize("field", ["search_limit", "concurrency"])
def test_research_state_rejects_coerced_numeric_limits(field: str) -> None:
    payload = {
        "status": "RUNNING",
        "phase": "SEARCH",
        "search_limit": 10,
        "concurrency": 4,
        "plans": {},
        field: "4",
    }

    with pytest.raises(ValidationError):
        ResearchState.model_validate(payload)


@pytest.mark.parametrize("plan_id", ["", " ", "\t"])
def test_search_plan_key_must_be_nonblank(plan_id: str) -> None:
    with pytest.raises(ValidationError, match="Hypothesis ID"):
        ResearchState(
            status="RUNNING",
            phase="SEARCH",
            search_limit=10,
            concurrency=4,
            plans={
                plan_id: PlanState(
                    kind="SEARCH",
                    context_ref=_CONTEXT_REF,
                    turns_used=0,
                    turn_limit=12,
                    patience=4,
                )
            },
        )


def test_research_state_model_serialization_uses_plan_contract_shape() -> None:
    state = ResearchState(
        status="RUNNING",
        phase="PREPARE",
        search_limit=10,
        concurrency=4,
        plans={
            "prepare": PlanState(
                kind="PREPARE",
                context_ref=_CONTEXT_REF,
                turns_used=0,
                turn_limit=12,
            )
        },
    )

    assert set(state.model_dump()["plans"]["prepare"]) == {
        "kind",
        "context_ref",
        "turns_used",
        "turn_limit",
    }


def test_load_accepts_non_search_plan_with_search_fields_omitted(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "status": "RUNNING",
                "phase": "PREPARE",
                "search_limit": 10,
                "concurrency": 4,
                "plans": {
                    "prepare": {
                        "kind": "PREPARE",
                        "context_ref": _CONTEXT_REF,
                        "turns_used": 0,
                        "turn_limit": 12,
                    }
                },
                "validation": None,
            }
        ),
        encoding="utf-8",
    )

    loaded = ResearchState.load(path)

    assert loaded.plans["prepare"].kind == "PREPARE"


def test_research_state_rejects_unknown_status_and_phase() -> None:
    with pytest.raises(ValidationError):
        ResearchState(
            status="PAUSED",
            phase="IDLE",
            search_limit=10,
            concurrency=4,
        )


def test_save_atomically_replaces_existing_state(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"old": true}\n', encoding="utf-8")

    _search_state().save(path)

    assert ResearchState.load(path) == _search_state()
    assert not (tmp_path / "state.json.tmp").exists()


def test_save_removes_temporary_file_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    original = '{"old": true}\n'
    path.write_text(original, encoding="utf-8")

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("athena.core.persistence.os.replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        _search_state().save(path)

    assert path.read_text(encoding="utf-8") == original
    assert not (tmp_path / "state.json.tmp").exists()


def test_load_rejects_non_object_json(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="must be an object"):
        ResearchState.load(path)


def test_resume_checkpoint_fields_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = ResearchState(
        status="RUNNING",
        phase="PREPARE",
        search_limit=10,
        concurrency=1,
        task_text="predict titanic survival",
        kaggle_download=False,
        task_research_task="inspect competition",
        task_research_ref=_CONTEXT_REF,
        task_research_agent_id="general-worker",
        evaluator_ref=_TRUSTED_REF,
    )

    state.save(path)
    loaded = ResearchState.load(path)

    assert loaded == state
    assert loaded.task_text == "predict titanic survival"
    assert loaded.kaggle_download is False
    assert loaded.task_research_task == "inspect competition"
    assert loaded.task_research_ref == _CONTEXT_REF
    assert loaded.task_research_agent_id == "general-worker"
    assert loaded.evaluator_ref == _TRUSTED_REF
    # 旧 schema 兼容：resume 字段不写进 state.json，而是落在 resume.json。
    core = json.loads(path.read_text(encoding="utf-8"))
    assert not set(core).intersection(
        {
            "task_text",
            "kaggle_download",
            "task_research_task",
            "task_research_ref",
            "task_research_agent_id",
            "evaluator_ref",
        }
    )
    resume = json.loads((tmp_path / "resume.json").read_text(encoding="utf-8"))
    assert resume["task_text"] == "predict titanic survival"
    assert resume["state_digest"]


def test_legacy_state_without_resume_fields_defaults_to_none(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "status": "RUNNING",
                "phase": "PREPARE",
                "search_limit": 10,
                "concurrency": 1,
                "plans": {},
                "validation": None,
            }
        ),
        encoding="utf-8",
    )

    loaded = ResearchState.load(path)

    assert loaded.task_text is None
    assert loaded.kaggle_download is None
    assert loaded.task_research_task is None
    assert loaded.task_research_ref is None
    assert loaded.task_research_agent_id is None
    assert loaded.evaluator_ref is None


def test_stale_resume_file_is_ignored_when_core_state_changed(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = ResearchState(
        status="RUNNING",
        phase="PREPARE",
        search_limit=10,
        concurrency=1,
        task_text="predict titanic survival",
    )
    state.save(path)
    # 模拟旧代码重写核心 state（不写 resume.json）后新代码重新读取。
    path.write_text(
        json.dumps(
            {
                "status": "RUNNING",
                "phase": "PREPARE",
                "search_limit": 5,
                "concurrency": 1,
                "plans": {},
                "validation": None,
            }
        ),
        encoding="utf-8",
    )

    loaded = ResearchState.load(path)

    assert loaded.search_limit == 5
    assert loaded.task_text is None


def test_load_migrates_intermediate_inline_resume_state(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "status": "RUNNING",
                "phase": "PREPARE",
                "search_limit": 10,
                "concurrency": 1,
                "plans": {},
                "validation": None,
                "task_text": "predict titanic survival",
                "task_research_ref": _CONTEXT_REF,
                "task_research_agent_id": "general-worker",
            }
        ),
        encoding="utf-8",
    )

    loaded = ResearchState.load(path)

    assert loaded.task_text == "predict titanic survival"
    # 没有任务原文的调研引用无法判断归属，加载时按无缓存归一化。
    assert loaded.task_research_ref is None
    assert loaded.task_research_agent_id is None
    core = json.loads(path.read_text(encoding="utf-8"))
    assert "task_text" not in core
    assert "task_research_ref" not in core
    resume = json.loads((tmp_path / "resume.json").read_text(encoding="utf-8"))
    assert resume["task_text"] == "predict titanic survival"


def test_load_rewrites_inline_core_even_when_resume_file_matches(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    payload = {
        "status": "RUNNING",
        "phase": "PREPARE",
        "search_limit": 10,
        "concurrency": 1,
        "plans": {},
        "validation": None,
        "task_text": "inline task",
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "resume.json").write_text(
        json.dumps({"state_digest": _core_digest(payload), "task_text": "resume task"}),
        encoding="utf-8",
    )

    loaded = ResearchState.load(path)

    assert loaded.task_text == "resume task"
    core = json.loads(path.read_text(encoding="utf-8"))
    assert "task_text" not in core
    resume = json.loads((tmp_path / "resume.json").read_text(encoding="utf-8"))
    assert resume["task_text"] == "resume task"


def test_load_strips_unknown_top_level_keys_and_backs_up(
    tmp_path: Path, caplog
) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "status": "RUNNING",
                "phase": "PREPARE",
                "search_limit": 10,
                "concurrency": 1,
                "plans": {},
                "validation": None,
                "sota": {"baseline": "Wheat Loop", "value": 3352.5},
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level("WARNING"):
        loaded = ResearchState.load(path)

    assert loaded.status == "RUNNING"
    assert "sota" not in loaded.model_dump()
    assert any("sota" in message for message in caplog.messages)
    backup = (tmp_path / "state.json.corrupt").read_text(encoding="utf-8")
    assert "Wheat Loop" in backup


def test_load_unknown_key_does_not_mask_invalid_known_fields(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "status": "BROKEN",
                "phase": "PREPARE",
                "search_limit": 10,
                "concurrency": 1,
                "plans": {},
                "validation": None,
                "sota": {"value": 3352.5},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        ResearchState.load(path)

    assert (tmp_path / "state.json.corrupt").is_file()
