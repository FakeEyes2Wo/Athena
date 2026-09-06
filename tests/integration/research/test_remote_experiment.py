"""端到端：一次实验真的跑在租来的机器上，产物真的回到本地被打分。

这一组是整条链路的验收判据，对应设计文档第十节末尾那几条**反过来会红**的断言。
它守的失败模式和 ``corpus_ref`` 两臂都是 ``None`` 是同一类：全链路绿、分数照出、
日志照有，但跑的根本不是你以为的那台机器。

"远端"由本地子进程跑同一份 agent 扮演，GPU 事实用假 ``nvidia-smi`` 输出注入。
除此之外都是真的：真租约、真通道、真镜像、真进程、真打分。
"""

import json
import sys
from pathlib import Path

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.core.workspace import GitDiff, GitWorkBranch
from athena.execution.pool import GpuPool
from athena.execution.remote.channel import RemoteChannel, SubprocessTransport
from athena.execution.remote.ssh import SshHost
from athena.execution.runtime import ExecutionContext, ExecutionRuntime
from athena.research.contracts import CandidateEvaluation, EvaluatorDescriptor
from athena.research.supervisor.experiment import PlanRunner
from athena.research.supervisor.plans import PlanInput, PlanState

_REF = "sha256:" + "a" * 64


class _FakeTransport(SubprocessTransport):
    def __init__(self, host: SshHost) -> None:
        super().__init__(sys.executable)
        self._host = host

    @property
    def description(self) -> str:
        return f"fake:{self._host.name}"


@pytest.fixture
def fake_gpus(monkeypatch):
    real_request = RemoteChannel.request
    table: dict[str, list[dict]] = {}

    async def request(self, op: str, **fields):
        reply = await real_request(self, op, **fields)
        if op == "probe" and self.description.startswith("fake:"):
            reply = {**reply, "gpus": table.get(self.description[5:], [])}
        return reply

    monkeypatch.setattr(RemoteChannel, "request", request)
    return table


class _Evaluator:
    """可信评估器替身——它永远在控制节点上跑，看得到预测就说明产物回来了。"""

    def __init__(self) -> None:
        self.scored_predictions: dict[str, bytes] | None = None

    async def score(self, **kwargs) -> CandidateEvaluation:
        self.scored_predictions = dict(kwargs["predictions"])
        return CandidateEvaluation(
            candidate_id=kwargs["candidate_id"],
            test_score=0.75,
            direction=kwargs.get("direction", "maximize"),
        )


async def _evaluator_ref(store: LocalArtifactStore, base: Path) -> str:
    evaluator_dir = base / "eval"
    evaluator_dir.mkdir(parents=True, exist_ok=True)
    readme = "# Evaluator Freeze Marker\n\nDo not edit.\n"
    (evaluator_dir / "README.md").write_text(readme, encoding="utf-8")
    readme_ref = await store.put_text(readme)
    descriptor = EvaluatorDescriptor(
        dir_path=str(evaluator_dir),
        readme_ref=readme_ref,
        entrypoint="eval.py",
    )
    return await store.put_text(descriptor.model_dump_json())


class _Workspace:
    def __init__(self) -> None:
        self.commits: list[str] = []

    async def diff(self, branch) -> GitDiff:
        return GitDiff(ref=_REF, paths=("train.py",))

    async def commit(self, branch, diff, message) -> str:
        self.commits.append(message)
        return "c1"


async def _lease_runtime(tmp_path, fake_gpus, workspace_dir: Path, store):
    fake_gpus["gpu-01"] = [
        {
            "index": 0,
            "name": "NVIDIA A100",
            "memory_total_mib": 81920,
            "memory_used_mib": 0,
            "utilization_pct": 0,
        }
    ]
    pool = GpuPool(
        [
            SshHost(
                name="gpu-01",
                alias="gpu01.lab",
                scratch=(tmp_path / "scratch").as_posix(),
            )
        ],
        transport_factory=_FakeTransport,
        store=store,
    )
    lease = await pool.acquire("h1", local_workspace=workspace_dir)
    runtime = ExecutionRuntime(
        project_root=tmp_path,
        environment_root=tmp_path,
        store=store,
        backend=lease.backend,
    )
    return pool, lease, runtime


