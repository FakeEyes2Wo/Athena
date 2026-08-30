"""PREPARE 的两个 evaluator 必须各自拿到自己的工作区。

search 与 final evaluator 共用同一个 agent 类型名时，第二次注册会被
``AgentTypeRegistry.contains`` 跳过，final evaluator 的 ``write_file`` 与 shell cwd
于是停在 search evaluator 的目录里——留出集标签被写进了 SEARCH 看得见的地方，正好
违背 prepare_phase 自己那句「frozen separately so the SEARCH evaluator's labels are
never reused as the final test」。
"""

from pathlib import Path
from types import SimpleNamespace

from athena.research import prepare_phase
from athena.research.prepare_phase import _run_evaluator_agent


class _Registry:
    """只记录已注册类型名的 AgentTypeRegistry 替身。"""

    def __init__(self) -> None:
        self.types: list[str] = []

    def contains(self, agent_type: str) -> bool:
        return agent_type in self.types

    def add(self, agent_type: str) -> None:
        self.types.append(agent_type)


def _runtime(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        workspaces_root=tmp_path / "workspaces",
        registry=_Registry(),
        provider=object(),
        store=object(),
        execution=object(),
        agents=object(),
        scripts=object(),
        events=SimpleNamespace(project_agent_event=lambda *a: None),
        kaggle_tools=lambda agent_type: None,
        ask_user=None,
    )


async def test_each_evaluator_registers_its_own_workspace(tmp_path, monkeypatch):
    """两个 evaluator 各注册一个类型，各自绑定自己的目录。"""
    registered: list[tuple[str, Path]] = []
    planned: list[tuple[str, Path]] = []

    def fake_register(registry, **kwargs):
        registry.add(kwargs["agent_type"])
        registered.append((kwargs["agent_type"], kwargs["workspace"]))

    async def fake_run_plan(**kwargs):
        planned.append((kwargs["agent_type"], kwargs["evaluator_dir"]))
        return "ref"

    monkeypatch.setattr(prepare_phase, "register_evaluator_agent", fake_register)
    monkeypatch.setattr(prepare_phase, "run_evaluator_plan", fake_run_plan)
    rt = _runtime(tmp_path)
    workspaces = tmp_path / "workspaces"

    await _run_evaluator_agent(
        rt,
        directory_name="evaluator",
        agent_id="evaluator",
        plan_id="evaluator",
        task="build the search evaluator",
        label="evaluator",
    )
    await _run_evaluator_agent(
        rt,
        directory_name="final_evaluator",
        agent_id="final_evaluator",
        plan_id="final_evaluator",
        task="build the final evaluator",
        label="final_evaluator",
    )

    assert registered == [
        ("evaluator", workspaces / "evaluator"),
        ("final_evaluator", workspaces / "final_evaluator"),
    ]
    assert planned == [
        ("evaluator", workspaces / "evaluator"),
        ("final_evaluator", workspaces / "final_evaluator"),
    ]


async def test_repeating_the_same_evaluator_registers_it_once(tmp_path, monkeypatch):
    """同一个 evaluator 再跑一次不重复注册（注册是按类型名幂等的）。"""
    registered: list[str] = []

    def fake_register(registry, **kwargs):
        registry.add(kwargs["agent_type"])
        registered.append(kwargs["agent_type"])

    async def fake_run_plan(**kwargs):
        return "ref"

    monkeypatch.setattr(prepare_phase, "register_evaluator_agent", fake_register)
    monkeypatch.setattr(prepare_phase, "run_evaluator_plan", fake_run_plan)
    rt = _runtime(tmp_path)

    for _ in range(2):
        await _run_evaluator_agent(
            rt,
            directory_name="evaluator",
            agent_id="evaluator",
            plan_id="evaluator",
            task="build the search evaluator",
            label="evaluator",
        )

    assert registered == ["evaluator"]


async def test_final_evaluator_type_still_loads_the_evaluator_prompt(tmp_path):
    """final evaluator 用自己的类型名注册，但 prompt 仍须是 evaluator_agent.md。

    ``register_prompt_agent`` 默认按 ``agent_type`` 找 ``{type}_agent.md``；不把
    prompt 名与注册名解耦的话，final evaluator 起 turn 时会因为找不到
    ``final_evaluator_agent.md`` 直接炸掉——而这条路径 monkeypatch 掉注册的用例
    是看不见的。
    """
    from athena.agents.prepare_agent import register_evaluator_agent
    from athena.core.agent.registry import AgentTypeRegistry

    registry = AgentTypeRegistry()
    register_evaluator_agent(
        registry,
        provider=object(),
        artifacts=object(),
        workspace=tmp_path,
        runtime=None,
        agent_type="final_evaluator",
    )

    spec = registry.require_spec("final_evaluator", agent_id="final_evaluator")

    assert spec is not None
