"""分叉的回归用例：两臂必须共用 evaluator 与基线，且必须不共用语料。

上一次 A/B 答不了原问题，不是执行的错而是设计的错：两臂各跑各的 PREPARE，于是各自写出
一个 evaluator、各自训出一个基线。同一任务、同一数据，两个基线的强弱就能差 0.03 以上，
而 SEARCH 的改进也就 +0.05 量级——基线方差与待测效应同量级。这些用例把"只剩 ideation
一个变量"钉住。
"""

import json
import unittest
from pathlib import Path
from tempfile import mkdtemp

from athena.research.fork import ForkError, fork_project
from athena.research.supervisor.state import ResearchState


def _tree(*, with_baseline: bool = True, evaluator: str = "sha256:eval") -> dict:
    experiments: dict[str, dict] = {
        "exp_search": {
            "id": "exp_search",
            "kind": "search",
            "plan": {"run_config_ref": "sha256:other"},
        }
    }
    if with_baseline:
        experiments["exp_baseline"] = {
            "id": "exp_baseline",
            "kind": "baseline",
            "plan": {"run_config_ref": evaluator},
        }
    return {"version": 2, "sota_id": "exp_baseline", "hypotheses": {}, "experiments": experiments}


def _prepared(**tree_kwargs) -> Path:
    """造一个"PREPARE 已完成"的项目目录。"""
    root = Path(mkdtemp(prefix="fork_src_"))
    athena = root / ".athena"
    (athena / "artifacts" / "ab").mkdir(parents=True)
    (athena / "artifacts" / "ab" / "cdef").write_text("blob", encoding="utf-8")
    (athena / "repo").mkdir(parents=True)
    (athena / "repo" / "solution.py").write_text("print(1)\n", encoding="utf-8")
    (athena / "workspaces").mkdir(parents=True)
    (athena / "workspaces" / "leftover.txt").write_text("stale", encoding="utf-8")
    (athena / "research_tree.json").write_text(
        json.dumps(_tree(**tree_kwargs)), encoding="utf-8"
    )
    state = ResearchState(
        status="WAITING",
        phase="SEARCH",
        search_limit=6,
        concurrency=1,
        corpus_ref="sha256:corpus-from-source",
        corpus_ideated_ref="sha256:corpus-from-source",
        eda_dir="workspaces/eda",
    )
    state.save(athena / "state.json")
    return root


def _target() -> Path:
    return Path(mkdtemp(prefix="fork_dst_")) / "arm"


class ForkTest(unittest.TestCase):
    def test_the_frozen_evaluator_and_baseline_come_along(self) -> None:
        """两臂共用同一个 evaluator，分数才第一次真正可比。"""
        source, target = _prepared(), _target()

        result = fork_project(source, target)

        self.assertEqual("sha256:eval", result.evaluator_ref)
        self.assertEqual("exp_baseline", result.baseline_experiment_id)
        self.assertEqual(
            json.loads((source / ".athena" / "research_tree.json").read_text("utf-8")),
            json.loads((target / ".athena" / "research_tree.json").read_text("utf-8")),
        )

    def test_artifacts_and_repo_are_copied_so_refs_still_resolve(self) -> None:
        source, target = _prepared(), _target()

        result = fork_project(source, target)

        self.assertEqual(("artifacts", "repo"), result.copied)
        self.assertEqual(
            "blob",
            (target / ".athena" / "artifacts" / "ab" / "cdef").read_text("utf-8"),
        )
        self.assertTrue((target / ".athena" / "repo" / "solution.py").is_file())

    def test_the_corpus_is_cleared_because_it_is_the_variable_under_test(self) -> None:
        """不清的话，关调研那臂会继承开调研那臂的语料，A/B 直接失去意义。"""
        source, target = _prepared(), _target()

        fork_project(source, target)

        state = ResearchState.load(target / ".athena" / "state.json")
        self.assertIsNone(state.corpus_ref)
        self.assertIsNone(state.corpus_ideated_ref)

    def test_the_arm_starts_at_search_not_prepare(self) -> None:
        """重跑 PREPARE 就等于把要固定的那两样又变回变量。"""
        source, target = _prepared(), _target()

        fork_project(source, target)

        state = ResearchState.load(target / ".athena" / "state.json")
        self.assertEqual("SEARCH", state.phase)
        self.assertEqual("RUNNING", state.status)
        self.assertEqual({}, state.plans)

    def test_settings_that_are_not_the_variable_survive(self) -> None:
        source, target = _prepared(), _target()

        fork_project(source, target)

        state = ResearchState.load(target / ".athena" / "state.json")
        self.assertEqual(6, state.search_limit)
        self.assertEqual("workspaces/eda", state.eda_dir)

    def test_stale_working_directories_are_left_behind(self) -> None:
        """上一次运行的临时产物复制过来，只会让新臂从一个半旧的工作区起步。"""
        source, target = _prepared(), _target()

        fork_project(source, target)

        self.assertFalse((target / ".athena" / "workspaces").exists())

    def test_a_source_without_a_baseline_is_refused(self) -> None:
        source, target = _prepared(with_baseline=False), _target()

        with self.assertRaises(ForkError) as caught:
            fork_project(source, target)

        self.assertIn("baseline", str(caught.exception))

    def test_a_baseline_without_a_frozen_evaluator_is_refused(self) -> None:
        source, target = _prepared(evaluator=""), _target()

        with self.assertRaises(ForkError) as caught:
            fork_project(source, target)

        self.assertIn("evaluator", str(caught.exception))

    def test_an_existing_target_is_never_overwritten(self) -> None:
        """一条臂跑到一半被另一条覆盖掉，表现出来是"分数怎么变了"，不是一条错误信息。"""
        source, target = _prepared(), _target()
        fork_project(source, target)

        with self.assertRaises(ForkError):
            fork_project(source, target)

    def test_a_missing_source_says_so(self) -> None:
        with self.assertRaises(ForkError):
            fork_project(Path(mkdtemp(prefix="empty_")), _target())


class LegacyStateTest(unittest.TestCase):
    def test_the_old_ideation_flag_is_migrated_rather_than_rejected(self) -> None:
        """``extra="forbid"`` 下留着旧字段会让续跑崩掉，而不是降级。"""
        payload = {
            "status": "RUNNING",
            "phase": "SEARCH",
            "search_limit": 4,
            "concurrency": 1,
            "corpus_ref": "sha256:corpus",
            "corpus_ideation_done": True,
        }
        path = Path(mkdtemp(prefix="legacy_")) / "state.json"
        path.write_text(json.dumps(payload), encoding="utf-8")

        state = ResearchState.load(path)

        self.assertEqual("sha256:corpus", state.corpus_ideated_ref)

    def test_an_unfinished_old_run_still_gets_its_extra_round(self) -> None:
        payload = {
            "status": "RUNNING",
            "phase": "SEARCH",
            "search_limit": 4,
            "concurrency": 1,
            "corpus_ref": "sha256:corpus",
            "corpus_ideation_done": False,
        }
        path = Path(mkdtemp(prefix="legacy_")) / "state.json"
        path.write_text(json.dumps(payload), encoding="utf-8")

        self.assertIsNone(ResearchState.load(path).corpus_ideated_ref)


if __name__ == "__main__":
    unittest.main()
