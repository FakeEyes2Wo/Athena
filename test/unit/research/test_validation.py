"""VALIDATE 依赖闭包与 final-test 状态机测试（supervisor_imp_docs Task 8）。

FULL_LINEAGE ablation 对每个 accepted intervention 计算依赖闭包（自身 +
全部传递依赖者）；final-test 唯一状态机在数据库 UNIQUE 索引下前向推进、
幂等 reserve、lease fencing、CAS 推进、Artifact 哈希持久化，已有 score 不可
被不同非空值覆盖（崩溃恢复不重训/重评分）。
"""

import sqlite3

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.research.supervisor.journal import PlanJournal, StaleLeaseError
from athena.research.validation import (
    RECOVERY_COMMIT,
    RECOVERY_EVALUATE,
    ValidationService,
    dependency_closure,
    final_test_recovery_action,
    generalization_gap,
    generalization_warning,
)

_GRAPH = {
    "feature-a": ["model-b"],
    "model-b": ["calibration-c"],
    "feature-x": ["model-b"],  # 共享下游
}


def test_dependency_closure_removes_transitive_dependents() -> None:
    assert dependency_closure(_GRAPH, "feature-a") == (
        "feature-a",
        "model-b",
        "calibration-c",
    )


def test_dependency_closure_isolated_intervention_is_itself() -> None:
    assert dependency_closure(_GRAPH, "feature-x") == (
        "feature-x",
        "model-b",
        "calibration-c",
    )


def test_ablation_scope_covers_all_accepted_interventions() -> None:
    service = ValidationService()
    scope = service.ablation_scope(_GRAPH, ["feature-a", "feature-x"])
    assert scope["feature-a"] == ("feature-a", "model-b", "calibration-c")
    assert scope["feature-x"] == ("feature-x", "model-b", "calibration-c")
    # 闭包至少包含自身
    assert all(intervention in closure for intervention, closure in scope.items())


# ---- final-test 泛化差距（design §2.5） ----


def test_generalization_gap_maximize_is_test_minus_final() -> None:
    """maximize 指标 gap = test - final（final 更低 = 泛化退化，gap 为正）。"""
    assert generalization_gap(0.80, 0.75, "maximize") == pytest.approx(0.05)
    assert generalization_gap(0.80, 0.85, "maximize") == pytest.approx(-0.05)


def test_generalization_gap_minimize_is_final_minus_test() -> None:
    """minimize 指标 gap = final - test（final 更高 = 泛化退化，gap 为正）。"""
    assert generalization_gap(0.10, 0.12, "minimize") == pytest.approx(0.02)
    assert generalization_gap(0.12, 0.10, "minimize") == pytest.approx(-0.02)


def test_generalization_warning_ignores_numeric_noise() -> None:
    """纯数值噪声（同一 tie tolerance 内）不算泛化退化，不触发 warning。"""
    assert generalization_warning(1e-13) is False
    assert generalization_warning(0.05) is True


def test_build_result_warns_but_still_completed() -> None:
    """真实但不理想的泛化表现：warning=true，但状态仍 COMPLETED。"""
    service = ValidationService()
    result = service.build_result(
        test_score=0.80,
        final_test_score=0.70,
        direction="maximize",
    )
    assert result.status == "COMPLETED"
    assert result.generalization_gap == pytest.approx(0.10)
    assert result.generalization_warning is True


def test_build_result_ideal_generalization_no_warning() -> None:
    """final 不差于 test 时 gap <= 0，无 warning。"""
    service = ValidationService()
    result = service.build_result(
        test_score=0.80,
        final_test_score=0.82,
        direction="maximize",
    )
    assert result.generalization_gap == pytest.approx(-0.02)
    assert result.generalization_warning is False


# ---- final-test 唯一状态机 ----

_FROZEN = {
    "sota_ref": "sha256:s",
    "eval_spec_ref": "sha256:e",
    "dataset_ref": "sha256:d",
}


def _journal_with_attempt(tmp_path):
    journal = PlanJournal(tmp_path / "s.db")
    lease = journal.claim_lease("e1", "owner")
    attempt = journal.reserve_final_test(
        execution_id="e1",
        sota_experiment_id="s1",
        final_test_ref="f1",
        lease=lease,
        **_FROZEN,
    )
    return journal, lease, attempt


def test_reserve_final_test_is_idempotent(tmp_path) -> None:
    """同一唯一键 reserve 复用同一 attempt（崩溃恢复不产生第二个逻辑尝试）。"""
    journal = PlanJournal(tmp_path / "s.db")
    lease = journal.claim_lease("e1", "owner")
    first = journal.reserve_final_test(
        execution_id="e1",
        sota_experiment_id="s1",
        final_test_ref="f1",
        lease=lease,
        **_FROZEN,
    )
    again = journal.reserve_final_test(
        execution_id="e1",
        sota_experiment_id="s1",
        final_test_ref="f1",
        lease=lease,
        **_FROZEN,
    )
    assert again.attempt_id == first.attempt_id
    assert first.status == "RESERVED"
    assert first.sota_ref == "sha256:s"  # 冻结 refs 与 attempt 同事务落库
    journal.close()


