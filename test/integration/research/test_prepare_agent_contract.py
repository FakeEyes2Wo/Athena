"""One-Agent PREPARE integration contract."""

import json
import subprocess
from pathlib import Path

import pytest

from athena.agents.prepare_agent import register_prepare_agent
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.provider import StreamEvent
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.artifact_store import LocalArtifactStore
from athena.core.git_workspace import LocalGitWorkspace
from athena.execution.runtime import CommandResult, ExecutionRuntime
from athena.research.evaluation import TrustedEvaluator
from athena.research.script_runner import DataScriptRunner
from athena.research.supervisor.prepare import run_prepare_plan


def _answer() -> str:
    return json.dumps(
        {"decision": "submit", "reason": "baseline ready", "suggestions": []}
    )


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
        outputs = {
            "predictions": "outputs/predictions.csv",
            "report": "outputs/report.md",
            "evaluator": "evaluator/eval.py",
        }
        if self.missing == "evaluator":
            outputs.pop("evaluator")
        if self.missing == "report":
            outputs.pop("report")
        actions: list[tuple[str, str] | None] = [
            (
                "evaluator/pyproject.toml",
                "[project]\nname='prepare-eval'\nversion='0.1.0'\n"
                "requires-python='>=3.11'\ndependencies=[]\n",
            ),
            (
                "evaluator/eval.py",
                "import csv,json,sys\n"
                "pred={r[0]:r[1] for r in list(csv.reader(open('predictions.csv')))[1:]}\n"
                "lab={r[0]:r[1] for r in list(csv.reader(open('labels.csv')))[1:]}\n"
                "score=sum(pred[k]==lab[k] for k in pred)/len(pred)\n"
                "json.dump({'primary':score},open(sys.argv[sys.argv.index('--output')+1],'w'))\n",
            ),
            ("evaluator/labels.csv", "id,label\nr1,0\nr2,1\n"),
            ("solution/features.py", "def feature(value):\n    return int(value)\n"),
            (
                "solution/model.py",
                "from solution.features import feature\n" "assert feature('1') == 1\n",
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
        if self.missing == "labels":
            actions = [
                action
                for action in actions
                if action is None or action[0] != "evaluator/labels.csv"
            ]
        return actions

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
        for marker in (
            "experiment.json",
            "evaluator draft is missing",
            "evaluator labels are missing",
            "report output",
        ):
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
            answer = _answer()
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

    async def run(self, context, command=None, *, argv=None, emit=None, **_kwargs):
        assert command is None
        assert argv is not None
        self.argv_calls.append(argv)
        root = context.workspace_root
        assert (root / "solution" / "features.py").is_file()
        assert (root / "solution" / "model.py").is_file()
        (root / "outputs").mkdir(exist_ok=True)
        if not self.missing_predictions:
            (root / "outputs" / "predictions.csv").write_text(
                "id,prediction\nr1,0\nr2,1\n", encoding="utf-8"
            )
        (root / "outputs" / "report.md").write_text(
            "# PREPARE baseline\n", encoding="utf-8"
        )
        result = CommandResult(ok=True, stdout="", stderr="", exit_code=0)
        if emit is not None:
            await emit("command/completed", "exec:prepare", result.to_dict())
        return result


class _Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        invalid_first: bool = False,
        missing: str | None = None,
    ) -> None:
        self.tmp_path = tmp_path
        self.repo = tmp_path / "repo"
        self.worktrees = tmp_path / "worktrees"
        self.store = LocalArtifactStore(tmp_path / "artifacts")
        self.provider = _PrepareProvider(invalid_first=invalid_first, missing=missing)
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
        runtime = ExecutionRuntime(project_root=workspace, store=self.store)
        registry = AgentTypeRegistry()
        register_prepare_agent(
            registry,
            provider=self.provider,
            artifacts=self.store,
            workspace=workspace,
            runtime=runtime,
        )
        self.agents = AgentRuntime(
            type_registry=registry,
            project_root=self.tmp_path,
            rollout_dir=self.tmp_path / ".athena" / "logs" / "agents",
        )
        self.agents.start()
        self.scripts = DataScriptRunner(
            store=self.store, workdir=self.tmp_path / ".athena" / "script-runs"
        )
        self.execution = _ManifestExecution(
            workspace, missing_predictions=self.missing == "predictions"
        )
        self.tree_ref = await self.store.put_text('{"experiments": []}')

    async def run(self, *, max_turns: int = 3, publish=None):
        return await run_prepare_plan(
            agents=self.agents,
            scripts=self.scripts,
            evaluator=TrustedEvaluator(self.scripts),
            git=self.git,
            workspace=self.branch,
            execution=self.execution,
            store=self.store,
            tree_ref=self.tree_ref,
            task="inspect data and build a trusted baseline",
            max_turns=max_turns,
            publish=publish,
        )

    async def close(self) -> None:
        await self.agents.aclose()
        await self.git.remove(self.branch, delete_branch=True, force=True)


@pytest.mark.asyncio
async def test_prepare_repairs_same_plan_and_submits_outputs(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, invalid_first=True)
    await harness.start()
    try:
        result = await harness.run()
        assert result.metric == 1.0
        assert "commands" in harness.provider.feedback_seen
        assert (Path(harness.branch.path) / "outputs" / "report.md").is_file()
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_prepare_uses_one_agent_plan_and_workspace(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    await harness.start()
    try:
        await harness.run()
        snapshots = harness.agents.list_agents()
        assert [(item.agent_id, item.name) for item in snapshots] == [
            ("prepare", "prepare")
        ]
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
        assert text_events[0][2] == {"delta": _answer(), "accumulated": _answer()}
        assert [event[0] for event in published].count("command/completed") == 1
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
        assert len(harness.agents.list_agents()) == 1
        assert harness.provider.turn == 2
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["labels", "evaluator", "report", "predictions"])
async def test_missing_labels_evaluator_report_or_predictions_cannot_submit(
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
        assert len(harness.agents.list_agents()) == 1
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
