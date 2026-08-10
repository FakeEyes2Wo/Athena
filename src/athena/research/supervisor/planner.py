"""DeterministicSupervisorPlanner — 显式状态机（supervisor_design §5/§6）。

根据缺失事实选择下一步，而不是把整个阶段展开成一份计划。每份 Plan 只覆盖一个
协调步骤，可批量并行多个 worker。首版为确定性实现：不调用 LLM，只依据已提交
事实投影。未来若引入 LLM 决策，只替换本组件。
"""

from athena.core.contracts import new_id
from athena.research.supervisor.journal import PlanJournal
from athena.research.supervisor.models import (
    ControlStatus,
    OperationType,
    ResearchPhase,
    SupervisorOperation,
    SupervisorPlan,
    utc_now,
)
from athena.research.supervisor.state import (
    Budget,
    ProjectFacts,
    ProjectStateStore,
    project_phase,
)

_PREPARE_INGEST = "PREPARE_INGEST"
_PREPARE_ROLE_REVIEW = "PREPARE_ROLE_REVIEW"
_PREPARE_ROLE_REVIEW_ACCEPT = "PREPARE_ROLE_REVIEW_ACCEPT"
_PREPARE_ROLE_REVIEW_REVISE = "PREPARE_ROLE_REVIEW_REVISE"
_PREPARE_ROLE_REVIEW_RESOLUTION = "PREPARE_ROLE_REVIEW_RESOLUTION"
_PREPARE_EVAL = "PREPARE_EVAL"
_PREPARE_EDA = "PREPARE_EDA"
_PREPARE_EDA_REVIEW = "PREPARE_EDA_REVIEW"
_PREPARE_EDA_REVIEW_ACCEPT = "PREPARE_EDA_REVIEW_ACCEPT"
_PREPARE_EDA_REVIEW_REVISE = "PREPARE_EDA_REVIEW_REVISE"
_PREPARE_EDA_REVIEW_RESOLUTION = "PREPARE_EDA_REVIEW_RESOLUTION"
_PREPARE_BASELINE = "PREPARE_BASELINE"
_SEARCH_ROUND = "SEARCH_ROUND"
_SEARCH_STOP = "SEARCH_STOP"
_SEARCH_NO_SOTA = "SEARCH_NO_SOTA"
_VALIDATE_FINAL_TEST = "VALIDATE_FINAL_TEST"

# 修订预算：Reflection REVISE 后 follow-up 原 worker 的次数上限（ExecutionConfig
# max_prepare_revisions 默认 2；确定性 Planner 不解析 Artifact，用同值常量）。
_MAX_PREPARE_REVISIONS = 2

# 每类独立评审的权威事实与协调常量。Planner 只读 journal facts 分支，不解析
# Artifact 正文；verdict/draft/revision 由评审 Plan 的 COMMIT_FACTS 写入。
_REVIEWS = {
    "role": {
        "source_fact": "dataset_role_proposal_ref",
        "gate_fact": "dataset_role_review_ref",
        "draft_fact": "role_review_draft_ref",
        "verdict_fact": "role_review_verdict",
        "revision_fact": "role_review_revision",
        "agent_fact": "data_agent_id",
        "prompt": "review the dataset role proposal",
        "request_id": "data_role_resolution",
        "question": "角色评审修订预算已耗尽。请决定：接受当前 proposal，或提交角色修正。",
        "resolution_fact": "role_resolution",
        "failure_reason": "DATA_ROLE_REVIEW_EXHAUSTED",
        "review_reason": _PREPARE_ROLE_REVIEW,
        "accept_reason": _PREPARE_ROLE_REVIEW_ACCEPT,
        "revise_reason": _PREPARE_ROLE_REVIEW_REVISE,
        "resolution_reason": _PREPARE_ROLE_REVIEW_RESOLUTION,
        "revise_message": "role proposal 未通过评审；按评审意见修订后重新提交 proposal。",
    },
    "eda": {
        "source_fact": "eda_report_ref",
        "gate_fact": "eda_review_ref",
        "draft_fact": "eda_review_draft_ref",
        "verdict_fact": "eda_review_verdict",
        "revision_fact": "eda_review_revision",
        "agent_fact": "eda_agent_id",
        "prompt": "review the eda report against the rubric",
        "request_id": "eda_review_resolution",
        "question": "EDA 评审修订预算已耗尽。请决定：接受当前 EDA 报告，或提交修正意见。",
        "resolution_fact": "eda_resolution",
        "failure_reason": "EDA_REVIEW_EXHAUSTED",
        "review_reason": _PREPARE_EDA_REVIEW,
        "accept_reason": _PREPARE_EDA_REVIEW_ACCEPT,
        "revise_reason": _PREPARE_EDA_REVIEW_REVISE,
        "resolution_reason": _PREPARE_EDA_REVIEW_RESOLUTION,
        "revise_message": "EDA 报告未通过评审；按评审意见修订后重新提交 EDA。",
    },
}


