"""Two-step PREPARE integration contract (evaluator freeze + experiment baseline)."""

import json
import subprocess
from pathlib import Path

import pytest

from athena.agents.prepare_agent import register_evaluator_agent, register_prepare_agent
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.provider import StreamEvent
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.artifact_store import LocalArtifactStore
from athena.core.git_workspace import LocalGitWorkspace
from athena.execution.runtime import (
    CommandRequest,
    CommandResult,
    ExecutionContext,
    ExecutionRuntime,
)
from athena.research.contracts import EvaluatorDescriptor
from athena.research.evaluation import TrustedEvaluator
from athena.research.script_runner import DataScriptRunner
from athena.research.supervisor.evaluator_plan import run_evaluator_plan
from athena.research.supervisor.prepare import run_prepare_plan

_EVALUATE_SCRIPT = (
    "import csv, json\n"
    "from pathlib import Path\n"
    "labels = {r[0]: r[1] for r in list(csv.reader("
    "open(Path(__file__).with_name('labels.csv'))))[1:]}\n"
    "preds = {r[0]: r[1] for r in list(csv.reader("
    "open('predictions/predictions.csv')))[1:]}\n"
    "score = sum(preds[k] == labels[k] for k in preds) / len(preds)\n"
    "print(json.dumps({'primary': score}))\n"
)


def _submit_answer() -> str:
    return json.dumps(
        {"decision": "submit", "reason": "baseline ready", "suggestions": []}
    )


class _EvaluatorProvider:
    model_name = "evaluator-integration"

    def __init__(self, *, missing: str | None = None) -> None:
        self.missing = missing
        self.calls = 0
        self._actions: list[tuple[str, str] | None] = []

    def _valid_actions(self) -> list[tuple[str, str] | None]:
        actions: list[tuple[str, str] | None] = [
            ("metric.json", '{"eval_script": "evaluate.py"}'),
            (
                "pyproject.toml",
                (
                    "[project]\nname = 'eval'\nversion = '0.1.0'\n"
                    "requires-python = '>=3.11'\ndependencies = []\n"
                ),
            ),
            ("evaluate.py", _EVALUATE_SCRIPT),
            ("labels.csv", "__athena_row_id,label\nr1,0\nr2,1\n"),
            (
                "HANDOFF.md",
                (
                    "# Eval contract\npredictions/predictions.csv (id,prediction); "
                    "accuracy over aligned ids\n"
                ),
            ),
            None,
        ]
        if self.missing == "labels":
            actions = [a for a in actions if a is None or a[0] != "labels.csv"]
        if self.missing == "evaluator":
            actions = [a for a in actions if a is None or a[0] != "metric.json"]
        return actions

    async def stream(self, _config, _tools, _messages, _cancel, **_kwargs):
        self.calls += 1
        if not self._actions:
            self._actions = self._valid_actions()
        action = self._actions.pop(0)
        if action is None:
            answer = _submit_answer()
            yield StreamEvent(
                kind="text_delta", data={"delta": answer, "accumulated": answer}
            )
        else:
            path, content = action
            yield StreamEvent(
                kind="function_call",
                data={
                    "call_id": f"write-{self.calls}",
                    "name": "write_file",
                    "arguments": {"path": path, "content": content},
                },
            )
        yield StreamEvent(kind="response_completed", data={"finish_reason": "stop"})


class _PrepareProvider:
    model_name = "prepare-integration"

    def __init__(
        self, *, invalid_first: bool = False, missing: str | None = None
    ) -> None:
        self.invalid_first = invalid_first
        self.missing = missing
        self.calls = 0
        self.turn = 0
        self.feedback_seen = ""
        self._actions: list[tuple[str, str] | None] = []

    def _valid_actions(self) -> list[tuple[str, str] | None]:
        outputs = {"predictions": "predictions", "report": "report.md"}
        if self.missing == "report":
            outputs.pop("report")
        return [
            ("solution/features.py", "def feature(value):\n    return int(value)\n"),
            (
                "solution/model.py",
                "from solution.features import feature\nassert feature('1') == 1\n",
            ),
            (
                "experiment.json",
                json.dumps(
                    {
                        "version": 1,
                        "commands": [["python", "solution/model.py"]],
                        "outputs": outputs,
                    }
                ),
            ),
            None,
        ]

    @staticmethod
    def _message_text(messages) -> str:
        return "\n".join(
            str(getattr(part, "content", ""))
            for message in messages
            for part in getattr(message, "parts", ())
        )

    async def stream(self, _config, _tools, messages, _cancel, **_kwargs):
        self.calls += 1
        text = self._message_text(messages)
        for marker in ("experiment.json", "report output"):
            if marker in text:
                self.feedback_seen = text
        if not self._actions:
            self.turn += 1
            if self.invalid_first and self.turn == 1:
                self._actions = [
                    ("experiment.json", '{"version":1,"commands":"python model.py"}'),
                    None,
                ]
            else:
                self._actions = self._valid_actions()
        action = self._actions.pop(0)
        if action is None:
            answer = _submit_answer()
            yield StreamEvent(
                kind="text_delta", data={"delta": answer, "accumulated": answer}
            )
        else:
            path, content = action
            yield StreamEvent(
                kind="function_call",
                data={
                    "call_id": f"write-{self.calls}",
                    "name": "write_file",
                    "arguments": {"path": path, "content": content},
                },
            )
        yield StreamEvent(kind="response_completed", data={"finish_reason": "stop"})