def test_reserve_conflicting_frozen_refs_rejected(tmp_path) -> None:
    """同一唯一键被不同冻结 refs 预留时拒绝（不静默复用错误事实）。"""
    journal, lease, _ = _journal_with_attempt(tmp_path)
    with pytest.raises(ValueError, match="different frozen refs"):
        journal.reserve_final_test(
            execution_id="e1",
            sota_experiment_id="s1",
            final_test_ref="f1",
            lease=lease,
            sota_ref="sha256:other",
            eval_spec_ref="sha256:e",
            dataset_ref="sha256:d",
        )
    journal.close()


def test_final_test_forward_transitions_preserve_score(tmp_path) -> None:
    """前向状态机推进；已有 score 只向前保留（不重评分）。"""
    journal, lease, attempt = _journal_with_attempt(tmp_path)
    attempt = journal.advance_final_test(attempt.attempt_id, "RUNNING", lease=lease)
    attempt = journal.advance_final_test(
        attempt.attempt_id,
        "PREDICTIONS_WRITTEN",
        lease=lease,
        prediction_ref="sha256:p",
    )
    attempt = journal.advance_final_test(
        attempt.attempt_id,
        "SCORED",
        lease=lease,
        score_ref="sha256:score",
        final_test_score=0.92,
    )
    attempt = journal.advance_final_test(attempt.attempt_id, "COMMITTED", lease=lease)
    assert attempt.status == "COMMITTED"
    assert attempt.final_test_score == 0.92  # 恢复时复用，不重评分
    assert attempt.prediction_ref == "sha256:p"  # Artifact 哈希在迁移中保存
    assert attempt.score_ref == "sha256:score"
    journal.close()


def test_final_test_score_is_immutable(tmp_path) -> None:
    """已有非空 score 不可被不同非空值覆盖（审计实测 0.92 -> 0.10 的问题）。"""
    journal, lease, attempt = _journal_with_attempt(tmp_path)
    for status in ("RUNNING", "PREDICTIONS_WRITTEN"):
        attempt = journal.advance_final_test(attempt.attempt_id, status, lease=lease)
    attempt = journal.advance_final_test(
        attempt.attempt_id,
        "SCORED",
        lease=lease,
        final_test_score=0.92,
    )
    with pytest.raises(ValueError, match="refusing to overwrite"):
        journal.advance_final_test(
            attempt.attempt_id,
            "COMMITTED",
            lease=lease,
            final_test_score=0.10,
        )
    # 相同 score 的幂等重放仍可向前提交（崩溃恢复不重评分）
    attempt = journal.advance_final_test(
        attempt.attempt_id,
        "COMMITTED",
        lease=lease,
        final_test_score=0.92,
    )
    assert attempt.status == "COMMITTED"
    assert attempt.final_test_score == 0.92
    journal.close()


