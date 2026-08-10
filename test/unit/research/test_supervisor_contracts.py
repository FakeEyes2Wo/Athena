"""Supervisor 研究合同测试（supervisor_imp_docs Task 1）。

覆盖 ExecutionConfig 默认值/硬上限与 project_phase 的事实门槛投影。
"""

import pytest
from pydantic import ValidationError

from athena.research.contracts import ExecutionConfig
from athena.research.supervisor.models import ResearchPhase
from athena.research.supervisor.state import ProjectFacts, project_phase

_ART = "artifact://sha256:" + "0" * 64


def prepared_facts(search_stop_ref=None) -> ProjectFacts:
    """PREPARE 全部门槛已接受、但 search 尚未停止时的事实集合。"""
    return ProjectFacts(
        task_ref=_ART,
        dataset_role_review_ref=_ART,
        dataset_manifest_ref=_ART,
        eval_spec_ref=_ART,
        eda_review_ref=_ART,
        baseline_experiment_ref=_ART,
        sota_experiment_ref=_ART,
        search_stop_ref=search_stop_ref,
    )


def validated_facts() -> ProjectFacts:
    """VALIDATE 完成（含 final-test 事实）的事实集合。"""
    return prepared_facts(search_stop_ref=_ART).model_copy(
        update={
            "final_test_attempt_ref": _ART,
            "validation_result_ref": _ART,
        }
    )


def test_execution_config_defaults_and_limits() -> None:
    cfg = ExecutionConfig()
    assert cfg.k_folds == 5
    assert (cfg.ideator_count, cfg.hypotheses_per_ideator) == (3, 2)
    with pytest.raises(ValidationError):
        ExecutionConfig(ideator_count=9)
    with pytest.raises(ValidationError):
        ExecutionConfig(hypotheses_per_ideator=0)


def test_phase_requires_accepted_prepare_and_validation_facts() -> None:
    assert project_phase(ProjectFacts(task_ref=_ART)) == ResearchPhase.PREPARE
    assert project_phase(prepared_facts(search_stop_ref=None)) == ResearchPhase.SEARCH
    assert project_phase(validated_facts()) == ResearchPhase.COMPLETED
