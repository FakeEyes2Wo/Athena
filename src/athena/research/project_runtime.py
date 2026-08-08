"""项目 Composition Root（设计 §3.1/§6）：装配共享基础设施 + root_supervisor + 阶段投影。

单项目只创建一套共享设施：ArtifactStore、AgentTypeRegistry、AgentRuntime（Thread 门面）。
新项目 ``create_root("supervisor", ...)`` 并把 ``root_supervisor_id`` 持久化；
重新打开项目读取该 id 并经确定性 rollout 恢复会话。项目阶段（§4.2）与状态（§4.3）
由确定性投影推进并持久化，Agent 状态与外部 phase 正交。project.json 只存确定性
事实；对话经 ``.athena/sessions/{agent_id}.jsonl`` Codex 风格轻持久化。
"""

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from athena.agents.base_runner import BaseAgentRunner
from athena.agents.data_agent import DataAgent
from athena.agents.init_agent import InitAgent
from athena.agents.orchestration import RunToolProjector
from athena.agents.reflection_agent import (
    ReflectionAgent,
    evaluate_data_analysis_review,
)
from athena.agents.report_agent import ReportAgent
from athena.agents.simple_agents import CodeAgent, IdeatorAgent, PlotAgent
from athena.agents.supervisor import SupervisorAgent
from athena.core.agent.agent_runtime import AgentRuntime

from athena.core.agent.codec import JsonCodec
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.agent.types import (
    AgentCommandError,
    AgentId,
    AgentSpec,
    AgentStatus,
    RunId,
)
from athena.core.research_models import Hypothesis
from athena.core.research_tree import ResearchTree
from athena.memory.index import MemoryStore
from athena.research.budget import BudgetSnapshot
from athena.research.models import EvalSpec, EvalSpecChain, MetricDef, TaskMetaData
from athena.core.artifact_store import LocalArtifactStore
from athena.core.bundle import VersionedBundle

# LLM Agent 映射（首版为空——spec「首版否」；后续按 spec 映射表填充即可启用转换）
LLM_AGENT_MAPPING: dict[str, dict] = {}


def _request(payload: dict) -> dict:
    return {"content": json.dumps(payload, ensure_ascii=False)}