@pytest.mark.asyncio
async def test_final_test_uses_real_content_addressed_refs(tmp_path) -> None:
    """prediction/score/frozen refs 用真实 ArtifactStore 内容引用（非测试字符串）。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    journal = PlanJournal(tmp_path / "s.db")
    lease = journal.claim_lease("e1", "owner")
    sota_ref = await store.put_text("frozen sota bundle")
    eval_ref = await store.put_text("eval spec")
    data_ref = await store.put_text("dataset manifest")
    attempt = journal.reserve_final_test(
        execution_id="e1",
        sota_experiment_id="s1",
        final_test_ref="f1",
        lease=lease,
        sota_ref=sota_ref,
        eval_spec_ref=eval_ref,
        dataset_ref=data_ref,
    )
    assert attempt.sota_ref == sota_ref
    attempt = journal.advance_final_test(attempt.attempt_id, "RUNNING", lease=lease)
    pred_ref = await store.put_text("prediction rows")
    attempt = journal.advance_final_test(
        attempt.attempt_id,
        "PREDICTIONS_WRITTEN",
        lease=lease,
        prediction_ref=pred_ref,
    )
    score_ref = await store.put_text('{"score": 0.92}')
    attempt = journal.advance_final_test(
        attempt.attempt_id,
        "SCORED",
        lease=lease,
        score_ref=score_ref,
        final_test_score=0.92,
    )
    assert attempt.prediction_ref == pred_ref
    assert attempt.score_ref == score_ref
    assert await store.get_text(attempt.score_ref) == '{"score": 0.92}'  # 可解析回原文
    journal.close()


def test_final_test_recovery_action_commits_only_when_score_exists(tmp_path) -> None:
    """崩溃恢复决策：SCORED + score_ref 才只提交；其余状态需 evaluator 评分。"""
    journal, lease, attempt = _journal_with_attempt(tmp_path)
    for status in ("RUNNING", "PREDICTIONS_WRITTEN"):
        attempt = journal.advance_final_test(attempt.attempt_id, status, lease=lease)
    attempt = journal.advance_final_test(
        attempt.attempt_id,
        "SCORED",
        lease=lease,
        score_ref="sha256:score",
        final_test_score=0.92,
    )
    assert final_test_recovery_action(attempt) == RECOVERY_COMMIT
    assert (
        final_test_recovery_action(attempt.model_copy(update={"score_ref": None}))
        == RECOVERY_EVALUATE
    )
    assert (
        final_test_recovery_action(
            attempt.model_copy(
                update={"status": "PREDICTIONS_WRITTEN", "score_ref": None}
            )
        )
        == RECOVERY_EVALUATE
    )
    journal.close()


def test_final_test_rejects_invalid_transition(tmp_path) -> None:
    """回退/跳级转换直接拒绝（RESERVED -> COMMITTED 非法）。"""
    journal, lease, attempt = _journal_with_attempt(tmp_path)
    with pytest.raises(ValueError, match="invalid final-test transition"):
        journal.advance_final_test(attempt.attempt_id, "COMMITTED", lease=lease)
    journal.close()


def test_final_test_advance_requires_stale_lease(tmp_path) -> None:
    """stale lease 无法推进（fencing）；非 owner 的写入立即失效。"""
    journal = PlanJournal(tmp_path / "s.db")
    first = journal.claim_lease("e1", "owner-a")
    attempt = journal.reserve_final_test(
        execution_id="e1",
        sota_experiment_id="s1",
        final_test_ref="f1",
        lease=first,
        **_FROZEN,
    )
    second = journal.take_over_expired_lease("e1", "owner-b", now=first.expires_at)
    with pytest.raises(StaleLeaseError):
        journal.advance_final_test(attempt.attempt_id, "RUNNING", lease=first)
    # 新 owner 可以正常推进
    journal.advance_final_test(attempt.attempt_id, "RUNNING", lease=second)
    journal.close()


def test_final_test_stale_lease_leaves_no_partial_write(tmp_path) -> None:
    """原子事务内 fencing 拒绝后无部分写入（prediction/score 不落库）。"""
    journal = PlanJournal(tmp_path / "s.db")
    first = journal.claim_lease("e1", "owner-a")
    attempt = journal.reserve_final_test(
        execution_id="e1",
        sota_experiment_id="s1",
        final_test_ref="f1",
        lease=first,
        **_FROZEN,
    )
    attempt = journal.advance_final_test(
        attempt.attempt_id,
        "RUNNING",
        lease=first,
        prediction_ref="sha256:p",
    )
    # B 接管过期 lease（generation+1），A 的旧 generation 尝试推进必须被拒绝
    journal.take_over_expired_lease("e1", "owner-b", now=first.expires_at)
    with pytest.raises(StaleLeaseError):
        journal.advance_final_test(
            attempt.attempt_id,
            "PREDICTIONS_WRITTEN",
            lease=first,
            prediction_ref="sha256:p2",
        )
    stored = journal._attempt_from_row(
        journal._conn.execute(
            "SELECT * FROM final_test_attempts WHERE attempt_id = ?",
            (attempt.attempt_id,),
        ).fetchone()
    )
    assert stored.status == "RUNNING"
    assert stored.prediction_ref == "sha256:p"  # 未写入 sha256:p2
    journal.close()


def test_reserve_final_test_database_unique_constraint(tmp_path) -> None:
    """数据库层 UNIQUE 索引强制唯一键（直接 INSERT 重复触发 IntegrityError）。"""
    journal, lease, attempt = _journal_with_attempt(tmp_path)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        journal._conn.execute(
            "INSERT INTO final_test_attempts (attempt_id, execution_id,"
            " sota_experiment_id, eval_spec_version, status, sota_ref,"
            " eval_spec_ref, dataset_ref, final_test_ref, created_at, updated_at)"
            " VALUES ('ft_dup', 'e1', 's1', '1', 'RESERVED', 'sha256:s',"
            " 'sha256:e', 'sha256:d', 'f1', 'now', 'now')",
        )
        journal._conn.commit()
    journal.close()


def test_reserve_is_idempotent_across_connections(tmp_path) -> None:
    """并发/恢复后的第二个连接 reserve 同一键返回同一 attempt。"""
    journal, lease, attempt = _journal_with_attempt(tmp_path)
    other = PlanJournal(tmp_path / "s.db")
    other_lease = other.claim_lease("e1", "owner")
    again = other.reserve_final_test(
        execution_id="e1",
        sota_experiment_id="s1",
        final_test_ref="f1",
        lease=other_lease,
        **_FROZEN,
    )
    assert again.attempt_id == attempt.attempt_id
    other.close()
    journal.close()
