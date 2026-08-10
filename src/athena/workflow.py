"""DEPRECATED: Historical workflow only; Athena-cli, Supervisor, Agents and tests must not read, import or execute this module."""

import ast
import asyncio
import json
import shutil
from pathlib import Path

import pandas as pd

from athena.code.backends.deepseek import DeepSeekCodeBackend
from athena.code.engine import CodeEngine
from athena.code.execution import ExecutionRequest, LocalExperimentRuntime
from athena.code.monitor import AgentMonitor
from athena.core.agent import settings
from athena.core.artifact_store import LocalArtifactStore
from athena.core.bundle import DirectoryBundle
from athena.core.contracts import ArtifactRef
from athena.core.git_workspace import LocalGitWorkspace
from athena.core.research_models import (
    ComparisonVerdict,
    EvalResult,
    ExperimentPlan,
    Hypothesis,
)
from athena.core.research_tree import (
    Experiment,
    ExperimentStatus,
    ResearchTree,
)
from athena.core.workspace import GitWorkBranch
from athena.research.models import MetricSpec, TaskMetaData
from athena.research.project_runtime import ProjectRuntime

# --- 硬编码变量 ---
DATASET = Path("examples/titanic/train.csv").resolve()
TARGET = "Survived"
OUTPUT_DIR = Path("examples/titanic-run").resolve()
SPLIT_SEED = 42
TRAIN_FRAC = 0.8
PREPARE_ATTEMPTS = 3
SEARCH_ITERATIONS = 3

HYPOTHESIS = (
    "在泰坦尼克号乘客生存预测任务中，对 Age（缺失值插补）、Sex、Pclass、Fare（分箱）、"
    "SibSp/Parch（聚合成家庭规模）等特征进行工程化处理后，训练分类模型（如随机森林/"
    "梯度提升），预期在保留验证集上的准确率显著高于性别规则 baseline。重点验证缺失值"
    "处理与家庭规模聚合两项特征工程的指标增益。"
)

TASK = TaskMetaData(
    task_type="classification",
    data_type="tabular",
    target_vars=[TARGET],
    primary_metric=MetricSpec(name="accuracy", direction="maximize"),
)

# 确定性 baseline 模型：性别规则（female=存活）。读 val.csv，写 predictions.csv+labels.csv。
_BASELINE_MODEL = """\
import pandas as pd

val = pd.read_csv("val.csv")
val["__athena_row_id"] = val.index
val["prediction"] = (val["Sex"] == "female").astype(int)
val[["__athena_row_id", "prediction"]].to_csv("predictions.csv", index=False)
val[["__athena_row_id", "Survived"]].rename(
    columns={"Survived": "target"}
).to_csv("labels.csv", index=False)
"""


async def _run_py(runtime: LocalExperimentRuntime, entrypoint: str, cwd: Path) -> str:
    """在 worktree 里跑一个 Python 脚本；非零退出码视为失败。"""
    output = await runtime.run(
        ExecutionRequest(entrypoint=entrypoint, cwd=cwd, timeout_s=600)
    )
    if output.returncode != 0:
        raise RuntimeError(
            f"{entrypoint} failed (rc={output.returncode}): {output.stderr[:400]}"
        )
    return output.stdout


def _parse_primary(stdout: str) -> float:
    """从 eval.py stdout 解析 ``{"primary": acc}``（JSON 或 Python dict repr）。"""
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        for loader in (json.loads, ast.literal_eval):
            try:
                data = loader(line)
            except (ValueError, TypeError, SyntaxError):
                continue
            if isinstance(data, dict) and "primary" in data:
                return float(data["primary"])
    raise RuntimeError(f"eval 输出中没有 primary 指标: {stdout[:300]}")


def _write_data_split(path: Path) -> None:
    """把完整 train.csv 确定性切成 train.csv + val.csv 写入 worktree（公平比较）。"""
    df = pd.read_csv(DATASET)
    train = df.sample(frac=TRAIN_FRAC, random_state=SPLIT_SEED)
    val = df.drop(train.index).reset_index(drop=True)
    train.to_csv(path / "train.csv", index=False)
    val.to_csv(path / "val.csv", index=False)


def _diff_writer(store: LocalArtifactStore):
    """LocalGitWorkspace 需要的 BinaryDiffWriter：把二进制 diff 存为 artifact。"""

    async def _write(data: bytes) -> ArtifactRef:
        return await store.put_bytes(data)

    return _write


