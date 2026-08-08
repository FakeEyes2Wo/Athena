"""EvalSpec append-only 版本链测试（设计 §5.5）。"""

import pytest

from athena.research.models import EvalSpec, EvalSpecChain, MetricDef


def _spec() -> EvalSpec:
    return EvalSpec(
        primary=MetricDef(name="accuracy", direction="maximize", description="acc"),
        secondary=[MetricDef(name="auc", direction="maximize", description="auc")],
    )


def test_chain_starts_at_version_one() -> None:
    chain = EvalSpecChain(_spec())
    assert chain.version == 1
    assert chain.current is chain.version_at(1)


def test_append_metric_creates_new_immutable_version() -> None:
    chain = EvalSpecChain(_spec())
    v1 = chain.current
    new_version = chain.append_metric(
        MetricDef(name="f1", direction="maximize", description="f1")
    )
    assert new_version == 2
    assert chain.version == 2
    assert [m.name for m in chain.version_at(2).secondary] == ["auc", "f1"]
    # v1 快照不受影响，已有条目不可改写
    assert [m.name for m in chain.version_at(1).secondary] == ["auc"]
    assert v1 is not chain.current


def test_append_does_not_delete_existing_entries() -> None:
    chain = EvalSpecChain(_spec())
    chain.append_metric(MetricDef(name="f1", direction="maximize", description="f1"))
    v2 = chain.current
    assert v2.primary == chain.version_at(1).primary  # primary 不可变
    assert "auc" in [m.name for m in v2.secondary]  # 已有条目保留


def test_duplicate_metric_rejected() -> None:
    chain = EvalSpecChain(_spec())
    with pytest.raises(ValueError):
        chain.append_metric(
            MetricDef(name="auc", direction="maximize", description="dup")
        )
    assert chain.version == 1  # 失败零副作用


def test_version_at_out_of_range_raises() -> None:
    chain = EvalSpecChain(_spec())
    with pytest.raises(IndexError):
        chain.version_at(2)