class DeterministicSupervisorPlanner:
    """根据权威事实投影下一步一步式 Plan；无事可做返回 None。"""

    def __init__(self, journal: PlanJournal, state: ProjectStateStore) -> None:
        self._journal = journal
        self._state = state

    def next_plan(
        self,
        execution_id: str,
        facts: ProjectFacts,
        budget: Budget,
        control_status: ControlStatus,
    ) -> SupervisorPlan | None:
        """计算并返回下一步 Plan；控制状态非 RUNNING 或项目完成时返回 None。

        阶段由 project_phase 从事实投影；只读，不写入 Journal；返回的 Plan 由
        Validator 校验后交给 Executor。
        """
        if control_status is not ControlStatus.RUNNING:
            return None
        if facts.task_ref is None:
            return None  # IDLE：等 TASK_CONFIGURE 提交 task 事实
        if not budget.can_plan():
            return None

        phase = project_phase(facts)
        if phase is ResearchPhase.PREPARE:
            # ingest → role review(ACCEPT/REVISE 闭环) → eval spec → EDA → EDA review 闭环 → baseline
            if not facts.dataset_role_proposal_ref or not facts.dataset_manifest_ref:
                return self._prepare_ingest_plan(execution_id)
            if not facts.dataset_role_review_ref:
                plan = self._review_kind(execution_id, "role")
                if plan is not None:
                    return plan
                return None  # role review 未接受（阻塞等待人工决议）→ 不推进
            if not facts.eval_spec_ref:
                return self._prepare_eval_plan(execution_id)
            if not facts.eda_report_ref:
                return self._prepare_eda_plan(execution_id)
            if not facts.eda_review_ref:
                plan = self._review_kind(execution_id, "eda")
                if plan is not None:
                    return plan
                return None  # eda review 未接受（阻塞等待人工决议）→ 不推进
            return self._prepare_baseline_plan(execution_id)
        if phase is ResearchPhase.SEARCH:
            # 搜索已停止但从未产生 SOTA → 无法进入 VALIDATE，直接 FAILED（不循环 SEARCH）。
            if facts.search_stop_ref is not None and facts.sota_experiment_ref is None:
                return self._search_no_sota_plan(execution_id)
            # 首版 Code/Ideator 只产生一组确定性候选；首轮已选出 SOTA 后直接收尾。
            if facts.sota_experiment_ref is not None and facts.search_stop_ref is None:
                return self._search_stop_plan(execution_id)
            if budget.search_exhausted and facts.search_stop_ref is None:
                return self._search_stop_plan(execution_id)
            return self._search_round_plan(execution_id)
        if phase is ResearchPhase.VALIDATE:
            return self._validate_plan(execution_id)
        return None  # COMPLETED / 无可推进

    # ---- 各阶段 Plan 构造：SPAWN → WAIT → COMMIT ----

    def _prepare_ingest_plan(self, execution_id: str) -> SupervisorPlan:
        """PREPARE 第一步：确定性 ingest（dataset.ingest）+ DataAgent 角色提议。

        ``dataset.ingest`` 用 RUN_SERVICE 复制源字节并提交 ``dataset_manifest_ref``；
        DataAgent 产出自角色提议；``data_agent_id`` 从 dispatch 身份读取，供
        REVISE 用 FOLLOWUP_AGENT 交回同一 DataAgent（同 thread/workspace/reader
        lineage）。
        """
        task_config = self._task_config()
        data_path = str(task_config.get("data_payload", {}).get("data_path", ""))
        ingest = self._op(
            "service:dataset.ingest",
            OperationType.RUN_SERVICE,
            {
                "service": "dataset.ingest",
                "request": {"source_root": data_path},
            },
        )
        # DataAgent 读已摄取受管 raw snapshot（同 Plan 内 ingest 先提交
        # dataset_managed_root 事实），不消费原始源目录。
        target = str(task_config.get("data_payload", {}).get("target", ""))
        spawn = self._op(
            "spawn:data",
            OperationType.SPAWN_BATCH,
            {
                "spawns": [
                    {
                        "key": "data",
                        "agent_type": "data",
                        "payload": {
                            "kind": "role",
                            "data_path": {"fact": "dataset_managed_root"},
                            "target": target,
                        },
                    }
                ],
            },
        )
        wait = self._op(
            "wait:data",
            OperationType.WAIT_AGENTS,
            {
                "from_op": spawn.operation_id,
                "keys": ["data"],
            },
        )
        commit = self._op(
            "commit:ingest",
            OperationType.COMMIT_FACTS,
            {
                "facts": {
                    "dataset_role_proposal_ref": {
                        "from_op": wait.operation_id,
                        "worker": "data",
                    },
                    "data_agent_id": {
                        "from_op": spawn.operation_id,
                        "dispatch": "data",
                        "field": "agent_id",
                    },
                },
                "reason": _PREPARE_INGEST,
            },
        )
        return self._plan(execution_id, _PREPARE_INGEST, [ingest, spawn, wait, commit])

    def _prepare_eval_plan(self, execution_id: str) -> SupervisorPlan:
        """PREPARE 第三步：InitAgent 产出 EvalSpec + 冻结 eval bundle。

        InitAgent 产 ``eval_script``（协议事实）并指向其生成的 eval 工作区
        （``eval_workspace``/``eval_metadata``）；``scripts.freeze`` 把该工作区
        ``uv lock`` 冻结为 ``DataScriptBundle``，commit ``eval_bundle_ref`` 供
        SEARCH/VALIDATE 的可信 evaluator 消费（真实 frozen bundle，非 worker 结果）。
        """
        spawn = self._op(
            "spawn:eval",
            OperationType.SPAWN_BATCH,
            {
                "spawns": [
                    {
                        "key": "init",
                        "agent_type": "init",
                        "payload": self._data_aware_payload("init_payload"),
                    }
                ],
            },
        )
        wait = self._op(
            "wait:eval",
            OperationType.WAIT_AGENTS,
            {
                "from_op": spawn.operation_id,
                "keys": ["init"],
            },
        )
        freeze = self._op(
            "service:eval.freeze",
            OperationType.RUN_SERVICE,
            {
                "service": "scripts.freeze",
                "request": {
                    "workspace": {
                        "from_op": wait.operation_id,
                        "worker": "init",
                        "field": "eval_workspace",
                    },
                    "metadata": {
                        "from_op": wait.operation_id,
                        "worker": "init",
                        "field": "eval_metadata",
                    },
                },
            },
        )
        commit = self._op(
            "commit:eval",
            OperationType.COMMIT_FACTS,
            {
                "facts": {
                    "eval_spec_ref": {
                        "from_op": wait.operation_id,
                        "worker": "init",
                        "field": "eval_script",
                    },
                    "eval_bundle_ref": {
                        "from_op": freeze.operation_id,
                        "service_result": 0,
                        "raw": True,
                    },
                },
                "reason": _PREPARE_EVAL,
            },
        )
        return self._plan(execution_id, _PREPARE_EVAL, [spawn, wait, freeze, commit])

    def _prepare_eda_plan(self, execution_id: str) -> SupervisorPlan:
        """PREPARE 第四步：DataAgent 完整读取产出 EDA，commit eda_report_ref。

        ``eda_agent_id`` 供 EDA 评审 REVISE 时 follow-up 同一 DataAgent。
        """
        spawn = self._op(
            "spawn:eda",
            OperationType.SPAWN_BATCH,
            {
                "spawns": [
                    {
                        "key": "data",
                        "agent_type": "data",
                        "payload": {
                            **self._data_aware_payload("data_payload"),
                            "kind": "eda",
                        },
                    }
                ],
            },
        )
        wait = self._op(
            "wait:eda",
            OperationType.WAIT_AGENTS,
            {
                "from_op": spawn.operation_id,
                "keys": ["data"],
            },
        )
        commit = self._op(
            "commit:eda",
            OperationType.COMMIT_FACTS,
            {
                "facts": {
                    "eda_report_ref": {"from_op": wait.operation_id, "worker": "data"},
                    "eda_agent_id": {
                        "from_op": spawn.operation_id,
                        "dispatch": "data",
                        "field": "agent_id",
                    },
                },
                "reason": _PREPARE_EDA,
            },
        )
        return self._plan(execution_id, _PREPARE_EDA, [spawn, wait, commit])

    # ---- Reflection 独立评审：ACCEPT / REVISE 闭环 ----

    def _review_kind(self, execution_id: str, kind: str) -> SupervisorPlan | None:
        """驱动一类评审：评审 → ACCEPT 提升 / REVISE 同 Agent 修订 / 预算耗尽。

        只有被接受（ACCEPT 或有效 Human 决议）才提交 gate fact；REVISE 不产生
        gate fact，因此 project_phase 在通过前不会推进阶段。Planner 只读 journal
        facts 分支，不解析 Artifact 正文。
        """
        cfg = _REVIEWS[kind]
        if getattr(self._state.facts(), cfg["gate_fact"]) is not None:
            return None  # 已接受 → 该评审完成
        verdict = self._journal.get_fact(cfg["verdict_fact"])
        if verdict is None:
            return self._review_plan(execution_id, kind)
        if verdict == "ACCEPT":
            return self._review_accept_plan(execution_id, kind)
        revision = int(self._journal.get_fact(cfg["revision_fact"]) or 0)
        if revision < self._prepare_revision_limit():
            return self._review_revise_plan(execution_id, kind, revision)
        return self._review_resolution_plan(execution_id, kind)

    def _prepare_revision_limit(self) -> int:
        """修订上限：读 TASK_CONFIGURE 持久化的 ExecutionConfig 值；缺省 2。"""
        value = self._journal.get_fact("prepare_revision_limit")
        return int(value) if isinstance(value, (int, float)) else _MAX_PREPARE_REVISIONS

    def _review_plan(self, execution_id: str, kind: str) -> SupervisorPlan:
        """Reflection 独立评审：产出 draft（评审 Artifact）+ verdict（原值事实）。

        首次评审与每次修订后重评审都用相同 reason_code，但 idempotency key 按
        修订次数区分（v0/v1/…），避免重评审的 COMMIT_FACTS 被幂等重放跳过。
        """
        cfg = _REVIEWS[kind]
        source = self._journal.get_fact(cfg["source_fact"])
        suffix = f"v{int(self._journal.get_fact(cfg['revision_fact']) or 0)}"
        spawn = self._op(
            f"spawn:{kind}_review_{suffix}",
            OperationType.SPAWN_BATCH,
            {
                "spawns": [
                    {
                        "key": "reflection",
                        "agent_type": "reflection",
                        "payload": {
                            "content": cfg["prompt"],
                            "data_analysis_ref": source,
                            "kind": kind,
                        },
                    }
                ],
            },
        )
        wait = self._op(
            f"wait:{kind}_review_{suffix}",
            OperationType.WAIT_AGENTS,
            {
                "from_op": spawn.operation_id,
                "keys": ["reflection"],
            },
        )
        commit = self._op(
            f"commit:{kind}_review_{suffix}",
            OperationType.COMMIT_FACTS,
            {
                "facts": {
                    cfg["draft_fact"]: {
                        "from_op": wait.operation_id,
                        "worker": "reflection",
                    },
                    cfg["verdict_fact"]: {
                        "from_op": wait.operation_id,
                        "worker": "reflection",
                        "field": "decision",
                    },
                },
                "reason": cfg["review_reason"],
            },
        )
        return self._plan(execution_id, cfg["review_reason"], [spawn, wait, commit])

    def _review_accept_plan(
        self, execution_id: str, kind: str, *, human_resolution: bool = False
    ) -> SupervisorPlan:
        """ACCEPT → 把最新评审提升为 gate fact（dataset_role_review_ref 等）。"""
        cfg = _REVIEWS[kind]
        draft = self._journal.get_fact(cfg["draft_fact"])
        if not isinstance(draft, str):
            raise RuntimeError(f"no {kind} review draft to accept")
        facts: dict[str, object] = {cfg["gate_fact"]: draft, cfg["verdict_fact"]: None}
        if human_resolution:
            facts[cfg["resolution_fact"]] = "ACCEPT_CURRENT_PROPOSAL"
        commit = self._op(
            f"commit:{cfg['gate_fact']}",
            OperationType.COMMIT_FACTS,
            {
                "facts": facts,
                "reason": cfg["accept_reason"],
            },
        )
        return self._plan(execution_id, cfg["accept_reason"], [commit])

    def _review_revise_plan(
        self, execution_id: str, kind: str, revision: int, *, human: bool = False
    ) -> SupervisorPlan:
        """REVISE → FOLLOWUP_AGENT 原 worker 修订源事实，随后重新评审。

        同一 agent/thread/workspace 延续；自动修订消费 revision 预算，人工修正
        不消费（human=True）。修订后清除 verdict 触发下一次评审。
        """
        cfg = _REVIEWS[kind]
        agent_id = self._journal.get_fact(cfg["agent_fact"])
        if not isinstance(agent_id, str):
            raise RuntimeError(f"no {kind} agent id to follow up")
        draft = self._journal.get_fact(cfg["draft_fact"])
        context_refs = [draft] if isinstance(draft, str) else []
        suffix = f"v{revision}" if not human else "human"
        followup = self._op(
            f"followup:{kind}_revise_{suffix}",
            OperationType.FOLLOWUP_AGENT,
            {
                "agent_id": agent_id,
                "key": "agent",
                "reason": cfg["revise_message"],
                "context_refs": context_refs,
            },
        )
        wait = self._op(
            f"wait:{kind}_revise_{suffix}",
            OperationType.WAIT_AGENTS,
            {
                "from_op": followup.operation_id,
                "keys": ["agent"],
            },
        )
        facts: dict[str, object] = {
            cfg["source_fact"]: {"from_op": wait.operation_id, "worker": "agent"},
            cfg["verdict_fact"]: None,  # 修订完成 → 重新评审
        }
        if not human:
            facts[cfg["revision_fact"]] = revision + 1
        else:
            facts[cfg["resolution_fact"]] = "SUBMIT_ROLE_CORRECTION"
        commit = self._op(
            f"commit:{kind}_revise_{suffix}",
            OperationType.COMMIT_FACTS,
            {
                "facts": facts,
                "reason": cfg["revise_reason"],
            },
        )
        return self._plan(execution_id, cfg["revise_reason"], [followup, wait, commit])

    def _review_resolution_plan(
        self, execution_id: str, kind: str
    ) -> SupervisorPlan | None:
        """修订预算耗尽：interactive 持久化 HumanRequest，auto 直接 FAILED。"""
        cfg = _REVIEWS[kind]
        request = self._journal.human_request(cfg["request_id"])
        if request is not None and request.status == "OPEN":
            return None  # 已阻塞于开放请求，等 HUMAN_REPLY
        if request is not None and request.status == "ANSWERED":
            if request.answer == "ACCEPT_CURRENT_PROPOSAL":
                return self._review_accept_plan(
                    execution_id, kind, human_resolution=True
                )
            if request.answer == "SUBMIT_ROLE_CORRECTION":
                return self._review_revise_plan(execution_id, kind, 0, human=True)
        if self._journal.interaction_mode(execution_id) == "interactive":
            return self._request_human_plan(execution_id, kind)
        return self._fail_plan(execution_id, kind)

    def _request_human_plan(self, execution_id: str, kind: str) -> SupervisorPlan:
        """创建/重置持久化决议 HumanRequest（request_id 确定，可重放）。"""
        cfg = _REVIEWS[kind]
        source = self._journal.get_fact(cfg["source_fact"])
        draft = self._journal.get_fact(cfg["draft_fact"])
        agent_id = self._journal.get_fact(cfg["agent_fact"])
        context_refs = [r for r in (source, draft) if isinstance(r, str)]
        request = self._op(
            f"human:{cfg['request_id']}",
            OperationType.REQUEST_HUMAN,
            {
                "request_id": cfg["request_id"],
                "reason_code": cfg["resolution_reason"],
                "question": cfg["question"],
                "context_refs": context_refs,
                "input_mode": "single_select",
                "options": ["ACCEPT_CURRENT_PROPOSAL", "SUBMIT_ROLE_CORRECTION"],
                "requesting_agent_id": agent_id if isinstance(agent_id, str) else None,
            },
        )
        return self._plan(execution_id, cfg["resolution_reason"], [request])

    def _fail_plan(self, execution_id: str, kind: str) -> SupervisorPlan:
        """auto 修订预算耗尽 → 记录失败原因并把 execution 置为 FAILED。"""
        cfg = _REVIEWS[kind]
        commit = self._op(
            f"fail_reason:{kind}",
            OperationType.COMMIT_FACTS,
            {
                "facts": {"failure_reason": cfg["failure_reason"]},
                "reason": cfg["resolution_reason"],
            },
        )
        fail = self._op(
            f"fail_status:{kind}",
            OperationType.SET_EXECUTION_STATUS,
            {
                "status": ControlStatus.FAILED.value,
            },
        )
        return self._plan(execution_id, cfg["resolution_reason"], [commit, fail])

    def _prepare_baseline_plan(self, execution_id: str) -> SupervisorPlan:
        """PREPARE 第六步：Ideator 提方向 + CodeAgent 实现 baseline → trusted evaluator 评分。

        CodeAgent 产 baseline 的 predictions/labels（payload 带 ``baseline`` 标记，
        确定性 fallback 产出弱于 SEARCH 候选的基线模型）；``evaluation.baseline``
        用冻结 eval bundle 运行可信 evaluator，提交真实 ``baseline_experiment_ref``
        （CandidateEvaluation Artifact，SEARCH freeze 的 parent_score 可解析），
        不再是 worker result 原文。
        """
        task_config = self._task_config()
        target = str(task_config.get("data_payload", {}).get("target", ""))
        data_path: dict[str, object] = {"fact": "dataset_managed_root"}
        spawn = self._op(
            "spawn:baseline",
            OperationType.SPAWN_BATCH,
            {
                "spawns": [
                    {
                        "key": "ideator",
                        "agent_type": "ideator",
                        "payload": {
                            "content": "propose baseline direction",
                            "data_path": data_path,
                            "target": target,
                        },
                    },
                    {
                        "key": "code",
                        "agent_type": "code",
                        "payload": {
                            "content": "implement baseline",
                            "baseline": True,
                            "data_path": data_path,
                            "target": target,
                        },
                    },
                ],
            },
        )
        wait = self._op(
            "wait:baseline",
            OperationType.WAIT_AGENTS,
            {
                "from_op": spawn.operation_id,
                "keys": ["ideator", "code"],
            },
        )
        evaluate = self._op(
            "service:baseline.eval",
            OperationType.RUN_SERVICE,
            {
                "service": "evaluation.baseline",
                "search_experiment": False,  # baseline 评估不消费搜索预算
                "request": {
                    "eval_bundle": {"artifact": "eval_bundle_ref"},
                    "predictions": {
                        "from_op": wait.operation_id,
                        "worker": "code",
                        "field": "predictions",
                    },
                    "labels": {
                        "from_op": wait.operation_id,
                        "worker": "code",
                        "field": "labels",
                    },
                    "candidate_id": "baseline",
                    "direction": str(
                        task_config.get("ideator_payload", {}).get(
                            "direction", "maximize"
                        )
                    ),
                },
            },
        )
        return self._plan(execution_id, _PREPARE_BASELINE, [spawn, wait, evaluate])

    def _search_round_plan(self, execution_id: str) -> SupervisorPlan:
        """SEARCH 一轮真实编排：假设入图 → 候选可信评分 → freeze_round 判定唯一 SOTA。

        单 Plan 按序执行（executor 顺序运行、fail-fast）：Ideator 产假设 →
        ``search.register_hypotheses`` 全部入图 → CodeAgent 产候选（每候选
        predictions/labels）→ ``evaluation.candidate_batch`` 用 PREPARE 冻结的
        eval bundle 运行可信 evaluator 产出真实 test_score → ``freeze_ranking_round``
        按该 score 排序并 CAS 判定 SOTA（reason_code SEARCH_ROUND 使预算在 freeze
        服务事务中原子消费；register/evaluate 不消费预算）。
        parent_score 从 ``baseline_experiment_ref`` artifact 提取；尚无父 SOTA
        时 handler 按方向取最劣界，确立首个 SOTA。
        """
        cfg = self._task_config()
        spawn = self._op(
            "spawn:search",
            OperationType.SPAWN_BATCH,
            {
                "spawns": [
                    {
                        "key": "ideator",
                        "agent_type": "ideator",
                        "payload": self._data_aware_payload("ideator_payload"),
                    },
                    {
                        "key": "code",
                        "agent_type": "code",
                        "payload": self._data_aware_payload("code_payload"),
                    },
                ],
            },
        )
        wait = self._op(
            "wait:search",
            OperationType.WAIT_AGENTS,
            {
                "from_op": spawn.operation_id,
                "keys": ["ideator", "code"],
            },
        )
        register = self._op(
            "service:search.register",
            OperationType.RUN_SERVICE,
            {
                "service": "search.register_hypotheses",
                "search_experiment": False,  # 入图不消费预算（预算按轮一次）
                "request": {
                    "hypotheses": {
                        "from_op": wait.operation_id,
                        "worker": "ideator",
                        "field": "hypotheses",
                    }
                },
            },
        )
        evaluate = self._op(
            "service:search.eval",
            OperationType.RUN_SERVICE,
            {
                "service": "evaluation.candidate_batch",
                "search_experiment": False,  # 评估不消费预算（预算在 freeze 一次性消费）
                "request": {
                    "eval_bundle": {"artifact": "eval_bundle_ref"},
                    "candidates": {
                        "from_op": wait.operation_id,
                        "worker": "code",
                        "field": "candidates",
                    },
                },
            },
        )
        freeze = self._op(
            "service:search.freeze",
            OperationType.RUN_SERVICE,
            {
                "service": "search.freeze_ranking_round",
                "search_experiment": True,  # 仅 freeze 服务消费一次搜索预算
                "request": {
                    "parent_score": {
                        "artifact": "baseline_experiment_ref",
                        "field": "test_score",
                    },
                    "selected_count": int(cfg.get("selected_hypotheses_per_round", 2)),
                    "candidates": {
                        "from_op": evaluate.operation_id,
                        "service_result": 0,
                    },
                },
            },
        )
        return self._plan(
            execution_id, _SEARCH_ROUND, [spawn, wait, register, evaluate, freeze]
        )

    def _search_stop_plan(self, execution_id: str) -> SupervisorPlan:
        """搜索预算耗尽 → 提交 search_stop_ref 决策事实，不再生成新 round。"""
        commit = self._op(
            "commit:search_stop",
            OperationType.COMMIT_FACTS,
            {
                "facts": {"search_stop_ref": "search_stop:accepted"},
                "reason": _SEARCH_STOP,
            },
        )
        return self._plan(execution_id, _SEARCH_STOP, [commit])

    def _search_no_sota_plan(self, execution_id: str) -> SupervisorPlan:
        """搜索已停止但从未产生 SOTA → 无法进入 VALIDATE，execution FAILED。

        不重试/不循环 SEARCH（没有可验证的 SOTA，final-test 无从谈起）；与评审
        修订耗尽同构：记录 ``failure_reason`` 并把控制状态置为 FAILED。
        """
        commit = self._op(
            "commit:search_no_sota",
            OperationType.COMMIT_FACTS,
            {
                "facts": {"failure_reason": "SEARCH_STOPPED_NO_SOTA"},
                "reason": _SEARCH_NO_SOTA,
            },
        )
        fail = self._op(
            "fail_status:search_no_sota",
            OperationType.SET_EXECUTION_STATUS,
            {
                "status": ControlStatus.FAILED.value,
            },
        )
        return self._plan(execution_id, _SEARCH_NO_SOTA, [commit, fail])

    def _validate_plan(self, execution_id: str) -> SupervisorPlan:
        """VALIDATE 真实编排：reserve final-test → code 产预测 → 可信评分 → COMMITTED。

        ``RESERVE_FINAL_TEST`` 在同一原子事务冻结 SOTA/EvalSpec/dataset refs 并
        插入唯一 attempt（``final_test_attempt_id`` 事实供后续 op 复用）；
        ``ADVANCE_FINAL_TEST`` 按 RESERVED→RUNNING→PREDICTIONS_WRITTEN→SCORED
        →COMMITTED 前向推进（lease-fenced CAS）；SCORED 前的 ``evaluation.candidate_batch``
        用冻结 eval bundle 运行可信 final evaluator，产出独立 score Artifact 与真实
        final_test_score（非 fallback 候选字段）；COMMITTED 时 executor 构建
        ``ValidationResult``（gap/warning）并提交 final-test facts。
        """
        sota = self._journal.get_fact("sota_experiment_ref")
        eval_spec = self._journal.get_fact("eval_spec_ref")
        dataset = self._journal.get_fact("dataset_manifest_ref")
        if not all(isinstance(ref, str) for ref in (sota, eval_spec, dataset)):
            raise RuntimeError("VALIDATE requires sota/eval_spec/dataset facts")
        reserve = self._op(
            "reserve:final_test",
            OperationType.RESERVE_FINAL_TEST,
            {
                "sota_experiment_id": sota,
                "sota_ref": sota,
                "eval_spec_ref": eval_spec,
                "dataset_ref": dataset,
                "final_test_ref": eval_spec,  # final-test sealed 锚（首版以 EvalSpec 引用）
            },
        )
        spawn = self._op(
            "spawn:final_test",
            OperationType.SPAWN_BATCH,
            {
                "spawns": [
                    {
                        "key": "code",
                        "agent_type": "code",
                        "payload": self._data_aware_payload("code_payload"),
                    }
                ],
            },
        )
        wait = self._op(
            "wait:final_test",
            OperationType.WAIT_AGENTS,
            {
                "from_op": spawn.operation_id,
                "keys": ["code"],
            },
        )
        attempt_ref = {"fact": "final_test_attempt_id"}
        running = self._op(
            "advance:final_test_running",
            OperationType.ADVANCE_FINAL_TEST,
            {
                "attempt_id": attempt_ref,
                "expected_status": "RUNNING",
            },
        )
        predicted = self._op(
            "advance:final_test_pred",
            OperationType.ADVANCE_FINAL_TEST,
            {
                "attempt_id": attempt_ref,
                "expected_status": "PREDICTIONS_WRITTEN",
                "prediction_ref": {"from_op": wait.operation_id, "worker": "code"},
            },
        )
        evaluate = self._op(
            "service:final_test.eval",
            OperationType.RUN_SERVICE,
            {
                "service": "evaluation.candidate_batch",
                "search_experiment": False,  # final-test 评估不消费搜索预算
                "request": {
                    "eval_bundle": {"artifact": "eval_bundle_ref"},
                    "candidates": {
                        "from_op": wait.operation_id,
                        "worker": "code",
                        "field": "candidates",
                    },
                },
            },
        )
        scored = self._op(
            "advance:final_test_scored",
            OperationType.ADVANCE_FINAL_TEST,
            {
                "attempt_id": attempt_ref,
                "expected_status": "SCORED",
                # 可信 final evaluator 评分：独立 score Artifact（batch ref）+ 真实 score。
                "score_ref": {
                    "from_op": evaluate.operation_id,
                    "service_result": 0,
                    "raw": True,
                },
                "final_test_score": {
                    "from_op": evaluate.operation_id,
                    "service_result": 0,
                    "index": 0,
                    "field": "test_score",
                },
            },
        )
        committed = self._op(
            "advance:final_test_commit",
            OperationType.ADVANCE_FINAL_TEST,
            {
                "attempt_id": attempt_ref,
                "expected_status": "COMMITTED",
            },
        )
        return self._plan(
            execution_id,
            _VALIDATE_FINAL_TEST,
            [reserve, spawn, wait, running, predicted, evaluate, scored, committed],
        )

    def _spawn_wait_commit(
        self,
        execution_id: str,
        reason_code: str,
        rounds: list[tuple[str, str, dict[str, object]]],
        fact_specs: dict[str, object],
    ) -> SupervisorPlan:
        """构造 SPAWN→WAIT→COMMIT 一步式 Plan；facts 引用最后一个 round 的 worker。

        rounds 为 ``(key, agent_type, payload)`` 按序派发并等待；fact_specs 的
        value 为 ``None``（整个 result ref）、``str``（extract 字段存为 artifact）
        或 ``dict`` resolver spec（与 ``{from_op, worker}`` 合并，如
        ``{"field": "verdict"}`` 取字段原值）。
        """
        operations: list[SupervisorOperation] = []
        last_wait: SupervisorOperation | None = None
        for key, agent_type, payload in rounds:
            spawn = self._op(
                f"spawn:{key}",
                OperationType.SPAWN_BATCH,
                {
                    "spawns": [
                        {"key": key, "agent_type": agent_type, "payload": payload}
                    ],
                },
            )
            operations.append(spawn)
            last_wait = self._op(
                f"wait:{key}",
                OperationType.WAIT_AGENTS,
                {
                    "from_op": spawn.operation_id,
                    "keys": [key],
                },
            )
            operations.append(last_wait)
        if last_wait is None:
            raise ValueError("_spawn_wait_commit requires at least one round")
        facts: dict[str, object] = {}
        for fact_key, spec in fact_specs.items():
            base: dict[str, object] = {
                "from_op": last_wait.operation_id,
                "worker": rounds[-1][0],
            }
            if isinstance(spec, dict):
                ref: dict[str, object] = {**base, **spec}
            elif spec is None:
                ref = base
            else:
                ref = {**base, "extract": spec}
            facts[fact_key] = ref
        operations.append(
            self._op(
                f"commit:{reason_code.lower()}",
                OperationType.COMMIT_FACTS,
                {"facts": facts, "reason": reason_code},
            )
        )
        return self._plan(execution_id, reason_code, operations)

    # ---- 输入 / 公共构造 ----

    def _task_config(self) -> dict[str, dict[str, object]]:
        raw = self._journal.get_fact("task_config")
        if not isinstance(raw, dict):
            raise RuntimeError("task_config fact missing; call TASK_CONFIGURE first")
        return {key: value for key, value in raw.items() if isinstance(value, dict)}

    def _data_aware_payload(self, cfg_key: str) -> dict[str, object]:
        """worker payload：把 ``data_path`` 解析到受管 raw snapshot 事实。

        ingest（PREPARE_INGEST）先提交 ``dataset_managed_root``；后续 DataAgent/
        ideator/code worker 经 ``{"fact": ...}`` 由 executor 在执行时解析，消费
        同一份不可变受管目录，而不是 TASK_CONFIGURE 的原始绝对源路径。
        """
        payload = dict(self._task_config().get(cfg_key, {}))
        payload["data_path"] = {"fact": "dataset_managed_root"}
        if not payload.get("target"):
            payload["target"] = {
                "artifact": "dataset_role_proposal_ref",
                "field": "target_column",
                "default": "",
            }
        return payload

    def _op(
        self,
        idempotency_key: str,
        operation_type: OperationType,
        inputs: dict[str, object],
    ) -> SupervisorOperation:
        return SupervisorOperation(
            operation_id=new_id("op"),
            operation_type=operation_type,
            idempotency_key=idempotency_key,
            inputs=inputs,
        )

    def _plan(
        self,
        execution_id: str,
        reason_code: str,
        operations: list[SupervisorOperation],
    ) -> SupervisorPlan:
        return SupervisorPlan(
            plan_id=new_id("plan"),
            execution_id=execution_id,
            sequence=self._journal.next_sequence(execution_id),
            snapshot_version=self._journal.snapshot_version(),
            reason_code=reason_code,
            operations=operations,
            wait_policy="ALL_COMPLETED",
            created_at=utc_now(),
        )