async def _run_baseline(
    ws: LocalGitWorkspace,
    runtime: LocalExperimentRuntime,
    store: LocalArtifactStore,
    tree: ResearchTree,
    base: str,
    eval_text: str,
) -> tuple[str, float]:
    """baseline 实验：确定性性别规则模型，跑 eval 得 acc 并 set_sota。"""
    base_hyp = tree.add_hypothesis(
        Hypothesis(
            statement="性别规则（female=存活）作为确定性 baseline",
            intervention="仅用 Sex 特征按性别规则预测",
            expected_effect="提供准确率基线，供后续实验对比",
        )
    )
    work: GitWorkBranch = await ws.create(base, "baseline")
    path = Path(work.path)
    _write_data_split(path)
    (path / "eval.py").write_text(eval_text, encoding="utf-8")
    (path / "model.py").write_text(_BASELINE_MODEL, encoding="utf-8")
    await _run_py(runtime, "model.py", path)
    acc = _parse_primary(await _run_py(runtime, "eval.py", path))
    diff = await ws.diff(work)
    commit = await ws.commit(work, diff, "baseline: gender-rule model")
    run_config = await store.put_text(eval_text)
    per_sample = await store.put_bytes((path / "predictions.csv").read_bytes())
    exp_id = "exp-baseline"
    tree.add_experiment(
        exp_id,
        Experiment(
            hypothesis_id=base_hyp,
            commit=commit,
            plan=ExperimentPlan(
                kind="baseline",
                change="baseline",
                rubrics=[],
                run_config_ref=run_config,
                budget={},
                acceptance_rule="baseline accuracy",
            ),
            gitwork=work,
        ),
    )
    tree.transition_experiment(exp_id, ExperimentStatus.RUNNING)
    tree.complete_experiment(
        exp_id,
        eval=EvalResult(experiment_id=exp_id, primary=acc, per_sample=per_sample),
        verdict=None,
        artifacts={},
    )
    tree.set_sota(exp_id)
    return exp_id, acc


async def _run_search_loop(
    ws: LocalGitWorkspace,
    runtime: LocalExperimentRuntime,
    store: LocalArtifactStore,
    tree: ResearchTree,
    engine: CodeEngine,
    eval_text: str,
) -> list[tuple[str, float]]:
    """多轮 search：每轮从当前 SOTA 分支建 worktree，LLM 读/改进 model.py。

    第一轮删除继承的 baseline 模型逼 LLM 从零写真正训练的模型；后续轮保留上一轮
    model.py 让 LLM 读它做新特征工程/更强模型/神经网络（如 sklearn MLPClassifier）。
    每轮跑 eval 得 acc，与当前 SOTA 比较，赢则 set_sota。返回 [(exp_id, acc), ...]。
    """
    results: list[tuple[str, float]] = []
    for i in range(1, SEARCH_ITERATIONS + 1):
        sota_id = tree.best_experiment_id()
        sota = tree.get_experiment(sota_id)
        search_hyp = tree.add_hypothesis(
            Hypothesis(
                statement=f"{HYPOTHESIS}（第 {i} 轮改进，当前 SOTA acc={sota.eval.primary:.4f}）",
                intervention="新特征工程 / 更强模型 / 神经网络",
                expected_effect=f"验证集准确率高于当前 SOTA（{sota.eval.primary:.4f}）",
            )
        )
        work: GitWorkBranch = await ws.create(sota.commit, f"search-{i}")
        path = Path(work.path)
        _write_data_split(path)
        (path / "eval.py").write_text(eval_text, encoding="utf-8")
        if i == 1:
            # 第一轮：删掉继承的 baseline 模型，逼 LLM 从零写真正训练的模型
            for stale in ("model.py", "predictions.csv", "labels.csv"):
                (path / stale).unlink(missing_ok=True)
        instruction = (
            f"第 {i} 轮研究。当前 SOTA 实验 '{sota_id}' 验证集准确率 {sota.eval.primary:.4f}。\n"
            + (
                "工作区没有 model.py。请写 model.py：读 train.csv 做特征工程（Age 插补/"
                "Fare 分箱/SibSp+Parch 聚合等）并训练分类器（可尝试不同模型甚至神经网络，"
                "如 sklearn MLPClassifier），在 val.csv 上预测，写 predictions.csv"
                "（__athena_row_id, prediction）与 labels.csv（__athena_row_id, target）。"
                if i == 1
                else "工作区有上一轮 model.py。读它了解现有方法，然后改进：新特征工程、"
                "不同/更强模型、甚至神经网络（如 sklearn MLPClassifier）。必须在 train.csv"
                " 上真正训练。"
            )
            + " 运行 eval.py 得到主指标并迭代提升，确认 acc 高于当前 SOTA。写 REPORT.md"
            " 说明改进了什么、acc 是多少。"
        )
        result = await engine.run(
            prompt=instruction,
            target_dir=str(path),
            max_rounds=3,
            entrypoint="model.py",
        )
        if not result.success:
            print(f"[SEARCH] iter={i} CodeEngine 未产出可运行 model.py，跳过本轮")
            continue
        acc = _parse_primary(await _run_py(runtime, "eval.py", path))
        diff = await ws.diff(work)
        commit = await ws.commit(work, diff, f"search-{i}: LLM-improved model")
        run_config = await store.put_text(eval_text)
        per_sample = await store.put_bytes((path / "predictions.csv").read_bytes())
        cur_sota = tree.get_experiment(tree.best_experiment_id())
        winner = "candidate" if acc >= cur_sota.eval.primary else "baseline"
        verdict = ComparisonVerdict(winner=winner, p_value=0.0)
        exp_id = f"exp-search-{i}"
        tree.add_experiment(
            exp_id,
            Experiment(
                hypothesis_id=search_hyp,
                parent_id=sota_id,
                commit=commit,
                plan=ExperimentPlan(
                    kind="search",
                    change=f"search iteration {i}",
                    rubrics=[],
                    run_config_ref=run_config,
                    budget={},
                    acceptance_rule="acc >= current SOTA",
                ),
                gitwork=work,
            ),
        )
        tree.transition_experiment(exp_id, ExperimentStatus.RUNNING)
        tree.complete_experiment(
            exp_id,
            eval=EvalResult(experiment_id=exp_id, primary=acc, per_sample=per_sample),
            verdict=verdict,
            artifacts={},
        )
        if winner == "candidate":
            tree.set_sota(exp_id)
        results.append((exp_id, acc))
        print(f"[SEARCH] iter={i} exp={exp_id} acc={acc:.4f} winner={winner}")
    return results


