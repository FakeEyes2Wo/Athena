"""Focused tests for surfacing evaluator HANDOFF.md into SEARCH ideator context."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.research.runtime import ResearchRuntime, _read_eval_handoff

_HANDOFF = "# Eval contract\n\npredictions/predictions.csv: header,id,target\n"


async def _freeze_bundle(store, *, include_handoff: bool) -> str:
    tree: dict[str, str] = {}
    if include_handoff:
        tree["HANDOFF.md"] = await store.put_bytes(_HANDOFF.encode("utf-8"))
    tree["evaluate.py"] = await store.put_bytes(b"print('{\"primary\": 1.0}')\n")
    tree_ref = await store.put_text(json.dumps(tree, ensure_ascii=False))
    bundle = {
        "bundle_id": "bundle:test",
        "entrypoint": "evaluate.py",
        "runtime": "python-uv",
        "tree_ref": tree_ref,
    }
    return await store.put_text(json.dumps(bundle, ensure_ascii=False))


@pytest.mark.asyncio
async def test_read_eval_handoff_returns_markdown(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    evaluator_ref = await _freeze_bundle(store, include_handoff=True)
    assert await _read_eval_handoff(store, evaluator_ref) == _HANDOFF


@pytest.mark.asyncio
async def test_read_eval_handoff_empty_when_absent(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    evaluator_ref = await _freeze_bundle(store, include_handoff=False)
    assert await _read_eval_handoff(store, evaluator_ref) == ""


@pytest.mark.asyncio
async def test_read_eval_handoff_empty_when_no_ref(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    assert await _read_eval_handoff(store, None) == ""


@pytest.mark.asyncio
async def test_ideator_lane_surfaces_eval_handoff_in_context(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    evaluator_ref = await _freeze_bundle(store, include_handoff=True)

    runtime = ResearchRuntime.__new__(ResearchRuntime)
    runtime._store = store
    runtime._supervisor = SimpleNamespace(evaluator_ref=evaluator_ref)

    captured: dict[str, object] = {}

    async def create_root(agent_type, request, *, name):
        captured["request"] = request
        raise RuntimeError("stop after capture")

    runtime._agents = SimpleNamespace(create_root=create_root)

    with pytest.raises(RuntimeError, match="stop after capture"):
        await runtime._run_ideator_lane("ideator-1", 1, Path(tmp_path))

    request = captured["request"]
    assert request["context_refs"], "eval_handoff context ref should be attached"
    payload = json.loads(await store.get_text(request["context_refs"][0]))
    assert payload == {"eval_handoff": _HANDOFF}
