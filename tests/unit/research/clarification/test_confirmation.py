"""Journaled confirmation boundary tests."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.research.clarification.confirmation import (
    commit_confirmation,
    dependencies_from_runtime,
)
from athena.research.clarification.errors import ClarificationError
from athena.research.clarification.models import ClarificationDraft
from athena.research.clarification.persistence import ClarificationStore
from athena.research.runtime.clarification import confirm_and_start

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "clarification"


class FakeState:
    def __init__(self, tmp_path: Path, *, save_error: bool = False) -> None:
        self.task_understanding = None
        self.task_text = None
        self.handoff_refs: dict[str, str] = {}
        self.save_error = save_error
        self.saves: list[Path] = []
        self.path = tmp_path / "state.json"

    def save(self, path) -> None:
        self.saves.append(Path(path))
        if self.save_error:
            raise OSError("disk full")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "task_understanding": self.task_understanding,
                    "task_text": self.task_text,
                    "handoff_refs": self.handoff_refs,
                }
            ),
            encoding="utf-8",
        )


class FakeStore:
    def __init__(self) -> None:
        self.texts: dict[str, str] = {}

    async def put_text(self, text: str) -> str:
        ref = f"ref-{len(self.texts)}"
        self.texts[ref] = text
        return ref

    async def get_text(self, ref: str) -> str:
        return self.texts[ref]


class FakeRuntime:
    def __init__(self, tmp_path: Path, *, state_save_error: bool = False) -> None:
        self.session_id = "s-1"
        self.config = SimpleNamespace(paths=SimpleNamespace(athena=tmp_path))
        self.state_path = tmp_path / "state.json"
        self.state = FakeState(tmp_path, save_error=state_save_error)
        self.store = FakeStore()
        self.session = SimpleNamespace(
            lifecycle=SimpleNamespace(confirmation_lock=asyncio.Lock())
        )
        self.started_calls = 0
        self.fail_start = False

    async def start(self) -> None:
        self.started_calls += 1
        if self.fail_start:
            self.fail_start = False
            raise RuntimeError("start failed")


def _ready_draft(*, unresolved: bool = True) -> ClarificationDraft:
    text = (FIXTURES / "draft_ready.json").read_text(encoding="utf-8")
    payload = json.loads(text)
    if not unresolved:
        payload["unresolved"] = []
    return ClarificationDraft.model_validate(payload)


def _runtime_with_draft(tmp_path: Path, *, unresolved: bool = True):
    store = ClarificationStore(tmp_path)
    draft = _ready_draft(unresolved=unresolved)
    store.save(draft)
    runtime = FakeRuntime(tmp_path)
    return store, draft, runtime


@pytest.mark.asyncio
async def test_confirmation_commits_state_handoff_and_starts_once(
    tmp_path: Path,
) -> None:
    store, draft, runtime = _runtime_with_draft(tmp_path, unresolved=False)

    confirmed = await confirm_and_start(runtime, draft.draft_id, draft.revision, False)

    assert confirmed.status == "CONFIRMED"
    assert runtime.state.task_understanding is not None
    assert runtime.state.handoff_refs["task_clarification"].startswith("ref-")
    assert store.load().status == "CONFIRMED"
    assert store.handoff_path.is_file()
    assert runtime.started_calls == 1


@pytest.mark.asyncio
async def test_state_save_failure_rolls_back_and_never_starts_prepare(
    tmp_path: Path,
) -> None:
    store, draft, runtime = _runtime_with_draft(tmp_path, unresolved=False)
    runtime.state.save_error = True

    with pytest.raises(ClarificationError, match="state_save_failed"):
        await confirm_and_start(runtime, draft.draft_id, draft.revision, False)

    assert store.load().status == "READY_FOR_CONFIRMATION"
    assert runtime.started_calls == 0


@pytest.mark.asyncio
async def test_stale_revision_and_unresolved_ack_are_typed(tmp_path: Path) -> None:
    _store, draft, runtime = _runtime_with_draft(tmp_path)

    with pytest.raises(ClarificationError, match="stale_revision"):
        await confirm_and_start(runtime, draft.draft_id, draft.revision - 1, True)
    with pytest.raises(ClarificationError, match="unresolved_ack_required"):
        await confirm_and_start(runtime, draft.draft_id, draft.revision, False)


@pytest.mark.asyncio
async def test_same_committed_revision_is_idempotent(tmp_path: Path) -> None:
    _store, draft, runtime = _runtime_with_draft(tmp_path, unresolved=False)
    deps = dependencies_from_runtime(runtime)

    first = await commit_confirmation(deps, draft.draft_id, draft.revision, False)
    second = await commit_confirmation(deps, draft.draft_id, draft.revision, False)

    assert second == first
    assert runtime.started_calls == 0


@pytest.mark.asyncio
async def test_confirmed_start_failure_is_retryable(tmp_path: Path) -> None:
    _store, draft, runtime = _runtime_with_draft(tmp_path, unresolved=False)
    runtime.fail_start = True

    with pytest.raises(ClarificationError, match="confirmed start failed"):
        await confirm_and_start(runtime, draft.draft_id, draft.revision, False)

    # A second confirm sees the durable CONFIRMED revision and retries start.
    confirmed = await confirm_and_start(runtime, draft.draft_id, draft.revision, False)
    assert confirmed.status == "CONFIRMED"
    assert runtime.started_calls == 2
