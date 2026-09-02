"""Tests for the centralized confirmed task context provider."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.research.clarification.context import (
    ConfirmedTaskContextError,
    ConfirmedTaskContextProvider,
)
from athena.research.runtime_clarification import recover_confirmation
from athena.research.supervisor.state import ResearchState


def _state(tmp_path: Path, *, draft: bool = True):
    understanding = {
        "title": "Predict churn",
        "dataset": "churn.csv",
        "target": "churned",
        "task_type": "classification",
        "primary_metric": "roc_auc",
        "direction": "maximize",
        "evaluation_plan": "baseline eval",
    }
    attrs = {
        "task_text": "predict churn",
        "task_understanding": understanding,
        "handoff_refs": {},
        "task_clarification_draft_id": "draft-1" if draft else None,
        "task_clarification_revision": 4 if draft else None,
    }
    return SimpleNamespace(**attrs)


@pytest.mark.asyncio
async def test_provider_loads_confirmed_context_with_metadata(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    handoff = (
        "# TASK_CLARIFICATION\n\n"
        "Original task: predict churn\n\n"
        "Confirmed task contract for the churn experiment."
    )
    ref = await store.put_text(handoff)
    state = _state(tmp_path)
    state.handoff_refs["task_clarification"] = ref
    handoff_dir = tmp_path / "handoffs"
    handoff_dir.mkdir(parents=True)
    (handoff_dir / "TASK_CLARIFICATION.md").write_text(handoff, encoding="utf-8")

    provider = ConfirmedTaskContextProvider(
        state=state,
        store=store,
        state_root=tmp_path,
    )
    context = await provider.load()

    assert context.draft_id == "draft-1"
    assert context.revision == 4
    assert context.original_task == "predict churn"
    assert context.handoff_ref == ref
    block = context.render_prompt_block()
    assert "Confirmed task contract (authoritative)" in block
    assert "--- end of confirmed task contract ---" in block
    assert handoff in block


@pytest.mark.asyncio
async def test_from_runtime_uses_stable_config_paths(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    handoff = "# confirmed task"
    ref = await store.put_text(handoff)
    state = _state(tmp_path)
    state.handoff_refs["task_clarification"] = ref
    handoff_dir = tmp_path / "handoffs"
    handoff_dir.mkdir(parents=True)
    (handoff_dir / "TASK_CLARIFICATION.md").write_text(handoff, encoding="utf-8")
    runtime = SimpleNamespace(
        config=SimpleNamespace(paths=SimpleNamespace(athena=tmp_path)),
        state=state,
        store=store,
    )

    context = await ConfirmedTaskContextProvider.from_runtime(runtime).load()

    assert context.handoff_ref == ref
    assert context.original_task == "predict churn"


@pytest.mark.asyncio
async def test_provider_rejects_unconfirmed_state(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    state = _state(tmp_path)
    # No handoff_refs and no task_understanding.
    state.task_understanding = None
    provider = ConfirmedTaskContextProvider(
        state=state, store=store, state_root=tmp_path
    )

    with pytest.raises(ConfirmedTaskContextError, match="not confirmed"):
        await provider.load()


@pytest.mark.asyncio
async def test_provider_rejects_missing_named_handoff(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    handoff = "# handoff"
    ref = await store.put_text(handoff)
    state = _state(tmp_path)
    state.handoff_refs["task_clarification"] = ref
    provider = ConfirmedTaskContextProvider(
        state=state, store=store, state_root=tmp_path
    )

    with pytest.raises(ConfirmedTaskContextError, match="named task handoff"):
        await provider.load()


@pytest.mark.asyncio
async def test_provider_rejects_corrupt_named_handoff(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    handoff = "# handoff"
    ref = await store.put_text(handoff)
    state = _state(tmp_path)
    state.handoff_refs["task_clarification"] = ref
    handoff_dir = tmp_path / "handoffs"
    handoff_dir.mkdir(parents=True)
    (handoff_dir / "TASK_CLARIFICATION.md").write_text(
        "different handoff", encoding="utf-8"
    )
    provider = ConfirmedTaskContextProvider(
        state=state, store=store, state_root=tmp_path
    )

    with pytest.raises(ConfirmedTaskContextError, match="does not match"):
        await provider.load()


@pytest.mark.asyncio
async def test_legacy_checkpoint_without_draft_metadata_loads(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    handoff = "# legacy handoff"
    ref = await store.put_text(handoff)
    state = _state(tmp_path, draft=False)
    state.handoff_refs["task_clarification"] = ref
    handoff_dir = tmp_path / "handoffs"
    handoff_dir.mkdir(parents=True)
    (handoff_dir / "TASK_CLARIFICATION.md").write_text(handoff, encoding="utf-8")

    provider = ConfirmedTaskContextProvider(
        state=state, store=store, state_root=tmp_path
    )
    context = await provider.load()

    assert context.draft_id is None
    assert context.revision is None
    assert context.handoff_text == handoff


@pytest.mark.asyncio
async def test_recovery_materializes_named_handoff_for_legacy_checkpoint(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    handoff = "# legacy handoff\n\nConfirmed before named handoffs existed."
    ref = await store.put_text(handoff)
    state = ResearchState(
        status="IDLE",
        phase="PREPARE",
        search_limit=10,
        concurrency=1,
    )
    state.task_text = "predict churn"
    state.task_understanding = _state(tmp_path, draft=False).task_understanding
    state.handoff_refs = {"task_clarification": ref}
    state_path = tmp_path / "state.json"
    state.save(state_path)

    async def start() -> None:
        return None

    runtime = SimpleNamespace(
        config=SimpleNamespace(paths=SimpleNamespace(athena=tmp_path)),
        state=state,
        state_path=state_path,
        store=store,
        session_id="legacy-session",
        session=SimpleNamespace(
            lifecycle=SimpleNamespace(confirmation_lock=asyncio.Lock())
        ),
        start=start,
    )
    handoff_path = tmp_path / "handoffs" / "TASK_CLARIFICATION.md"
    assert not handoff_path.exists()

    await recover_confirmation(runtime)
    context = await ConfirmedTaskContextProvider.from_runtime(runtime).load()

    assert handoff_path.read_text(encoding="utf-8") == handoff
    assert context.handoff_text == handoff