async def main() -> None:
    """titanic 全流程：PREPARE（带重试）→ research loop → 报告 SOTA。"""
    runtime = LocalExperimentRuntime()

    # 1. PREPARE：InitAgent 写 eval.py；DataAgent 产出 EDA report（真调 DeepSeek）。
    #    内层 LLM 偶发只回文本不调用工具 → PREPARE 失败；重试清空输出目录重建，
    #    吸收该波动（PREPARE 残留 phase 会阻塞 CONFIGURED，故整目录重建）。
    project: ProjectRuntime | None = None
    store: LocalArtifactStore | None = None
    eval_text: str | None = None
    for attempt in range(1, PREPARE_ATTEMPTS + 1):
        shutil.rmtree(OUTPUT_DIR, ignore_errors=True)
        store = LocalArtifactStore(OUTPUT_DIR / "artifacts")
        tree = ResearchTree()
        project = ProjectRuntime(OUTPUT_DIR)
        project.register_defaults(
            model=settings.model_name(), client=settings.get_client()
        )
        await project.open()
        await project.configure(TASK)
        try:
            eda_ref = await project.prepare_data_analysis(DATASET, TARGET)
            if project.eval_ref is None:
                raise RuntimeError("init 未产出 eval.py")
            eval_text = await project.store.get_text(project.eval_ref)
            print(f"[PREPARE] attempt={attempt} EDA report ref = {eda_ref}")
            files = await DirectoryBundle.files(project.store, eda_ref)
            report_ref = files.get("report.md")
            if report_ref is not None:
                print("[EDA report.md]\n" + await project.store.get_text(report_ref))
            break
        except RuntimeError as exc:
            print(f"[PREPARE] attempt={attempt} failed: {exc}")
            await project.close()
            if attempt == PREPARE_ATTEMPTS:
                raise
    assert project is not None and store is not None and eval_text is not None

    # 2. Research loop
    ws = LocalGitWorkspace(
        OUTPUT_DIR / "repo",
        OUTPUT_DIR / "worktrees",
        _diff_writer(store),
    )
    base = await ws.init()
    base_exp, base_acc = await _run_baseline(ws, runtime, store, tree, base, eval_text)
    print(f"[BASELINE] exp={base_exp} acc={base_acc:.4f}")
    engine = CodeEngine(DeepSeekCodeBackend(), AgentMonitor())
    search_results = await _run_search_loop(ws, runtime, store, tree, engine, eval_text)
    if not search_results:
        raise RuntimeError("所有 search 迭代都未产出可运行模型")

    # 3. 报告（先持久化 research_tree 再打印，避免中途异常丢状态）
    tree.save(OUTPUT_DIR / "research_tree.json")
    sota_id = tree.best_experiment_id()
    sota = tree.get_experiment(sota_id)
    print(f"[SOTA] {sota.gitwork.branch} acc={sota.eval.primary:.4f}", flush=True)
    report_path = Path(sota.gitwork.path) / "REPORT.md"
    if report_path.is_file():
        print("[SOTA REPORT.md]\n" + report_path.read_text(encoding="utf-8"))
    await project.close()


if __name__ == "__main__":
    asyncio.run(main())
