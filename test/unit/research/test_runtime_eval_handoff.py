"""Focused tests for surfacing evaluator HANDOFF.md into SEARCH ideator context."""

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.research.turns.runner import AgentTurnRunner
from athena.research.contracts import EvaluatorDescriptor
from athena.research.runtime import ResearchRuntime
from athena.research.supervisor.evaluator_plan import read_eval_handoff
from athena.research.supervisor.prompt_context import handoff_block

_HANDOFF = "# Eval contract\n\npredictions/predictions.csv: header,id,target\n"


async def _freeze_bundle(store, *, include_handoff: bool) -> str:
    evaluator_dir = Path(tempfile.mkdtemp(prefix="athena-eval-"))
    if include_handoff:
        (evaluator_dir / "HANDOFF.md").write_text(_HANDOFF, encoding="utf-8")
    (evaluator_dir / "evaluate.py").write_text(
        "print('{\"primary\": 1.0}')\n", encoding="utf-8"
    )
    readme = "# Evaluator Freeze Marker\n\nDo not edit.\n"
    (evaluator_dir / "README.md").write_text(readme, encoding="utf-8")
    readme_ref = await store.put_text(readme)
    descriptor = EvaluatorDescriptor(
        dir_path=str(evaluator_dir),
        readme_ref=readme_ref,
        entrypoint="evaluate.py",
    )
    return await store.put_text(descriptor.model_dump_json())


@pytest.mark.asyncio
async def test_read_eval_handoff_returns_markdown(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    evaluator_ref = await _freeze_bundle(store, include_handoff=True)
    assert await read_eval_handoff(store, evaluator_ref) == _HANDOFF


@pytest.mark.asyncio
async def test_read_eval_handoff_empty_when_absent(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    evaluator_ref = await _freeze_bundle(store, include_handoff=False)
    assert await read_eval_handoff(store, evaluator_ref) == ""


@pytest.mark.asyncio
async def test_read_eval_handoff_empty_when_no_ref(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    assert await read_eval_handoff(store, None) == ""


@pytest.mark.asyncio
async def test_ideator_lane_surfaces_eval_handoff_in_context(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    evaluator_ref = await _freeze_bundle(store, include_handoff=True)

    runtime = ResearchRuntime(project_root=tmp_path)
    runtime.services.infrastructure.store = store
    runtime.services.workflow.supervisor = SimpleNamespace(
        evaluator_ref=evaluator_ref, state=SimpleNamespace(corpus_ref=None)
    )
    runner = AgentTurnRunner(runtime)

    captured: dict[str, object] = {}

    async def create_root(agent_type, request, *, name):
        captured["request"] = request
        raise RuntimeError("stop after capture")

    runtime.services.infrastructure.agents = SimpleNamespace(create_root=create_root)

    with pytest.raises(RuntimeError, match="stop after capture"):
        await runner._run_ideator_lane("ideator-1-1", 1, Path(tmp_path))

    request = captured["request"]
    # content 是唯一到得了 model 的信道；ref 仍然留着，供事后审计与重放。
    assert _HANDOFF.strip() in request["content"]
    payload = json.loads(await store.get_text(request["context_refs"][0]))
    assert payload == {"eval_handoff": _HANDOFF}


@pytest.mark.asyncio
async def test_the_prepare_baseline_is_handed_the_eval_contract(tmp_path) -> None:
    """真机（2026-08-16 第 10 次）：基线交出的预测评估器根本 join 不上，判 0.0。

    评估器写的 HANDOFF.md 已经把 ``__athena_row_id`` 与那 1200 行留出集讲得很清楚，
    prepare 的提示词也写着"契约已作为 context 附上"——但代码从来没附。基线因此只能猜，
    交出了 ``sample_id,probability,label_true`` 覆盖全部 6000 行。写 predictions 的
    Agent 必须拿到契约。
    """
    from athena.research.supervisor import prepare as prepare_module

    store = LocalArtifactStore(tmp_path / "artifacts")
    evaluator_ref = await _freeze_bundle(store, include_handoff=True)
    seen: dict[str, object] = {}

    class _Agents:
        async def create_root(self, agent_type, request, *, agent_id=None, name=None):
            seen["request"] = request
            raise RuntimeError("stop after the first dispatch")

    async def assert_baseline() -> None:
        pass

    with pytest.raises(RuntimeError, match="stop after the first dispatch"):
        await prepare_module.run_prepare_plan(
            agents=_Agents(),
            evaluator=SimpleNamespace(),
            git=SimpleNamespace(),
            workspace=SimpleNamespace(path=str(tmp_path / "ws")),
            execution=SimpleNamespace(ensure_environment=lambda: None),
            store=store,
            evaluator_ref=evaluator_ref,
            tree_ref=await store.put_text("{}"),
            task="build a baseline",
            max_turns=1,
            assert_baseline=assert_baseline,
        )

    # 必须在 content 里。base_runner 只把 trigger 的 content 当作 model 的 user
    # prompt（input_text = trigger.content），context_refs 从来没有被解析回正文——
    # 断言它出现在 refs 里等于什么都没验证。
    assert _HANDOFF.strip() in seen["request"]["content"]


@pytest.mark.asyncio
async def test_a_search_candidate_carries_the_eval_contract_in_its_plan_input(
    tmp_path,
) -> None:
    """候选同样在写 predictions/，契约随 PlanInput 一起冻结给它。"""
    from athena.research.supervisor.plans import PlanInput

    plan_input = PlanInput(
        evaluator_ref="sha256:" + "a" * 64,
        tree_ref="sha256:" + "b" * 64,
        eval_handoff=_HANDOFF,
    )

    restored = PlanInput.model_validate_json(plan_input.model_dump_json())

    assert restored.eval_handoff == _HANDOFF
    assert (
        PlanInput(
            evaluator_ref="sha256:" + "a" * 64, tree_ref="sha256:" + "b" * 64
        ).eval_handoff
        == ""
    )


def test_handoff_block_is_empty_when_there_is_no_contract() -> None:
    assert handoff_block("") == ""
    assert handoff_block("   \n ") == ""
    assert "row_id" in handoff_block("id column: row_id")