class ProjectRuntime:
    """单项目共享基础设施与 root Supervisor 生命周期。"""

    def __init__(self, project_root: Path) -> None:
        self._root = Path(project_root)
        self._state_path = self._root / ".athena" / "project.json"
        self._store = LocalArtifactStore(self._root / "artifacts")
        self._bundle = VersionedBundle(self._store)
        self._registry = AgentTypeRegistry()
        self._runtime = AgentRuntime(
            type_registry=self._registry,
            project_root=self._root,
            rollout_dir=self._root / ".athena" / "sessions",
        )
        self._supervisor_id: AgentId | None = None
        self._phase = "IDLE"
        self._status = "RUNNING"
        self._task: TaskMetaData | None = None
        self._eval_specs = EvalSpecChain()
        # 各阶段已提交事实 refs（端到端 §3 项目最小事实）
        self._sota_ref: str | None = None
        self._validation_ref: str | None = None
        self._report_ref: str | None = None
        # 项目记忆（memory-human-wait §3）：已验证事实/决定/修复
        self._memory = MemoryStore()
        # SEARCH 预算（端到端 §5.3）：预算耗尽即停止
        self._budget = BudgetSnapshot()
        # ResearchTree（设计 §4 所有权）：假设/实验/SOTA 的唯一所有者
        self._tree = ResearchTree()

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
        """从 Agent 状态投影控制状态（§4.3 确定性投影）。

        root Supervisor 处于持久化人工等待时投影为 WAITING_FOR_HUMAN。
        """
        if self._supervisor_id is not None:
            agent = self._runtime.agent_status(self._supervisor_id)
            if agent == AgentStatus.WAITING_FOR_HUMAN:
                return "WAITING_FOR_HUMAN"
        return self._status

    def projected_phase(self) -> str:
        """从已提交事实派生研究阶段（§4.2 完成条件，确定性投影）。

        按六阶段硬门槛推进，只根据已提交的 task/DataAnalysis/EvalSpec/SOTA/
        validation/report 事实计算，不依赖 Agent 声明；未满足时不推进。
        """
        if self._task is None:
            return "IDLE"
        if self._report_ref is not None:
            return "COMPLETED"
        if self._validation_ref is not None:
            return "REPORT"
        if self._sota_ref is not None:
            return "VALIDATE"
        has_analysis = bool(self._bundle.chains())
        has_protocol = self._eval_specs.version > 0
        if has_analysis and has_protocol:
            return "PREPARE"
        return "CONFIGURED"

    @property
    def kernel(self) -> AgentRuntime:
        """AgentRuntime 门面（Thread 模型），方法签名与退役前 AgentKernel 对齐。"""
        return self._runtime

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

    @property
    def memory(self) -> MemoryStore:
        """项目记忆索引（memory-human-wait §3）：已验证事实。"""
        return self._memory

    @property
    def budget(self) -> BudgetSnapshot:
        """SEARCH 预算快照（端到端 §5.3）：剩余实验与连续无改进计数。"""
        return self._budget

    @property
    def tree(self) -> ResearchTree:
        """ResearchTree（设计 §4）：假设/实验/SOTA 的唯一所有者。"""
        return self._tree

    def add_project_memory(
        self, *, kind: str, summary: str, source_refs: list[str], created_by: str
    ) -> str:
        """提交已验证事实为项目记忆，返回 entry_id 并持久化。"""
        entry_id = self._memory.add(
            scope="project",
            kind=kind,
            summary=summary,
            source_refs=source_refs,
            created_by=created_by,
        )
        self._save_state()
        return entry_id

    def freeze_eval_spec(
        self, primary: MetricDef, secondary: list[MetricDef] | None = None
    ) -> int:
        """冻结第一个评估协议版本（§7.2）；返回版本号。"""
        version = self._eval_specs.freeze(
            EvalSpec(primary=primary, secondary=secondary or [])
        )
        self._save_state()
        return version

    def register_defaults(
        self, *, model: str | None = None, client: Any = None
    ) -> None:
        """注册静态业务类型（确定性实现，每实例 factory）。"""
        # 首版框架：LLM_AGENT_MAPPING 为空，全部保持确定性骨架。
        # 后续填充映射 + 传入 model 时，改用 create_agent_for 注册 LLM Agent。
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
            "init",
            lambda _aid, _cfg=None: AgentSpec(
                runner=BaseAgentRunner(InitAgent(self._store)), codec=JsonCodec()
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
        eval_script: str | None = None,
    ) -> str:
        """PREPARE DataAnalysis 评审闭环（§7.1）：返回接受版本 ref。

        InitAgent 先做 task understanding：读数据集 schema 生成冻结的
        ``eval.py`` 并回填 ``EvalSpec``（用户首轮输入可用 ``eval_script``
        直接指定）。随后 DataAgent 生成并运行固定名分析脚本（读 ``data_path``、
        EDA、绘图到 ``figures/``、写 ``report.md``）提交 v1；Reflection 评审
        v1；通过接受 v1，否则 follow-up 原 DataAgent 提交 v2。``report`` 可
        覆盖报告文本（确定性 failed 路径，如空报告触发评审失败）。
        """
        if self._phase != "CONFIGURED":
            raise RuntimeError(f"PREPARE requires CONFIGURED, got {self._phase}")
        self._set_phase("PREPARE")
        workspace = workspace or (self._root / "workspace" / "data")
        await self._run_init_understanding(data_path, target, eval_script)
        payload: dict[str, str] = {
            "data_path": str(Path(data_path).resolve()),
            "target": target,
            "workspace": str(workspace),
        }
        if report is not None:
            payload["report"] = report
        data_id, run1 = await self._runtime.create_root(
            "data", _request(payload), name="data-root"
        )
        await self._runtime.wait_run(run1, timeout=10)
        chains = self._bundle.chains()
        if not chains:
            raise RuntimeError("DataAgent produced no analysis chain")
        analysis_id, v1 = next(iter(chains.items()))
        # Reflection 只读评审 v1，产出 rubric + score
        _, run2 = await self._runtime.create_root(
            "reflection", _request({"data_analysis_ref": v1}), name="reflection-root"
        )
        summary2 = await self._runtime.wait_run(run2, timeout=10)
        review_ref = json.loads(summary2.response_ref)["result_ref"]
        # EvaluationPolicy（确定性）：按 rubric/score 判定，不解释报告正文
        verdict = await evaluate_data_analysis_review(self._store, review_ref)
        if verdict.passed:
            return v1
        # failed → follow-up 原 DataAgent 提交修订版
        run3 = await self._runtime.followup(data_id, _request(payload))
        await self._runtime.wait_run(run3, timeout=10)
        v2 = self._bundle.latest(analysis_id)
        if v2 is None:
            raise RuntimeError("revision produced no new version")
        return v2

    async def _run_init_understanding(
        self,
        data_path: str | Path,
        target: str,
        eval_script: str | None,
    ) -> None:
        """InitAgent task understanding：生成 eval.py 并冻结 EvalSpec。

        请求带 ``data_path``/``target``；用户首轮输入可用 ``eval_script``
        覆盖默认生成。InitAgent 把 eval.py 写为 Artifact，调用方读取后回填
        ``EvalSpec.eval_script`` 并冻结协议版本（§7.2）。重复调用（PREPARE
        重入）不覆盖已冻结协议。
        """
        if self._eval_specs.version > 0:
            return
        request: dict[str, str] = {
            "data_path": str(Path(data_path).resolve()),
            "target": target,
        }
        if eval_script is not None:
            request["eval_script"] = eval_script
        _, run = await self._runtime.create_root(
            "init", _request(request), name="init-root"
        )
        summary = await self._runtime.wait_run(run, timeout=10)
        payload = json.loads(summary.response_ref)
        result_ref = payload["result_ref"]
        init_result = json.loads(await self._store.get_text(result_ref))
        spec = EvalSpec(
            primary=MetricDef(
                name=init_result["primary_metric"],
                direction=(
                    "minimize"
                    if init_result["primary_metric"] == "rmse"
                    else "maximize"
                ),
                description=f"task understanding primary metric "
                f"{init_result['primary_metric']}",
            ),
            eval_script=init_result["eval_script"],
        )
        self._eval_specs.freeze(spec)
        self._save_state()

    async def run_search(self, hypothesis: str) -> str:
        """SEARCH 阶段（设计 §5）：Ideator 假设 + CodeAgent 候选 diff 推进阶段。

        前置：PREPARE 已完成或处于 SEARCH 迭代中（预算内可重复）。Ideator
        假设提交 ResearchTree，CodeAgent 生成候选 diff 作为 SOTA，返回其 ref。
        真实 SEARCH 需 Ranker/Policy 完整编排，此处为确定性最小步骤。
        """
        if self._budget.is_exhausted:
            raise RuntimeError("SEARCH budget exhausted")
        if self._phase not in ("PREPARE", "SEARCH"):
            raise RuntimeError(f"SEARCH requires PREPARE, got {self._phase}")
        self._set_phase("SEARCH")
        _, run_id = await self._runtime.create_root(
            "ideator", {"content": hypothesis}, name="ideator-root"
        )
        summary = await self._runtime.wait_run(run_id, timeout=10)
        response = json.loads(summary.response_ref)
        hypothesis_ref = response["result_ref"]
        # 把 Ideator 假设提交到 ResearchTree（设计 §4 所有权）
        hyp_id = self._tree.add_hypothesis(
            Hypothesis(
                statement=hypothesis,
                intervention="Apply intervention",
                expected_effect="Improve the primary metric",
                sources=["ideator"],
            )
        )
        # SEARCH（设计 §5.3）：spawn CodeAgent 为选中假设生成候选 diff
        _, code_run = await self._runtime.create_root(
            "code", {"content": f"implement {hypothesis}"}, name="code-root"
        )
        code_summary = await self._runtime.wait_run(code_run, timeout=10)
        code_response = json.loads(code_summary.response_ref)
        candidate_ref = code_response["result_ref"]
        self._budget.consume(improved=True)  # 确定性骨架：每次假设视为改进
        self._sota_ref = candidate_ref  # SEARCH 完成事实：存在成功 SOTA
        self._save_state()
        return candidate_ref

    async def run_validate(self, experiment_ref: str) -> str:
        """VALIDATE 阶段（§5.4）：对冻结 SOTA 执行确定性验证。

        确定性骨架：spawn CodeAgent 记录验证结果。final-test 恰好一次——
        已提交成功 ``_validation_ref`` 后拒绝重入（端到端 §5.4 不变量）。
        """
        if self._validation_ref is not None:
            raise RuntimeError("VALIDATE already has a final-test result")
        if self._phase != "SEARCH":
            raise RuntimeError(f"VALIDATE requires SEARCH, got {self._phase}")
        self._set_phase("VALIDATE")
        _, run_id = await self._runtime.create_root(
            "code", {"content": f"validate {experiment_ref}"}, name="validate-root"
        )
        summary = await self._runtime.wait_run(run_id, timeout=10)
        response = json.loads(summary.response_ref)
        validation_ref = response["result_ref"]
        self._validation_ref = validation_ref  # VALIDATE 完成事实：final-test 已记录
        self._save_state()
        return validation_ref

    async def run_report(self, report_text: str) -> str:
        """REPORT 阶段（§5.5）：ReportAgent 产出 Bundle → Reflection 评审 → 通过。

        ReportAgent 提交 v1 报告 Bundle；ReflectionAgent 按报告 rubric 评审；
        EvaluationPolicy 判定：passed 接受 v1 并推进 COMPLETED，failed 则
        follow-up 原 ReportAgent 提交修订版 v2。返回接受的报告 Bundle ref。
        """
        if self._phase != "VALIDATE":
            raise RuntimeError(f"REPORT requires VALIDATE, got {self._phase}")
        self._set_phase("REPORT")
        report_id, run1 = await self._runtime.create_root(
            "report", {"content": report_text}, name="report-root"
        )
        summary1 = await self._runtime.wait_run(run1, timeout=10)
        v1 = json.loads(summary1.response_ref)["result_ref"]
        # Reflection 只读评审 v1，产出报告 rubric + score
        _, run2 = await self._runtime.create_root(
            "reflection", _request({"report_ref": v1}), name="report-review-root"
        )
        summary2 = await self._runtime.wait_run(run2, timeout=10)
        review_ref = json.loads(summary2.response_ref)["result_ref"]
        # EvaluationPolicy（确定性）：按 rubric/score 判定
        verdict = await evaluate_data_analysis_review(self._store, review_ref)
        if verdict.passed:
            self._report_ref = v1  # REPORT 完成事实：报告通过门槛
            self._set_phase("COMPLETED")
            return v1
        # failed → follow-up 原 ReportAgent 提交修订版 v2
        run3 = await self._runtime.followup(report_id, {"content": report_text})
        summary3 = await self._runtime.wait_run(run3, timeout=10)
        v2 = json.loads(summary3.response_ref)["result_ref"]
        return v2

    async def open(self, *, message: str | None = None) -> AgentId:
        """创建或恢复 root Supervisor；重开会话从 rollout 恢复记忆。"""
        self._load_state()
        if self._supervisor_id is None:
            self._supervisor_id, _ = await self._runtime.create_root(
                "supervisor", {"content": message or ""}, name="supervisor"
            )
            self._save_state()
        else:
            await self._runtime.resume_agent(
                self._supervisor_id, agent_type="supervisor", name="supervisor"
            )
            if message is not None:
                await self._runtime.followup(self._supervisor_id, {"content": message})
        return self._supervisor_id

    async def message(
        self, content: str, context_refs: list[str] | None = None
    ) -> None:
        """向 root Supervisor 投递消息（不触发 turn），供 App Server 外部调用面。"""
        if self._supervisor_id is None:
            raise RuntimeError("project not opened")
        await self._runtime.send_message(
            self._supervisor_id, content, context_refs or []
        )

    async def human_reply(self, request_id: str, reply: str) -> RunId:
        """提交人工回复并唤醒等待的 Agent（端到端 §4 外部调用面）。"""
        return await self._runtime.human_reply(request_id, reply)

    def pause(self) -> None:
        """项目 pause（§13）：停止派发新 turn，状态投影 PAUSED。"""
        self._runtime.pause()
        self._set_status("PAUSED")

    def resume(self) -> None:
        """项目 resume（§13）：恢复派发，状态投影 RUNNING。"""
        self._runtime.resume()
        self._set_status("RUNNING")

    async def stop(self) -> None:
        """项目 stop（§13）：中断活动 Run，保留已提交事实，投影 CANCELLED。

        停止派发（终态，不可 resume），等待中的 Agent 不删除其 wait/mailbox。
        """
        for snap in self._runtime.list_agents():
            try:
                await self._runtime.interrupt(snap.agent_id, "project stop")
            except (AgentCommandError, RuntimeError):
                # COMPAT: AgentRuntime 门面下 interrupt 对无活动 Run 的 Agent 报错
                # （旧 kernel 为幂等 no-op）；此处与旧语义一致地跳过。
                continue
        self._runtime.pause()
        self._set_status("CANCELLED")

    async def close(self) -> None:
        await self._runtime.aclose()

    def _set_phase(self, phase: str) -> None:
        self._phase = phase
        self._save_state()

    def _set_status(self, status: str) -> None:
        self._status = status
        self._save_state()

    def _save_state(self) -> None:
        """原子、耐久化 JSON 持久化确定性事实（§16.1）。

        只存确定性事实（root id / phase / task / eval_specs / refs / memory /
        budget / tree）；对话经 ``.athena/sessions/{agent_id}.jsonl`` 轻持久化，
        不再写入 store 快照。先写临时文件并 fsync，再 ``os.replace`` 原子替换，
        崩溃不留半写文件。
        """
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "root_supervisor_id": self._supervisor_id,
            "phase": self._phase,
            "status": self._status,
            "task": self._task.model_dump(mode="json") if self._task else None,
            "eval_specs": [
                spec.model_dump(mode="json") for spec in self._eval_specs.versions
            ],
            "sota_ref": self._sota_ref,
            "validation_ref": self._validation_ref,
            "report_ref": self._report_ref,
            "memory": self._memory.to_dict(),
            "budget": self._budget.model_dump(mode="json"),
            "tree": self._tree.to_dict(),
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
        if data.get("task"):
            self._task = TaskMetaData.model_validate(data["task"])
        if data.get("eval_specs"):
            self._eval_specs = EvalSpecChain.from_versions(
                [EvalSpec.model_validate(s) for s in data["eval_specs"]]
            )
        self._sota_ref = data.get("sota_ref")
        self._validation_ref = data.get("validation_ref")
        self._report_ref = data.get("report_ref")
        if data.get("memory"):
            self._memory.load_dict(data["memory"])
        if data.get("budget"):
            self._budget = BudgetSnapshot.model_validate(data["budget"])
        if data.get("tree"):
            self._tree = ResearchTree.from_dict(data["tree"])