class _ManifestExecution:
    def __init__(self, root: Path, *, missing_predictions: bool = False) -> None:
        self.project_root = root
        self.environment_root = root
        self.missing_predictions = missing_predictions
        self.argv_calls: list[list[str]] = []

    def ensure_environment(self) -> None:
        """Stub：测试不涉及真实共享环境初始化。"""
        return

    async def collect_outputs(self, _subdirs: tuple[str, ...]) -> None:
        """Keep locally written fixture outputs in place for validation."""

    async def run(
        self, context: ExecutionContext, request: CommandRequest
    ) -> CommandResult:
        assert request.command is None
        assert request.argv is not None
        assert request.evaluation_split == "search"
        self.argv_calls.append(request.argv)
        root = context.workspace_root
        assert (root / "solution" / "features.py").is_file()
        assert (root / "solution" / "model.py").is_file()
        (root / "predictions").mkdir(parents=True, exist_ok=True)
        if not self.missing_predictions:
            (root / "predictions" / "predictions.csv").write_text(
                "id,prediction\nr1,0\nr2,1\n", encoding="utf-8"
            )
        (root / "report.md").write_text("# PREPARE baseline\n", encoding="utf-8")
        result = CommandResult(ok=True, stdout="", stderr="", exit_code=0)
        if request.emit is not None:
            await request.emit("command/completed", "exec:prepare", result.to_dict())
        return result


