"""Atomic persistence for the autonomous Supervisor's ``state.json``.

断点续传字段与旧 schema 不兼容（``extra="forbid"``），因此分开持久化：
``state.json`` 只写旧版核心字段，新增的 resume 字段落到同目录 ``resume.json``，
并绑定核心 payload 的摘要——旧代码重写 ``state.json`` 后摘要失配，新代码会
自动忽略过期的 resume 数据。
"""

import hashlib
import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from athena.core.contracts import ArtifactRef
from athena.core.persistence import atomic_write_json
from athena.research.supervisor.plans import PlanState

logger = logging.getLogger(__name__)

RESUME_FIELDS = (
    "task_text",
    "kaggle_download",
    "task_research_task",
    "task_research_ref",
    "task_research_agent_id",
    "evaluator_ref",
)


def _resume_path(path: Path) -> Path:
    """Return the sibling resume file for one state path."""
    return path.with_name("resume.json")


def _core_digest(payload: Mapping) -> str:
    """Digest core payload so stale resume files can be detected after downgrade."""
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _merge_resume(candidate: dict, payload: Mapping, resume_path: Path) -> None:
    """Overlay a digest-matching resume file onto the core candidate."""
    if not resume_path.is_file():
        return
    try:
        resume = json.loads(resume_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # resume 文件损坏/截断 → 按无断点继续，核心状态仍可用
        return
    if not isinstance(resume, dict) or resume.get("state_digest") != _core_digest(
        payload
    ):
        logger.warning(
            "ignoring stale resume file %s (core state changed)", resume_path
        )
        return
    for key in RESUME_FIELDS:
        if key in resume:
            candidate[key] = resume[key]


class ResearchState(BaseModel):
    """Small durable checkpoint for unfinished autonomous research."""

    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["RUNNING", "WAITING", "COMPLETED", "STOPPED", "FAILED"]
    phase: Literal["PREPARE", "SEARCH", "VALIDATE", "COMPLETED"]
    search_limit: int = Field(ge=0)
    concurrency: int = Field(ge=1)
    # 每轮 ideation 的并行 lane 数与每 lane 假设数（与 SEARCH 并发度解耦）。
    ideator_count: int = Field(default=3, ge=1, le=8)
    hypotheses_per_ideator: int = Field(default=2, ge=1, le=5)
    # SEARCH 调度模式：False=自动按优先级出队；True=每个假设生成后等待人工选定。
    manual_mode: bool = False
    plans: dict[str, PlanState] = Field(default_factory=dict)
    validation: dict[str, object] | None = None
    # PREPARE 产出的 EDA 工作区目录（Ideator 自行探索）；仅路径元数据，非 EDA 结果。
    eda_dir: str | None = None
    # Supervisor 在首个 task-understanding turn 产出的结构化任务理解（供 GUI 意图预览）。
    task_understanding: dict[str, object] | None = None
    # Academic Survey 建好的论文语料索引；Ideator 只读，凭它调用 paper_* 检索算子。
    # 与其他引用一样落在本项目的 artifact store 里，换机器取不到时重跑即可。
    corpus_ref: str | None = None
    # Idea Generation 可用的 handoff 来源（前端 settings_set 可控制），
    # 取值示例：["kaggle"]、["kaggle", "literature"]、[]。
    handoff_sources: list[str] = Field(default_factory=lambda: ["kaggle", "literature"])
    # source -> artifact ref，记录已生成的 handoff，供断点续传复用。
    handoff_refs: dict[str, ArtifactRef] = Field(default_factory=dict)
    # 断点续传：首次完整任务文本（续跑时沿用，避免短消息污染 survey/PREPARE 提示词）。
    task_text: str | None = None
    # 断点续传：configure_kaggle 的持久化决定（None=未决定，False=已决定关闭）。
    kaggle_download: bool | None = None
    # 断点续传：任务理解阶段 general 调研的产物引用、worker 稳定 id 与任务原文；
    # 缓存按任务原文匹配，避免把调研结果错当成后续任意 general 任务的结果。
    task_research_task: str | None = None
    task_research_ref: ArtifactRef | None = None
    task_research_agent_id: str | None = None
    # 断点续传：已冻结评估器 bundle；PREPARE 重启时跳过 evaluator 重跑。
    evaluator_ref: ArtifactRef | None = None
    # 已经为哪一份语料补跑过 ideation。调研要十几分钟，第一轮 ideation 几乎必然早于它
    # 完成；而调度器只在"无假设可排"时才 GENERATE，短跑测里第一轮就把队列填满，于是
    # 语料一次都读不到。
    #
    # 记的是**语料引用**而不是一个布尔标记：语料现在可以增量扩充（见 PaperLibrary），
    # 布尔标记会让扩充之后的新论文永远读不到——第一轮补过就再也不补了。存引用之后，
    # 判据变成"这一份语料补过没有"，扩充自然触发下一轮。
    corpus_ideated_ref: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_corpus_ideation_flag(cls, payload: object) -> object:
        """把旧状态里的 ``corpus_ideation_done`` 翻译成 ``corpus_ideated_ref``。

        ``extra="forbid"`` 下，旧字段留在 ``state.json`` 里会让续跑直接校验失败——那是
        最糟的一种不兼容：用户看到的是崩溃，而不是降级。翻译而不是丢弃，是为了保住原
        行为：已经补过那一轮的运行不该因为升级而再补一轮。
        """
        if not isinstance(payload, Mapping) or "corpus_ideation_done" not in payload:
            return payload
        migrated = dict(payload)
        done = migrated.pop("corpus_ideation_done", False)
        if done and not migrated.get("corpus_ideated_ref"):
            migrated["corpus_ideated_ref"] = migrated.get("corpus_ref")
        return migrated

    @model_validator(mode="after")
    def _validate_plan_keys(self) -> "ResearchState":
        for plan_id, plan in self.plans.items():
            if plan.kind == "PREPARE" and plan_id != "prepare":
                raise ValueError("PREPARE plan key must be 'prepare'")
            if plan.kind == "VALIDATE" and plan_id != "validate":
                raise ValueError("VALIDATE plan key must be 'validate'")
            if plan.kind == "SEARCH" and (
                not plan_id.strip() or plan_id in {"prepare", "validate"}
            ):
                raise ValueError("SEARCH plan key must be a Hypothesis ID")
        return self

    def save(self, path: str | Path) -> Path:
        """Atomically replace ``path`` with the core state and persist resume fields.

        Core state keeps the legacy schema so older binaries can still read
        ``state.json``; resume fields live in a sibling ``resume.json``.
        """
        target = Path(path)
        core = self.model_dump(mode="json", exclude=RESUME_FIELDS)
        atomic_write_json(target, core)
        resume_payload = {
            key: getattr(self, key)
            for key in RESUME_FIELDS
            if getattr(self, key) is not None
        }
        resume_path = _resume_path(target)
        if resume_payload:
            atomic_write_json(
                resume_path,
                {"state_digest": _core_digest(core), **resume_payload},
            )
        else:
            resume_path.unlink(missing_ok=True)
        return target

    @classmethod
    def load(cls, path: str | Path) -> "ResearchState":
        """Load and validate a state object from JSON plus its resume file.

        兼容三种磁盘形态：旧版核心 schema、中间版本把 resume 字段内联进
        ``state.json``、当前版的核心 + ``resume.json`` 拆分布局。读取中间版本
        时会立即重写为拆分布局，让旧二进制重新可读核心文件。
        """
        target = Path(path)
        payload = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("research state payload must be an object")
        if "corpus_ideation_done" in payload:
            payload = cls._migrate_corpus_ideation_flag(payload)
        unknown = [key for key in payload if key not in cls.model_fields]
        if unknown:
            # agent/旧版本可能写入未知键（例如误写的 ``sota``）：告警、备份原文、
            # 剥离后继续加载，避免一个外来键让整个项目无法打开。
            logger.warning(
                "stripping unknown state.json keys %s from %s", unknown, target
            )
            try:
                backup = target.with_name("state.json.corrupt")
                backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                logger.warning("failed to back up corrupted state file", exc_info=True)
            payload = {
                key: value for key, value in payload.items() if key in cls.model_fields
            }
        candidate = dict(payload)
        inline_resume = any(key in candidate for key in RESUME_FIELDS)
        _merge_resume(candidate, payload, _resume_path(target))
        if candidate.get("task_research_ref") is not None and not candidate.get(
            "task_research_task"
        ):
            # 中间版本存过没有任务原文的调研引用：无法判断缓存归属，按无缓存处理。
            candidate["task_research_ref"] = None
            candidate["task_research_agent_id"] = None
            inline_resume = True
        state = cls.model_validate(candidate)
        if inline_resume:
            # 迁移中间版本的内联布局：旧二进制只能读不含 resume 字段的核心文件。
            state.save(target)
        return state