def _write_experiment(workspace: Path) -> None:
    """一个最小但真实的实验：写预测、写报告，并报出自己跑在哪。

    脚本用 write_bytes 而不是 write_text：预测文件的字节要能被逐字比对，
    text 模式会按执行方的平台做行尾转换，把一个真实的断言变成平台噪声。
    """
    source = [
        "import os, pathlib, socket",
        "out = pathlib.Path('outputs/predictions')",
        "out.mkdir(parents=True, exist_ok=True)",
        "body = 'id,pred' + chr(10) + '1,0.9' + chr(10)",
        "out.joinpath('pred.csv').write_bytes(body.encode())",
        # 二进制预测：后缀不在「源码类」白名单里，只可能靠 collect_outputs
        # 回来。少了它，这一组用例对「产物根本没拉回来」是瞎的。
        "out.joinpath('pred.npy').write_bytes(bytes(range(64)))",
        "pathlib.Path('report.md').write_bytes(",
        "    ('ran on ' + socket.gethostname() + chr(10)).encode())",
        "print('done', os.environ.get('CUDA_VISIBLE_DEVICES'))",
    ]
    (workspace / "train.py").write_text(chr(10).join(source), encoding="utf-8")
    (workspace / "experiment.json").write_text(
        json.dumps(
            {
                "version": 1,
                "commands": [[sys.executable, "train.py"]],
                "outputs": {
                    "predictions": "outputs/predictions",
                    "report": "report.md",
                },
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_an_experiment_runs_remotely_and_its_outputs_come_home(
    tmp_path, fake_gpus
) -> None:
    """产物必须回到控制节点：评估器与测试标签永远不出门。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_experiment(workspace)

    pool, lease, execution = await _lease_runtime(tmp_path, fake_gpus, workspace, store)
    evaluator = _Evaluator()
    bundle_ref = await _evaluator_ref(store, tmp_path)
    branch = GitWorkBranch(
        path=str(workspace), branch="athena/plan/h1", base_commit="c0"
    )
    runner = PlanRunner(
        execution=execution,
        store=store,
        evaluator=evaluator,
        workspace=_Workspace(),
        branch=branch,
        context=ExecutionContext(
            project_root=tmp_path,
            workspace_root=workspace,
            environment_root=tmp_path,
            experiment_id="h1",
        ),
        timeout_s=120,
        placement=lease.placement,
    )
    state = PlanState(
        kind="SEARCH", context_ref=_REF, turns_used=0, turn_limit=12, patience=4
    )

    try:
        result = await runner.run_turn(
            "h1", state, PlanInput(evaluator_ref=bundle_ref, tree_ref=_REF)
        )
    finally:
        await pool.release("h1")

    assert result.kind == "scored", result.error
    assert result.metric == 0.75
    # 预测确实回到了本地评估器手里，而不是留在"远端"。
    assert evaluator.scored_predictions == {
        "pred.csv": ("id,pred" + chr(10) + "1,0.9" + chr(10)).encode(),
        "pred.npy": bytes(range(64)),
    }
    # 本地工作区里也真的有那两份产物。
    assert (workspace / "outputs" / "predictions" / "pred.csv").is_file()
    assert (workspace / "outputs" / "predictions" / "pred.npy").is_file()


@pytest.mark.asyncio
async def test_the_evidence_says_which_machine_it_ran_on(tmp_path, fake_gpus) -> None:
    """异构算力下 A 臂 A100、B 臂 3090 的分数不可比——不记下来就无从发现。

    这条也是"静默没走远程"的探针：真的走了远程，evidence 里就一定有 placement，
    且 host 不是 local。
    """
    store = LocalArtifactStore(tmp_path / "artifacts")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_experiment(workspace)

    pool, lease, execution = await _lease_runtime(tmp_path, fake_gpus, workspace, store)
    bundle_ref = await _evaluator_ref(store, tmp_path)
    runner = PlanRunner(
        execution=execution,
        store=store,
        evaluator=_Evaluator(),
        workspace=_Workspace(),
        branch=GitWorkBranch(
            path=str(workspace), branch="athena/plan/h1", base_commit="c0"
        ),
        context=ExecutionContext(
            project_root=tmp_path,
            workspace_root=workspace,
            environment_root=tmp_path,
            experiment_id="h1",
        ),
        timeout_s=120,
        placement=lease.placement,
    )
    state = PlanState(
        kind="SEARCH", context_ref=_REF, turns_used=0, turn_limit=12, patience=4
    )

    try:
        result = await runner.run_turn(
            "h1", state, PlanInput(evaluator_ref=bundle_ref, tree_ref=_REF)
        )
        evidence = json.loads(await store.get_text(result.evidence_ref))
    finally:
        await pool.release("h1")

    placement = evidence["placement"]
    assert placement["host"] == "gpu-01"
    assert placement["host"] != "local"
    assert placement["gpu_model"] == "NVIDIA A100"
    assert placement["gpu_ids"] == [0]
    assert "queued_seconds" in placement


@pytest.mark.asyncio
async def test_local_execution_records_no_placement(tmp_path) -> None:
    """本地算力时不该凭空造一个 placement——"没有"和"是 local"是两回事。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_experiment(workspace)

    bundle_ref = await _evaluator_ref(store, tmp_path)
    runner = PlanRunner(
        execution=ExecutionRuntime(
            project_root=tmp_path, environment_root=tmp_path, store=store
        ),
        store=store,
        evaluator=_Evaluator(),
        workspace=_Workspace(),
        branch=GitWorkBranch(
            path=str(workspace), branch="athena/plan/h1", base_commit="c0"
        ),
        context=ExecutionContext(
            project_root=tmp_path,
            workspace_root=workspace,
            environment_root=tmp_path,
            experiment_id="h1",
        ),
        timeout_s=120,
    )
    state = PlanState(
        kind="SEARCH", context_ref=_REF, turns_used=0, turn_limit=12, patience=4
    )

    result = await runner.run_turn(
        "h1", state, PlanInput(evaluator_ref=bundle_ref, tree_ref=_REF)
    )
    evidence = json.loads(await store.get_text(result.evidence_ref))

    assert result.kind == "scored", result.error
    assert "placement" not in evidence