class _Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        invalid_first: bool = False,
        missing: str | None = None,
        evaluator_missing: str | None = None,
    ) -> None:
        self.tmp_path = tmp_path
        self.repo = tmp_path / "repo"
        self.worktrees = tmp_path / "worktrees"
        self.store = LocalArtifactStore(tmp_path / "artifacts")
        self.prepare_provider = _PrepareProvider(
            invalid_first=invalid_first, missing=missing
        )
        self.evaluator_provider = _EvaluatorProvider(missing=evaluator_missing)
        self.missing = missing

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    async def start(self) -> None:
        self.repo.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.name", "Athena Test")
        self._git("config", "user.email", "athena@example.invalid")
        (self.repo / "data.csv").write_text("id,value\nr1,0\nr2,1\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "--no-gpg-sign", "-m", "base")
        base = self._git("rev-parse", "HEAD")

        async def write_diff(content: bytes) -> str:
            return await self.store.put_bytes(content)

        self.git = LocalGitWorkspace(self.repo, self.worktrees, write_diff)
        self.branch = await self.git.create(base, "athena/prepare")
        workspace = Path(self.branch.path)
        self.runtime = ExecutionRuntime(project_root=workspace, store=self.store)
        self.scripts = DataScriptRunner(
            store=self.store, workdir=self.tmp_path / ".athena" / "script-runs"
        )
        self.execution = _ManifestExecution(
            workspace, missing_predictions=self.missing == "predictions"
        )
        self.tree_ref = await self.store.put_text('{"experiments": []}')

    def _agent_runtime(self, registry: AgentTypeRegistry) -> AgentRuntime:
        return AgentRuntime(
            type_registry=registry,
            project_root=self.tmp_path,
            rollout_dir=self.tmp_path / ".athena" / "logs" / "agents",
        )

    async def freeze_evaluator(self, *, max_turns: int = 3) -> str:
        evaluator_dir = self.tmp_path / "evaluator"
        evaluator_dir.mkdir(parents=True, exist_ok=True)
        registry = AgentTypeRegistry()
        register_evaluator_agent(
            registry,
            provider=self.evaluator_provider,
            artifacts=self.store,
            workspace=evaluator_dir,
            runtime=self.runtime,
        )
        agents = self._agent_runtime(registry)
        agents.start()
        try:
            return await run_evaluator_plan(
                agents=agents,
                scripts=self.scripts,
                store=self.store,
                evaluator_dir=evaluator_dir,
                execution=self.execution,
                task="write the evaluator",
                max_turns=max_turns,
            )
        finally:
            await agents.aclose()

    async def run(self, *, max_turns: int = 3, publish=None):
        evaluator_ref = await self.freeze_evaluator()
        workspace = Path(self.branch.path)
        registry = AgentTypeRegistry()
        register_prepare_agent(
            registry,
            provider=self.prepare_provider,
            artifacts=self.store,
            workspace=workspace,
            runtime=self.runtime,
        )
        self.agents = self._agent_runtime(registry)
        self.agents.start()
        return await run_prepare_plan(
            agents=self.agents,
            evaluator=TrustedEvaluator(self.scripts),
            git=self.git,
            workspace=self.branch,
            execution=self.execution,
            store=self.store,
            evaluator_ref=evaluator_ref,
            tree_ref=self.tree_ref,
            task="inspect data and build a trusted baseline",
            max_turns=max_turns,
            publish=publish,
        )

    async def close(self) -> None:
        if getattr(self, "agents", None) is not None:
            await self.agents.aclose()
        await self.git.remove(self.branch, delete_branch=True, force=True)


@pytest.mark.asyncio
async def test_evaluator_plan_freezes_a_bundle(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    await harness.start()
    try:
        evaluator_ref = await harness.freeze_evaluator()
        descriptor = EvaluatorDescriptor.model_validate_json(
            await harness.store.get_text(evaluator_ref)
        )
        evaluator_dir = Path(descriptor.dir_path)
        assert (evaluator_dir / "README.md").is_file()
        assert (evaluator_dir / "evaluate.py").is_file()
        assert (evaluator_dir / "labels.csv").is_file()
        assert (evaluator_dir / "HANDOFF.md").is_file()
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_prepare_repairs_same_plan_and_submits_outputs(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, invalid_first=True)
    await harness.start()
    try:
        result = await harness.run()
        assert result.metric == 1.0
        assert "commands" in harness.prepare_provider.feedback_seen
        assert (Path(harness.branch.path) / "report.md").is_file()
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_prepare_uses_one_agent_plan_and_workspace(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    await harness.start()
    try:
        await harness.run()
        # PREPARE reaps its one-shot agent after the phase completes; the rollout
        # file remains on disk for audit/resume, but the facade no longer holds it.
        assert harness.agents.list_agents() == []
        assert (tmp_path / ".athena" / "logs" / "agents" / "prepare.jsonl").is_file()
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_prepare_forwards_agent_text_delta_to_publisher(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    await harness.start()
    published: list[tuple[str, str, dict | None]] = []

    async def publish(kind: str, event_ref: str, data: dict | None) -> None:
        published.append((kind, event_ref, data))

    try:
        await harness.run(publish=publish)
        text_events = [event for event in published if event[0] == "agent/text_delta"]
        assert len(text_events) == 1
        assert text_events[0][2] == {
            "delta": _submit_answer(),
            "accumulated": _submit_answer(),
        }
        assert [event[0] for event in published].count("command/completed") == 1
        kinds = [event[0] for event in published]
        first_call = kinds.index("agent/function_call")
        first_tool_end = kinds.index("tool/end")
        final_text = kinds.index("agent/text_delta")
        visible_agent_events = [
            kind
            for kind in kinds
            if kind in {"agent/function_call", "tool/end", "agent/text_delta"}
        ]
        assert visible_agent_events[:2] == ["agent/function_call", "tool/end"]
        assert visible_agent_events[-1] == "agent/text_delta"
        assert first_call < first_tool_end < final_text
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_prepare_accepts_an_arbitrary_multifile_solution(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    await harness.start()
    try:
        result = await harness.run()
        assert result.metric == 1.0
        assert harness.execution.argv_calls == [["python", "solution/model.py"]]
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_invalid_manifest_is_repaired_by_the_same_agent(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, invalid_first=True)
    await harness.start()
    try:
        await harness.run()
        # The repaired PREPARE agent is reaped after the run; the same logical
        # agent is still evidenced by the provider having seen two turns.
        assert harness.agents.list_agents() == []
        assert harness.prepare_provider.turn == 2
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["labels", "evaluator"])
async def test_missing_labels_or_evaluator_cannot_submit(
    tmp_path: Path, missing: str
) -> None:
    harness = _Harness(tmp_path, evaluator_missing=missing)
    await harness.start()
    try:
        with pytest.raises(RuntimeError, match="turn budget exhausted"):
            await harness.freeze_evaluator(max_turns=1)
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["report", "predictions"])
async def test_missing_report_or_predictions_cannot_submit(
    tmp_path: Path, missing: str
) -> None:
    harness = _Harness(tmp_path, missing=missing)
    await harness.start()
    try:
        with pytest.raises(RuntimeError, match="turn budget exhausted"):
            await harness.run(max_turns=1)
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_prepare_has_no_internal_step_state(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, invalid_first=True)
    await harness.start()
    try:
        await harness.run()
        assert harness.agents.list_agents() == []
        assert not (
            Path(harness.branch.path) / ".athena" / "prepare-state.json"
        ).exists()
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_prepare_returns_result_without_writing_state_or_tree(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    await harness.start()
    try:
        result = await harness.run()
        assert result.commit
        assert not (tmp_path / ".athena" / "state.json").exists()
        assert not (tmp_path / ".athena" / "research_tree.json").exists()
    finally:
        await harness.close()
