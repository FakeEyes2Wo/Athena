"""PlanExecutor — 执行已通过校验的 Plan（supervisor_design §6）。

Executor 不自行决定下一步，只按序执行 operation 并记录结果。worker 结果是候选
Artifact；只有确定性 evaluator、policy 和 COMMIT_FACTS 可以把候选提升为权威
项目事实。单个 SPAWN_BATCH 内一个 worker 的业务失败作为该 batch 的终态结果
收集，不取消其他 worker，也不把整个 batch 标成技术失败。
"""

import asyncio
import json
from typing import Any, Protocol

from athena.core.contracts import ArtifactRef, ArtifactStore
from athena.research.supervisor.journal import PlanJournal
from athena.research.validation import ValidationService
from athena.research.supervisor.models import (
    ControlStatus,
    HumanRequest,
    LeaseToken,
    OperationStatus,
    OperationType,
    PlanStatus,
    SupervisorOperation,
    SupervisorPlan,
    utc_now,
)
from athena.research.supervisor.state import ProjectStateStore


class RunResult(Protocol):
    """Executor 依赖的最小 Run 终态视图。"""

    status: object
    response_ref: str | None


class WorkerRuntime(Protocol):
    """Executor 依赖的 worker 派发接口（AgentRuntime 已满足）。

    ``wait_run`` 返回 ``Any``：具体 Run 摘要类型由实现方决定，Executor 只通过
    :class:`RunResult` 视图读取 ``status``/``response_ref``。
    """

    async def create_root(
        self, agent_type: str, task: object, *, name: str = "root"
    ) -> tuple[str, str]: ...

    async def wait_run(self, run_id: str, *, timeout: float | None = None) -> Any: ...

    async def followup(self, agent_id: str, task: object) -> str: ...

    async def send_message(
        self,
        agent_id: str,
        content: str,
        context_refs: list[ArtifactRef] | None = None,
        *,
        source: str | None = None,
    ) -> None: ...

    def has_agent(self, agent_id: str) -> bool: ...

    async def resume_agent(
        self, agent_id: str, *, agent_type: str, name: str | None = None
    ) -> None: ...

    def run_summary(self, run_id: str) -> Any | None: ...


def _request(payload: dict[str, object]) -> dict[str, str]:
    return {"content": json.dumps(payload, ensure_ascii=False)}


