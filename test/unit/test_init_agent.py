"""InitAgent 单元测试（Task 8）：LLM 驱动编排 + fake 内层 builder。

InitAgent 外层确定性编排：构造内层 LLM agent（init_agent.md prompt）→ 运行 →
读取固定名 ``task_understanding.md`` + ``eval.py`` → 语法自检 → 打包 Artifact
payload ``{"task_understanding", "eval_script"}``。测试注入 ``fake_inner_builder``
预写产物，不 hit 真实 LLM API。
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.agents.init_agent import EVAL_ENTRYPOINT, InitAgent, _validated_workspace
from athena.core.agent.models import AgentContext
from athena.core.artifact_store import LocalArtifactStore
from athena.core.thread_models import AthenaThread, AthenaTurn
from athena.core.tool import ToolRegistry
from test.unit._support import fake_inner_builder


async def _noop_emit(*_a):
    pass


def _context(input_text: str) -> AgentContext:
    return AgentContext(
        thread=AthenaThread(
            thread_id="t1", session_id="s1", status="running", context_ref="ctx://0"
        ),
        turn=AthenaTurn(
            turn_id="t1.1", thread_id="t1", request_ref="req://1", status="running"
        ),
        emit=_noop_emit,
        tools=ToolRegistry(),
        cancel=asyncio.Event(),
        input_text=input_text,
    )


def _agent(store) -> InitAgent:
    return InitAgent(store, model="fake", client=None, inner_builder=fake_inner_builder)


@pytest.mark.asyncio
async def test_init_agent_payload_has_task_understanding_and_eval_script(
    tmp_path,
) -> None:
    """LLM 驱动：fake 内层写 task_understanding.md + eval.py → payload 双字段，eval 语法自检通过。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    agent = _agent(store)
    workspace = tmp_path / "prepare-eval"
    ctx = _context(
        json.dumps(
            {
                "data_path": str(tmp_path / "dataset.csv"),
                "target": "label",
                "workspace": str(workspace),
            }
        )
    )

    outcome = await agent.run(ctx)

    assert outcome.result_ref.startswith("sha256:")
    payload = json.loads(await store.get_text(outcome.result_ref))
    assert set(payload) == {
        "task_understanding",
        "eval_script",
        "eval_workspace",
        "eval_metadata",
    }
    assert "# Task Understanding" in payload["task_understanding"]  # 固定格式报告
    assert "predictions.csv" in payload["eval_script"]  # 自包含 eval.py 契约
    compile(payload["eval_script"], EVAL_ENTRYPOINT, "exec")  # 语法自检
    # eval 工作区可直接被 scripts.freeze 冻结为 bundle（uv project + entrypoint）
    ws = Path(payload["eval_workspace"])
    assert (ws / "pyproject.toml").is_file()
    assert payload["eval_metadata"] == {"entrypoint": EVAL_ENTRYPOINT}
    assert ws == workspace


@pytest.mark.asyncio
async def test_init_agent_requires_data_path(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    agent = _agent(store)

    with pytest.raises(ValueError, match="data_path"):
        await agent.run(_context("{}"))


@pytest.mark.asyncio
async def test_init_agent_requires_target(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    agent = _agent(store)

    with pytest.raises(ValueError, match="target"):
        await agent.run(
            _context(
                json.dumps(
                    {
                        "data_path": str(tmp_path / "dataset.csv"),
                        "workspace": str(tmp_path / "prepare-eval"),
                    }
                )
            )
        )


@pytest.mark.asyncio
async def test_init_agent_requires_an_assigned_workspace(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    agent = _agent(store)

    with pytest.raises(ValueError, match="workspace"):
        await agent.run(
            _context(
                json.dumps(
                    {"data_path": str(tmp_path / "dataset.csv"), "target": "label"}
                )
            )
        )


def test_init_agent_rejects_workspace_outside_project_root(tmp_path) -> None:
    """workspace 落在 project_root 之外 → 拒绝，不允许逃逸到系统 temp。"""
    runtime = SimpleNamespace(project_root=str(tmp_path / "project"))
    with pytest.raises(ValueError, match="outside project root"):
        _validated_workspace(str(tmp_path / "outside"), runtime)
