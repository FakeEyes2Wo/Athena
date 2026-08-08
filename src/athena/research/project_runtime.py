"""项目 Composition Root（设计 §3.1/§6）：装配共享基础设施 + root_supervisor + 阶段投影。

单项目只创建一套共享设施：ArtifactStore、AgentTypeRegistry、AgentKernel。
新项目 ``create_root("supervisor", ...)`` 并把 ``root_supervisor_id`` 持久化；
重新打开项目读取该 id 并 follow-up 原实例。项目阶段（§4.2）与状态（§4.3）
由确定性投影推进并持久化，Kernel AgentStatus 与外部 phase 正交。
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

from athena.agents.base_runner import BaseAgentRunner
from athena.agents.code_agent import CodeAgent
from athena.agents.data_agent import DataAgent
from athena.agents.ideator_agent import IdeatorAgent
from athena.agents.orchestration import RunToolProjector
from athena.agents.plot_agent import PlotAgent
from athena.agents.reflection_agent import ReflectionAgent
from athena.agents.report_agent import ReportAgent
from athena.agents.supervisor import SupervisorAgent
from athena.core.agent_kernel.codec import JsonCodec
from athena.core.agent_kernel.kernel import AgentKernel
from athena.core.agent_kernel.registry import AgentTypeRegistry
from athena.core.agent_kernel.session import (
    RolloutResourcesFactory,
    SessionResourcesFactory,
)
from athena.core.agent_kernel.store import AgentGraphStore
from athena.core.agent_kernel.store_json import load_store_json, store_to_json
from athena.core.agent_kernel.types import AgentId, AgentSpec, AgentStatus
from athena.evaluation.policy import evaluate_data_analysis_review
from athena.evaluation.types import EvalSpec, EvalSpecChain, MetricDef
from athena.research.models import TaskMetaData
from athena.storage.artifact_store import LocalArtifactStore
from athena.storage.bundle import VersionedBundle


def _request(payload: dict) -> dict:
    return {"content": json.dumps(payload, ensure_ascii=False)}


class ProjectRuntime:
    """单项目共享基础设施与 root Supervisor 生命周期。"""

    def __init__(
        self,
        project_root: Path,
        *,
        resources_factory: SessionResourcesFactory | None = None,
    ) -> None:
        self._root = Path(project_root)
        self._state_path = self._root / ".athena" / "project.json"
        # 生产默认使用 rollout 资源工厂：私有记忆按 context_ref 持久化/恢复
        self._resources_factory = resources_factory or RolloutResourcesFactory(
            self._root
        )
        self._store = LocalArtifactStore(self._root / "artifacts")
        self._bundle = VersionedBundle(self._store)
        self._registry = AgentTypeRegistry()
        self._kernel = AgentKernel(
            resources_factory=self._resources_factory,
            type_registry=self._registry,
        )
        self._supervisor_id: AgentId | None = None
        self._phase = "IDLE"
        self._status = "RUNNING"
        self._task: TaskMetaData | None = None
        self._eval_specs = EvalSpecChain()

    @property
    def supervisor_id(self) -> AgentId | None:
        """root Supervisor 的稳定实例 id；未创建返回 None。"""
        return self._supervisor_id

    @property
    def phase(self) -> str:
        """项目阶段（§4.2：IDLE/CONFIGURED/PREPARE/...），外部投影词。"""
        return self._phase

    @property
    def status(self) -> str:
        """项目控制状态（§4.3：RUNNING/PAUSED/WAITING_FOR_HUMAN/...）。"""
        return self._status

    def project_status(self) -> str:
        """从 Kernel 事实派生控制状态（§4.3 确定性投影）。

        root Supervisor 处于持久化人工等待时投影为 WAITING_FOR_HUMAN。
        """
        if self._supervisor_id is not None:
            agent = self._kernel.agent_status(self._supervisor_id)
            if agent == AgentStatus.WAITING_FOR_HUMAN:
                return "WAITING_FOR_HUMAN"
        return self._status

    def projected_phase(self) -> str:
        """从已提交事实派生研究阶段（§4.2 完成条件，确定性投影）。

        只根据 TaskMetaData、DataAnalysis 版本与冻结 EvalSpec 是否存在计算，
        不依赖 Agent 的自然语言声明；未满足完成条件时不推进。
        """
        if self._task is None:
            return "IDLE"
        has_analysis = bool(self._bundle.chains())
        has_protocol = self._eval_specs.version > 0
        if has_analysis and has_protocol:
            return "PREPARE"
        return "CONFIGURED"

    @property
    def kernel(self) -> AgentKernel:
        return self._kernel

    @property
    def bundle(self) -> VersionedBundle:
        """DataAnalysis 版本所有权链（存储层事实）。"""
        return self._bundle

    @property
    def store(self) -> LocalArtifactStore:
        """项目内容寻址 ArtifactStore。"""
        return self._store

    @property
    def eval_specs(self) -> EvalSpecChain:
        """冻结评估协议（§7.2）：append-only EvalSpec 版本链。"""
        return self._eval_specs

    def freeze_eval_spec(
        self, primary: MetricDef, secondary: list[MetricDef] | None = None
    ) -> int:
        """冻结第一个评估协议版本（§7.2）；返回版本号。"""
        version = self._eval_specs.freeze(
            EvalSpec(primary=primary, secondary=secondary or [])
        )
        self._save_state()
        return version

    def register_defaults(self) -> None:
        """注册静态业务类型（确定性实现，每实例 factory）。"""
        self._registry.register(
            "supervisor",
            lambda _aid, _cfg=None: AgentSpec(
                runner=BaseAgentRunner(
                    SupervisorAgent(),
                    projector=RunToolProjector(),
                    agent_type="supervisor",
                ),
                codec=JsonCodec(),
            ),
        )
        self._registry.register(
            "data",
            lambda aid, _cfg=None: AgentSpec(
                runner=BaseAgentRunner(DataAgent(self._store, self._bundle, aid)),
                codec=JsonCodec(),
            ),
        )
        self._registry.register(
            "plot",
            lambda _aid, _cfg=None: AgentSpec(
                runner=BaseAgentRunner(PlotAgent(self._store)), codec=JsonCodec()
            ),
        )
        self._registry.register(
            "reflection",
            lambda _aid, _cfg=None: AgentSpec(
                runner=BaseAgentRunner(ReflectionAgent(self._store)),
                codec=JsonCodec(),
            ),
        )
        # §8 完成条件 1：七类型均为真实 factory（确定性实现，非占位）
        self._registry.register(
            "ideator",
            lambda _aid, _cfg=None: AgentSpec(
                runner=BaseAgentRunner(IdeatorAgent(self._store)), codec=JsonCodec()
            ),
        )
        self._registry.register(
            "code",
            lambda _aid, _cfg=None: AgentSpec(
                runner=BaseAgentRunner(CodeAgent(self._store)), codec=JsonCodec()
            ),
        )
        self._registry.register(
            "report",
            lambda _aid, _cfg=None: AgentSpec(
                runner=BaseAgentRunner(ReportAgent(self._store)), codec=JsonCodec()
            ),
        )

    async def configure(self, task: TaskMetaData) -> None:
        """确定性 CONFIGURE（§4.2）：提交有效任务事实 → CONFIGURED。"""
        if self._phase != "IDLE":
            raise RuntimeError(f"cannot configure from phase {self._phase}")
        self._task = task
        self._set_phase("CONFIGURED")

    async def prepare_data_analysis(
        self,
        data_path: str | Path,
        target: str,
        *,
        report: str | None = None,
        workspace: Path | None = None,
    ) -> str:
        """PREPARE DataAnalysis 评审闭环（§7.1）：返回接受版本 ref。

        DataAgent 生成并运行固定名分析脚本（读 ``data_path``、EDA、绘图到
        ``figures/``、写 ``report.md``）提交 v1；Reflection 评审 v1；通过接受
        v1，否则 follow-up 原 DataAgent 提交 v2。``report`` 可覆盖报告文本
        （确定性 failed 路径，如空报告触发评审失败）。
        """
        if self._phase != "CONFIGURED":
            raise RuntimeError(f"PREPARE requires CONFIGURED, got {self._phase}")
        self._set_phase("PREPARE")
        workspace = workspace or (self._root / "workspace" / "data")
        payload: dict[str, str] = {
            "data_path": str(Path(data_path).resolve()),
            "target": target,
            "workspace": str(workspace),
        }
        if report is not None:
            payload["report"] = report
        data_id, run1 = await self._kernel.create_root(
            "data", _request(payload), name="data-root"
        )
        await self._kernel.wait_run(run1, timeout=10)
        chains = self._bundle.chains()
        if not chains:
            raise RuntimeError("DataAgent produced no analysis chain")
        analysis_id, v1 = next(iter(chains.items()))
        # Reflection 只读评审 v1，产出 rubric + score
        _, run2 = await self._kernel.create_root(
            "reflection", _request({"data_analysis_ref": v1}), name="reflection-root"
        )
        summary2 = await self._kernel.wait_run(run2, timeout=10)
        review_ref = json.loads(summary2.response_ref)["result_ref"]
        # EvaluationPolicy（确定性）：按 rubric/score 判定，不解释报告正文
        verdict = await evaluate_data_analysis_review(self._store, review_ref)
        if verdict.passed:
            return v1
        # failed → follow-up 原 DataAgent 提交修订版
        run3 = await self._kernel.followup(data_id, _request(payload))
        await self._kernel.wait_run(run3, timeout=10)
        v2 = self._bundle.latest(analysis_id)
        if v2 is None:
            raise RuntimeError("revision produced no new version")
        return v2

    async def run_search(self, hypothesis: str) -> str:
        """SEARCH 阶段（设计 §5）：spawn Ideator 生成假设并推进阶段。

        返回假设 Artifact ref。真实 SEARCH 需 Ranker/CodeAgent/Policy 完整编排,
        此处为确定性最小步骤（§8 条件 2 六阶段流程推进）。
        """
        if self._phase not in ("CONFIGURED", "PREPARE"):
            raise RuntimeError(f"SEARCH requires CONFIGURED/PREPARE, got {self._phase}")
        self._set_phase("SEARCH")
        _, run_id = await self._kernel.create_root(
            "ideator", {"content": hypothesis}, name="ideator-root"
        )
        summary = await self._kernel.wait_run(run_id, timeout=10)
        response = json.loads(summary.response_ref)
        return response["result_ref"]

    async def run_validate(self, experiment_ref: str) -> str:
        """VALIDATE 阶段（§5）：对冻结 SOTA 执行确定性验证并推进阶段。

        真实 VALIDATE 需完整 ablation + 唯一 frozen final-test；此处为确定性
        最小步骤（spawn CodeAgent 记录验证结果）。
        """
        if self._phase != "SEARCH":
            raise RuntimeError(f"VALIDATE requires SEARCH, got {self._phase}")
        self._set_phase("VALIDATE")
        _, run_id = await self._kernel.create_root(
            "code", {"content": f"validate {experiment_ref}"}, name="validate-root"
        )
        summary = await self._kernel.wait_run(run_id, timeout=10)
        response = json.loads(summary.response_ref)
        return response["result_ref"]

    async def run_report(self, report_text: str) -> str:
        """REPORT 阶段（§5.5）：ReportAgent 产出 Bundle → Reflection 评审 → 通过。

        ReportAgent 提交 v1 报告 Bundle；ReflectionAgent 按报告 rubric 评审；
        EvaluationPolicy 判定：passed 接受 v1 并推进 COMPLETED，failed 则
        follow-up 原 ReportAgent 提交修订版 v2。返回接受的报告 Bundle ref。
        """
        if self._phase != "VALIDATE":
            raise RuntimeError(f"REPORT requires VALIDATE, got {self._phase}")
        self._set_phase("REPORT")
        report_id, run1 = await self._kernel.create_root(
            "report", {"content": report_text}, name="report-root"
        )
        summary1 = await self._kernel.wait_run(run1, timeout=10)
        v1 = json.loads(summary1.response_ref)["result_ref"]
        # Reflection 只读评审 v1，产出报告 rubric + score
        _, run2 = await self._kernel.create_root(
            "reflection", _request({"report_ref": v1}), name="report-review-root"
        )
        summary2 = await self._kernel.wait_run(run2, timeout=10)
        review_ref = json.loads(summary2.response_ref)["result_ref"]
        # EvaluationPolicy（确定性）：按 rubric/score 判定
        verdict = await evaluate_data_analysis_review(self._store, review_ref)
        if verdict.passed:
            self._set_phase("COMPLETED")
            return v1
        # failed → follow-up 原 ReportAgent 提交修订版 v2
        run3 = await self._kernel.followup(report_id, {"content": report_text})
        summary3 = await self._kernel.wait_run(run3, timeout=10)
        v2 = json.loads(summary3.response_ref)["result_ref"]
        return v2

    async def open(self, *, message: str | None = None) -> AgentId:
        """创建或恢复 root Supervisor；重新打开时 follow-up 原实例。"""
        self._load_state()
        await self._kernel.start()
        if self._supervisor_id is None:
            self._supervisor_id, _ = await self._kernel.create_root(
                "supervisor", {"content": message or ""}, name="supervisor"
            )
            self._save_state()
        elif message is not None:
            await self._kernel.followup(self._supervisor_id, {"content": message})
        return self._supervisor_id

    def pause(self) -> None:
        """项目 pause（§13）：停止派发新 turn，状态投影 PAUSED。"""
        self._kernel.pause()
        self._set_status("PAUSED")

    def resume(self) -> None:
        """项目 resume（§13）：恢复派发，状态投影 RUNNING。"""
        self._kernel.resume()
        self._set_status("RUNNING")

    async def stop(self) -> None:
        """项目 stop（§13）：中断活动 Run，保留已提交事实，投影 CANCELLED。

        停止派发（终态，不可 resume），等待中的 Agent 不删除其 wait/mailbox。
        """
        for snap in self._kernel.list_agents():
            if snap.status == AgentStatus.RUNNING:
                await self._kernel.interrupt(snap.agent_id, "project stop")
        self._kernel.pause()
        self._set_status("CANCELLED")

    async def close(self) -> None:
        await self._kernel.aclose()

    def _set_phase(self, phase: str) -> None:
        self._phase = phase
        self._save_state()

    def _set_status(self, status: str) -> None:
        self._status = status
        self._save_state()

    def _save_state(self) -> None:
        """原子、耐久化 JSON 持久化项目状态（§16.1）。

        metadata 与 Kernel store 快照都存为可读 JSON（§10 生产持久化基础）；
        先写临时文件并 fsync，再 ``os.replace`` 原子替换，崩溃不留半写文件。
        """
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "root_supervisor_id": self._supervisor_id,
            "phase": self._phase,
            "status": self._status,
            "eval_specs": [
                spec.model_dump(mode="json") for spec in self._eval_specs.versions
            ],
            "store": store_to_json(self._kernel._store),
        }
        temp = self._state_path.with_suffix(".json.tmp")
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self._state_path)

    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        with open(self._state_path, encoding="utf-8") as handle:
            data = json.load(handle)
        self._supervisor_id = data.get("root_supervisor_id")
        self._phase = data.get("phase", "IDLE")
        self._status = data.get("status", "RUNNING")
        if data.get("eval_specs"):
            self._eval_specs = EvalSpecChain.from_versions(
                [EvalSpec.model_validate(s) for s in data["eval_specs"]]
            )
        store = AgentGraphStore()
        load_store_json(data["store"], store)
        # §3.2/§16.1：恢复时校验 root 引用；缺引用但恰有一个合法 root 则修复并记录
        if self._supervisor_id is not None and store.agent(self._supervisor_id) is None:
            raise ValueError(
                f"corrupt project state: root supervisor "
                f"{self._supervisor_id} missing from store"
            )
        if self._supervisor_id is None:
            roots = [
                a.agent_id
                for a in store.agents().values()
                if a.parent_id is None and a.status is not AgentStatus.CLOSED
            ]
            if len(roots) == 1:
                self._supervisor_id = roots[0]
                logger.warning(
                    "repaired missing root supervisor reference -> %s",
                    self._supervisor_id,
                )
            elif len(roots) > 1:
                raise ValueError(
                    "multiple root supervisors in store; manual selection required"
                )
        self._kernel = AgentKernel(
            resources_factory=self._resources_factory,
            type_registry=self._registry,
            store=store,
        )
        self._kernel._rebuild_sessions()
        self._kernel._recover()
