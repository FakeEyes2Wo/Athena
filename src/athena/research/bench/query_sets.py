"""随包发布的 known-item 查询集，以及从任意路径读一份自定义查询集。

查询集是**版本化的输入数据**，和代码一起进 git：金标改了要能在 diff 里看见，因为一次
金标改动会让前后两次基准不可比，而这种不可比是静默的。
"""

import json
from pathlib import Path

from athena.research.bench.schemas import QuerySet

DATASETS_DIR = Path(__file__).parent / "datasets"
DEFAULT_QUERY_SET = "imbalance_auc"


def available() -> list[str]:
    """随包发布的查询集名字。"""
    return sorted(path.stem for path in DATASETS_DIR.glob("*.json"))


def load_query_set(name_or_path: str = DEFAULT_QUERY_SET) -> QuerySet:
    """按名字取随包查询集，或按路径读一份自定义的。

    先试路径再试名字：名字是短标识（``imbalance_auc``），路径必然带分隔符或后缀，
    两者不会误判。找不到时把可用名字一并写进异常——这类错误几乎总是拼错名字。
    """
    candidate = Path(name_or_path)
    if candidate.suffix == ".json" or candidate.exists():
        return QuerySet.model_validate_json(candidate.read_text(encoding="utf-8"))
    packaged = DATASETS_DIR / f"{name_or_path}.json"
    if not packaged.is_file():
        raise FileNotFoundError(
            f"unknown query set {name_or_path!r}; packaged sets: {', '.join(available())}"
        )
    return QuerySet.model_validate_json(packaged.read_text(encoding="utf-8"))


def dump_report(report, path: str | Path) -> Path:
    """把基准报告写成缩进 JSON，供两次运行 diff。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report.model_dump(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target
