"""Test the EDA sampling service (summary tools get_schema/get_summary/get_sample
were removed: DataAgent now writes analysis scripts directly)."""

import pandas as pd

from athena.research.data_models import create_analysis_samples


def test_large_dataset_uses_three_fixed_samples() -> None:
    frame = pd.DataFrame({"value": range(100_001)})
    stored: list[pd.DataFrame] = []

    def put_frame(sampled: pd.DataFrame) -> str:
        stored.append(sampled)
        return f"artifact://sample-{len(stored)}"

    refs = create_analysis_samples(frame, put_frame=put_frame)

    assert [item.seed for item in refs] == [17, 42, 97]
    assert [item.artifact for item in refs] == [
        "artifact://sample-1",
        "artifact://sample-2",
        "artifact://sample-3",
    ]
    assert [len(sampled) for sampled in stored] == [10_000, 10_000, 10_000]


def test_small_dataset_uses_one_fixed_sample() -> None:
    frame = pd.DataFrame({"value": range(5)})
    first: list[pd.DataFrame] = []
    second: list[pd.DataFrame] = []

    first_refs = create_analysis_samples(
        frame,
        put_frame=lambda sampled: first.append(sampled) or "artifact://first",
    )
    second_refs = create_analysis_samples(
        frame,
        put_frame=lambda sampled: second.append(sampled) or "artifact://second",
    )

    assert [item.seed for item in first_refs] == [17]
    assert [item.seed for item in second_refs] == [17]
    assert set(first[0].index) == set(frame.index)
    assert first[0].index.tolist() == second[0].index.tolist()


def test_configurable_eda_cap_triggers_three_samples() -> None:
    """data-analysis §7.1：EDA 上限可配置，超过项目配置时恰好三项等规模样本。"""
    frame = pd.DataFrame({"value": range(500)})  # 默认上限 100k 下仅 1 样本
    stored: list[pd.DataFrame] = []

    def put_frame(sampled: pd.DataFrame) -> str:
        stored.append(sampled)
        return f"artifact://sample-{len(stored)}"

    refs = create_analysis_samples(
        frame, put_frame=put_frame, eda_cap=100, sample_size=100
    )
    assert [item.seed for item in refs] == [17, 42, 97]
    assert len(refs) == 3
    assert [len(s) for s in stored] == [100, 100, 100]  # 三项等规模样本