class PlanExecutor:
    """按序执行 Plan 的 operation；同一 Plan 内 worker 结果跨 op 传递。"""

    def __init__(
        self,
        *,
        journal: PlanJournal,
        state: ProjectStateStore,
        runtime: WorkerRuntime,
        store: ArtifactStore,
        services: Any | None = None,
    ) -> None:
        self._journal = journal
        self._state = state
        self._runtime = runtime
        self._store = store
        self._services = services
        self._lease: LeaseToken | None = None

    async def execute(self, plan: SupervisorPlan, *, lease: LeaseToken) -> None:
        """按序执行 Plan；任一 operation 失败则 fail-fast 结束当前 Plan。

        已 SUCCEEDED 的 operation（崩溃恢复）跳过不重放；写路径全部经
        lease 校验 + 定向更新，幂等。
        """
        self._lease = lease
        try:
            if self._journal.load_plan(plan.plan_id) is None:
                self._journal.save_plan(plan, lease=lease)  # 首次持久化
            self._journal.set_plan_status(plan.plan_id, PlanStatus.RUNNING, lease=lease)
            for operation in plan.operations:
                if operation.status is OperationStatus.SUCCEEDED:
                    continue  # 恢复：已完成的 operation 不重放
                operation.status = OperationStatus.RUNNING
                self._journal.advance_operation(operation, plan.plan_id, lease=lease)
                try:
                    await self._execute_operation(operation, plan)
                    if operation.operation_type is not OperationType.COMMIT_FACTS:
                        # COMMIT_FACTS 已在 complete_operation 内推进状态
                        self._journal.advance_operation(
                            operation,
                            plan.plan_id,
                            lease=lease,
                            status=OperationStatus.SUCCEEDED,
                        )
                except Exception as exc:  # 业务失败 → 当前 Plan fail-fast
                    operation.error = f"{type(exc).__name__}: {exc}"
                    operation.status = OperationStatus.FAILED
                    plan.status = PlanStatus.FAILED
                    plan.completed_at = utc_now()
                    self._journal.advance_operation(
                        operation, plan.plan_id, lease=lease, error=operation.error
                    )
                    self._journal.set_plan_status(
                        plan.plan_id,
                        PlanStatus.FAILED,
                        lease=lease,
                        completed_at=plan.completed_at,
                    )
                    return
            plan.status = PlanStatus.COMPLETED
            plan.completed_at = utc_now()
            self._journal.set_plan_status(
                plan.plan_id,
                PlanStatus.COMPLETED,
                lease=lease,
                completed_at=plan.completed_at,
            )
        finally:
            self._lease = None

    def _lease_required(self) -> LeaseToken:
        """当前 execute 绑定的写锁（仅 execute 内可用）。"""
        if self._lease is None:
            raise RuntimeError(
                "executor requires a lease; call execute(plan, lease=...)"
            )
        return self._lease

    async def _execute_operation(
        self,
        operation: SupervisorOperation,
        plan: SupervisorPlan,
    ) -> None:
        handler = {
            OperationType.SPAWN_BATCH: self._spawn_batch,
            OperationType.FOLLOWUP_AGENT: self._followup_agent,
            OperationType.WAIT_AGENTS: self._wait_agents,
            OperationType.RUN_SERVICE: self._run_service,
            OperationType.COMMIT_FACTS: self._commit_facts,
            OperationType.SET_EXECUTION_STATUS: self._set_execution_status,
            OperationType.REQUEST_HUMAN: self._request_human,
            OperationType.RESERVE_FINAL_TEST: self._reserve_final_test,
            OperationType.ADVANCE_FINAL_TEST: self._advance_final_test,
        }.get(operation.operation_type)
        if handler is None:
            raise RuntimeError(
                f"unsupported operation type: {operation.operation_type}"
            )
        await handler(operation, plan)

    # ---- operation 实现 ----

    async def _spawn_batch(
        self,
        operation: SupervisorOperation,
        plan: SupervisorPlan,
    ) -> None:
        """一次派发一个或多个 worker；稳定 dispatch key + 崩溃后对账。"""
        spawns = operation.inputs.get("spawns")
        if not isinstance(spawns, list):
            raise ValueError("SPAWN_BATCH inputs require a 'spawns' list")
        lease = self._lease_required()
        dispatch: dict[str, object] = {}
        for spec in spawns:
            key = str(spec.get("key", ""))
            if not key:
                raise ValueError("each spawn requires a stable 'key'")
            agent_type = str(spec["agent_type"])
            payload = spec.get("payload", {})
            if not isinstance(payload, dict):
                raise ValueError("spawn payload must be a dict")
            # 允许 payload 值引用同 Plan 前序 op 已提交的事实（如 ingest 的
            # dataset_managed_root）：执行时解析为具体值，worker 收到确定路径。
            payload = {
                str(key): await self._resolve_fact_value(plan, value)
                for key, value in payload.items()
            }
            dispatch_key = f"{plan.plan_id}:{key}"
            payload_ref = await self._store.put_text(
                json.dumps(payload, ensure_ascii=False)
            )
            record = self._journal.reserve_dispatch(
                dispatch_key, agent_type, payload_ref, lease=lease
            )
            agent_id = record.get("agent_id")
            run_id = record.get("run_id")
            if isinstance(agent_id, str) and isinstance(run_id, str):
                # 恢复：认领已派发的 agent，不再重新 spawn
                if not self._runtime.has_agent(agent_id):
                    await self._runtime.resume_agent(
                        agent_id, agent_type=agent_type, name=f"{agent_type}-{key}"
                    )
            else:
                agent_id, run_id = await self._runtime.create_root(
                    agent_type, _request(payload), name=f"{agent_type}-{key}"
                )
                self._journal.complete_dispatch(
                    dispatch_key, agent_id, run_id, lease=lease
                )
            dispatch[key] = {"agent_id": agent_id, "run_id": run_id}
        operation.agent_id = None
        operation.run_id = None
        operation.outputs["dispatch"] = dispatch

    async def _followup_agent(
        self,
        operation: SupervisorOperation,
        plan: SupervisorPlan,
    ) -> None:
        """只延续已知原实例，必须携带修订原因与失败证据 refs。

        输出 ``followups = {key: {"run_id", "agent_id"}}``，供 WAIT_AGENTS
        按 key 等待该 follow-up run 并提取 result_ref。
        """
        agent_id = operation.inputs.get("agent_id")
        if not isinstance(agent_id, str):
            raise ValueError("FOLLOWUP_AGENT requires 'agent_id'")
        reason = operation.inputs.get("reason", "")
        context_refs = operation.inputs.get("context_refs", [])
        if not isinstance(context_refs, list):
            raise ValueError("FOLLOWUP_AGENT context_refs must be a list")
        key = str(operation.inputs.get("key", "agent"))
        payload = {"content": str(reason), "context_refs": context_refs}
        await self._runtime.send_message(
            agent_id, str(reason), context_refs, source="supervisor"
        )
        run_id = await self._runtime.followup(agent_id, payload)
        operation.agent_id = agent_id
        operation.run_id = run_id
        operation.outputs["run_id"] = run_id
        operation.outputs["followups"] = {key: {"run_id": run_id, "agent_id": agent_id}}

    async def _run_service(
        self,
        operation: SupervisorOperation,
        plan: SupervisorPlan,
    ) -> None:
        """调用静态注册的确定性领域服务，记录结果并原子提交服务 facts。

        服务只接受白名单静态名称（validator 已校验）；结果 refs/evidence 写入
        operation outputs，``ServiceResult.facts`` 经 complete_operation 幂等提交
        （单事务，与 COMMIT_FACTS 相同语义）。
        """
        if self._services is None:
            raise RuntimeError("RUN_SERVICE requires a services registry")
        service = str(operation.inputs["service"])
        request = operation.inputs.get("request", {})
        if not isinstance(request, dict):
            raise ValueError("RUN_SERVICE request must be a dict")
        # 请求字段可引用前序 worker 结果（{from_op, worker, field}），
        # 让 Planner 把 LLM 产物动态喂给确定性服务（如候选评估、假设入图）。
        request = {
            str(key): await self._resolve_fact_value(plan, value)
            for key, value in request.items()
        }
        result = await self._services.run(service, request)
        operation.outputs["service"] = service
        operation.outputs["result_refs"] = [r for r in result.result_refs]
        operation.outputs["evidence_refs"] = [r for r in result.evidence_refs]
        if result.facts:
            operation.outputs["committed"] = result.facts
            # 搜索预算按"轮"消费一次：仅 freeze 服务 op 显式标记（design §2.5，
            # "一轮完成并冻结 RankingRound 时只更新一次"）；缺省回退 reason_code
            # 以兼容手工构造的 SEARCH_ROUND 测试 Plan。
            search_experiment = operation.inputs.get(
                "search_experiment", plan.reason_code == "SEARCH_ROUND"
            )
            self._journal.complete_operation(
                operation,
                plan.plan_id,
                lease=self._lease_required(),
                facts=result.facts,
                search_experiment=search_experiment,
            )

    async def _wait_agents(
        self,
        operation: SupervisorOperation,
        plan: SupervisorPlan,
    ) -> None:
        """等待已派发 worker 完成，收集每个 worker 的 result_ref。"""
        from_op = str(operation.inputs["from_op"])
        keys = operation.inputs.get("keys")
        if not isinstance(keys, list):
            raise ValueError("WAIT_AGENTS requires a 'keys' list")
        outputs = self._outputs_of(plan, from_op)
        dispatch = outputs.get("dispatch", {})
        followups = outputs.get("followups", {})
        results: dict[str, object] = {}
        tasks = []
        for key in keys:
            key_str = str(key)
            entry = dispatch.get(key_str) if isinstance(dispatch, dict) else None
            if entry is None and isinstance(followups, dict):
                entry = followups.get(key_str)
            if not isinstance(entry, dict) or "run_id" not in entry:
                raise RuntimeError(f"no dispatch/followup entry for worker {key}")
            tasks.append((key_str, str(entry["run_id"])))

        async def wait_one(key: str, run_id: str) -> tuple[str, object]:
            summary = await self._runtime.wait_run(run_id)
            result_ref = self._extract_result_ref(summary)
            return key, result_ref

        for key, ref in await asyncio.gather(
            *(wait_one(key, run_id) for key, run_id in tasks)
        ):
            results[key] = ref
        operation.outputs["results"] = results

    @staticmethod
    def _outputs_of(plan: SupervisorPlan, operation_id: str) -> dict[str, object]:
        """按 operation_id 取同 Plan 内前序 op 的 outputs（worker 结果跨 op 传递）。"""
        for operation in plan.operations:
            if operation.operation_id == operation_id:
                return operation.outputs
        return {}

    def _extract_result_ref(self, summary: RunResult) -> ArtifactRef:
        """从 Run 终态解出 worker 的 result_ref；失败或缺失视为业务失败。"""
        status = getattr(summary, "status", None)
        status_value = getattr(status, "value", None)
        if status_value not in (None, "completed"):
            raise RuntimeError(f"worker run not completed: {status_value}")
        response_ref = summary.response_ref
        if not response_ref:
            raise RuntimeError("worker run produced no response")
        payload = json.loads(response_ref)
        result_ref = payload.get("result_ref")
        if not isinstance(result_ref, str):
            raise RuntimeError("worker response has no result_ref")
        return result_ref

    async def _commit_facts(
        self,
        operation: SupervisorOperation,
        plan: SupervisorPlan,
    ) -> None:
        """提交白名单 fact key；幂等，单事务内完成并消费搜索预算。"""
        facts = operation.inputs.get("facts")
        if not isinstance(facts, dict):
            raise ValueError("COMMIT_FACTS requires a 'facts' dict")
        resolved: dict[str, object] = {}
        for key, spec in facts.items():
            resolved[key] = await self._resolve_fact_value(plan, spec)
        operation.outputs["committed"] = resolved  # 事务内持久化
        next_version = self._journal.complete_operation(
            operation,
            plan.plan_id,
            lease=self._lease_required(),
            facts=resolved,
            search_experiment=plan.reason_code == "SEARCH_ROUND",
        )
        operation.outputs["state_version"] = next_version

    async def _resolve_fact_value(self, plan: SupervisorPlan, spec: object) -> object:
        """解析事实值：字面量或引用。

        ``{"artifact": fact_key, "field": ..., "default": ...}`` → 读事实（ArtifactRef），
        读 artifact 并提取字段；任何一步失败/缺失 → 返回 ``default``。
        ``{"from_op", "worker"}`` → 整个 worker result ref；
        加 ``"extract"`` → 字段存为文本 artifact 返回其 ref；
        加 ``"field"`` → 字段原值直接作为事实值（如评审 verdict）；
        ``{"from_op", "dispatch", "field"}`` → 读 spawn op 的 dispatch 身份（如 agent_id）。
        ``{"from_op", "service_result": idx}`` → 读前序 RUN_SERVICE op 的 result_refs[idx]
        内容；加 ``"raw"`` → 返回 ref 本身（如把冻结 bundle ref 提交为事实）；
        加 ``"index"`` → 内容为列表时取第 index 项（如评估批次 [0]）；
        加 ``"field"`` → 返回内容字段值（如把评估结果列表喂给 freeze 服务）。
        """
        if isinstance(spec, dict) and "artifact" in spec:
            raw = self._journal.get_fact(str(spec["artifact"]))
            default = spec.get("default")
            if not isinstance(raw, str):
                return default
            try:
                payload = json.loads(await self._store.get_text(raw))
            except Exception:
                return default
            field = spec.get("field")
            if field is None:
                return payload
            value = payload.get(str(field))
            return value if value is not None else default
        if isinstance(spec, dict) and "service_result" in spec:
            # {"from_op", "service_result": idx} → 读前序 RUN_SERVICE op 的 result_ref。
            from_op = str(spec.get("from_op", ""))
            outputs = self._outputs_of(plan, from_op)
            refs = outputs.get("result_refs", [])
            index = int(spec.get("service_result", 0))
            if not isinstance(refs, list) or index >= len(refs):
                raise RuntimeError(
                    f"service op {from_op} produced no result_refs[{index}]"
                )
            ref = str(refs[index])
            if spec.get("raw"):
                return ref
            payload = json.loads(await self._store.get_text(ref))
            if "index" in spec:
                payload = payload[int(spec["index"])]
            field = spec.get("field")
            if field is None:
                return payload
            value = payload.get(str(field))
            if value is None:
                raise RuntimeError(f"service result has no field {field!r}")
            return value
        if isinstance(spec, dict) and "fact" in spec:
            # {"fact": key} → 读取 Journal 事实原值（如 final_test_attempt_id）。
            value = self._journal.get_fact(str(spec["fact"]))
            if value is None:
                raise RuntimeError(f"fact {spec['fact']!r} not set")
            return value
        if not isinstance(spec, dict) or "from_op" not in spec:
            return spec
        from_op = str(spec["from_op"])
        outputs = self._outputs_of(plan, from_op)
        dispatch_key = spec.get("dispatch")
        if dispatch_key is not None:
            dispatch = outputs.get("dispatch", {})
            entry = (
                dispatch.get(str(dispatch_key)) if isinstance(dispatch, dict) else None
            )
            value = (
                entry.get(str(spec.get("field", "")))
                if isinstance(entry, dict)
                else None
            )
            if not isinstance(value, str):
                raise RuntimeError(
                    f"dispatch {dispatch_key!r} has no field {spec.get('field')!r}"
                )
            return value
        worker = str(spec.get("worker", ""))
        results = outputs.get("results", {})
        if not isinstance(results, dict) or worker not in results:
            raise RuntimeError(f"no worker result {worker!r} from op {from_op}")
        result_ref = str(results[worker])
        raw_field = spec.get("field")
        if raw_field is not None:
            payload = json.loads(await self._store.get_text(result_ref))
            value = payload.get(str(raw_field))
            if value is None:
                raise RuntimeError(f"worker result has no field {raw_field!r}")
            return value
        extract = spec.get("extract")
        if extract is None:
            return result_ref
        payload = json.loads(await self._store.get_text(result_ref))
        field = payload.get(str(extract))
        if not isinstance(field, str):
            raise RuntimeError(f"worker result has no field {extract!r}")
        return await self._store.put_text(field)

    async def _set_execution_status(
        self,
        operation: SupervisorOperation,
        plan: SupervisorPlan,
    ) -> None:
        """只改变 execution 控制状态，不能直接修改研究阶段。"""
        status = ControlStatus(str(operation.inputs["status"]))
        self._state.set_control_status(status)
        operation.outputs["status"] = status.value

    async def _request_human(
        self,
        operation: SupervisorOperation,
        plan: SupervisorPlan,
    ) -> None:
        """创建项目级持久化 HumanRequest，并投影 WAITING_FOR_HUMAN。"""
        inputs = operation.inputs
        context_refs = inputs.get("context_refs", [])
        options = inputs.get("options", [])
        if not isinstance(context_refs, list) or not isinstance(options, list):
            raise ValueError("REQUEST_HUMAN context_refs/options must be lists")
        response_schema = inputs.get("response_schema")
        default_answer = inputs.get("default_answer")
        requesting_agent_id = inputs.get("requesting_agent_id")
        request = HumanRequest(
            request_id=str(inputs["request_id"]),
            execution_id=plan.execution_id,
            plan_id=plan.plan_id,
            reason_code=str(inputs.get("reason_code", "")),
            question=str(inputs["question"]),
            context_refs=list(context_refs),
            input_mode=str(inputs.get("input_mode", "single_select")),
            options=list(options),
            allow_free_text=bool(inputs.get("allow_free_text", False)),
            response_schema=(
                response_schema if isinstance(response_schema, dict) else None
            ),
            default_answer=default_answer if isinstance(default_answer, str) else None,
            requesting_agent_id=(
                requesting_agent_id if isinstance(requesting_agent_id, str) else None
            ),
            created_at=utc_now(),
        )
        self._journal.save_human_request(request)
        operation.outputs["request_id"] = request.request_id

    async def _reserve_final_test(
        self,
        operation: SupervisorOperation,
        plan: SupervisorPlan,
    ) -> None:
        """预留唯一 final-test attempt（lease-fenced，冻结 SOTA/EvalSpec/dataset refs）。

        把 attempt_id 经 ``complete_operation`` 原子提交为 ``final_test_attempt_id``
        事实，供后续 ADVANCE_FINAL_TEST op 经 ``{"fact": ...}`` 解析复用。
        """
        lease = self._lease_required()
        attempt = self._journal.reserve_final_test(
            execution_id=plan.execution_id,
            sota_experiment_id=str(operation.inputs["sota_experiment_id"]),
            sota_ref=str(operation.inputs["sota_ref"]),
            eval_spec_ref=str(operation.inputs["eval_spec_ref"]),
            dataset_ref=str(operation.inputs["dataset_ref"]),
            final_test_ref=str(operation.inputs["final_test_ref"]),
            lease=lease,
        )
        operation.outputs["attempt_id"] = attempt.attempt_id
        operation.outputs["committed"] = {"final_test_attempt_id": attempt.attempt_id}
        self._journal.complete_operation(
            operation,
            plan.plan_id,
            lease=lease,
            facts={"final_test_attempt_id": attempt.attempt_id},
        )

    async def _advance_final_test(
        self,
        operation: SupervisorOperation,
        plan: SupervisorPlan,
    ) -> None:
        """按前向状态机推进 final-test（lease-fenced CAS）。

        ``attempt_id`` 经 ``{"fact": "final_test_attempt_id"}`` 解析；
        ``prediction_ref``/``score_ref``/``final_test_score`` 支持引用解析
        （worker 结果字段或 artifact 字段）；已有非空 score 由 journal 拒绝覆盖。
        """
        lease = self._lease_required()
        attempt_id = await self._resolve_fact_value(
            plan, operation.inputs["attempt_id"]
        )
        expected_status = str(operation.inputs["expected_status"])
        kwargs: dict[str, object] = {}
        for key in ("prediction_ref", "score_ref", "test_score", "final_test_score"):
            if key in operation.inputs:
                kwargs[key] = await self._resolve_fact_value(
                    plan, operation.inputs[key]
                )
        # 确定性 fallback：candidates 列表 → [0]["test_score"]（真实 evaluator 接线前的占位）
        if (
            "final_test_score" in kwargs
            and isinstance(kwargs["final_test_score"], list)
            and kwargs["final_test_score"]
        ):
            kwargs["final_test_score"] = kwargs["final_test_score"][0].get("test_score")
        attempt = self._journal.advance_final_test(
            attempt_id, expected_status, lease=lease, **kwargs
        )
        operation.outputs["attempt_status"] = attempt.status
        operation.outputs["final_test_score"] = attempt.final_test_score
        if attempt.status == "COMMITTED":
            # 计算泛化差距并持久化 ValidationResult（方向从 eval_spec artifact 读）。
            direction = await self._eval_direction(plan)
            result = ValidationService().build_result(
                test_score=attempt.test_score or 0.0,
                final_test_score=attempt.final_test_score or 0.0,
                direction=direction,
            )
            result_ref = await self._store.put_text(result.model_dump_json())
            self._journal.complete_operation(
                operation,
                plan.plan_id,
                lease=lease,
                facts={
                    "final_test_attempt_ref": attempt.attempt_id,
                    "final_test_score": attempt.final_test_score,
                    "validation_result_ref": result_ref,
                },
            )
            operation.outputs["validation_result_ref"] = result_ref

    async def _eval_direction(self, plan: SupervisorPlan) -> str:
        """读取 EvalSpec 指标方向（default maximize）。"""
        ref = self._journal.get_fact("eval_spec_ref")
        if isinstance(ref, str):
            try:
                spec = json.loads(await self._store.get_text(ref))
                direction = spec.get("direction")
                if isinstance(direction, str):
                    return direction
            except Exception:
                pass
        return "maximize"
