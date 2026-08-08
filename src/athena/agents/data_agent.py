"""DataAgent — 数据分析的唯一提交者：写分析脚本 → 运行 → 收集 → 提交。

首版为确定性实现（无 LLM）：根据请求中的 ``data_path``/``target``/``workspace``
生成固定名的 ``analysis.py``（默认 EDA + 绘图脚本），用受信任运行时执行，收集
``report.md`` 与 ``figures/*.png`` 提交 DataAnalysis 版本。

不再暴露 DataTools：数据交互全部由脚本内的 pandas/matplotlib 直接完成
（读 CSV、EDA、画图到 ``figures/``、写 ``report.md``）。请求可选带 ``report``
覆盖报告文本，用于评审闭环的 failed 路径（确定性空报告触发评审）。

提交链语义不变（设计 data-analysis-agent-workflow §5）：首次 create v1；
后续 v2+ 必须同 owner 且 ``parent_ref == latest_ref``，因此另一 DataAgent 实例
或 Reflection/Plot 都不能提交新版本。
"""

import json
import tempfile
from pathlib import Path

from athena.code.execution import ExecutionRequest, LocalExperimentRuntime
from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.agent.runtime import BaseAgent
from athena.storage.artifact_store import ArtifactStore
from athena.storage.bundle import VersionedBundle

ANALYSIS_ENTRYPOINT = "analysis.py"
ANALYSIS_CONFIG = "analysis_config.json"

# 固定名默认 EDA + 绘图脚本（data/__init__ 意图：固定脚本名，程序运行即可）。
# 用 Agg 无头后端；颜色遵循 dataviz：数值直方图单色序、相关矩阵用发散色带
# （coolwarm 双色 + 中性中点）、类别用固定 tab10 序，不用彩虹色。
DEFAULT_ANALYSIS_SCRIPT = '''\
"""Athena DataAgent 默认 EDA + 绘图脚本（确定性骨架生成）。

读取数据集 → EDA → 生成 figures/*.png → 写 report.md。
不依赖 DataTools——所有数据交互由 pandas/matplotlib 直接完成。
"""
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import colormaps
import pandas as pd

plt.rcParams["axes.prop_cycle"] = plt.cycler(color=colormaps["tab10"].colors)

config = json.loads(Path("analysis_config.json").read_text(encoding="utf-8"))
data_path = config["data_path"]
target = config.get("target", "")

frame = pd.read_csv(data_path)
figures = Path("figures")
figures.mkdir(exist_ok=True)

report = ["# Data Analysis Report", ""]

# --- Schema ----------------------------------------------------------
report.append("## Schema")
report.append("")
for column, dtype in frame.dtypes.items():
    report.append(f"- `{column}`: {dtype}")
report.append("")

# --- Overview --------------------------------------------------------
report.append("## Overview")
report.append("")
report.append(f"- rows: **{len(frame)}**")
report.append(f"- columns: **{len(frame.columns)}**")
missing = frame.isna().sum()
missing_columns = missing[missing > 0]
if not missing_columns.empty:
    report.append("")
    report.append("Missing values:")
    for column in missing_columns.index:
        report.append(f"  - `{column}`: {int(missing_columns[column])}")
report.append("")

created: list[str] = []

# --- Numeric distributions -------------------------------------------
numeric = frame.select_dtypes(include="number")
if not numeric.empty:
    plot_columns = list(numeric.columns[:8])
    fig, axes = plt.subplots(1, len(plot_columns), figsize=(2.6 * len(plot_columns), 2.6))
    if len(plot_columns) == 1:
        axes = [axes]
    for ax, column in zip(axes, plot_columns):
        values = numeric[column].dropna()
        ax.hist(values, bins=20, color="#4C72B0", edgecolor="white", linewidth=0.5)
        ax.set_title(str(column), fontsize=10)
        ax.tick_params(labelsize=8)
    fig.suptitle("Numeric feature distributions", fontsize=12)
    fig.tight_layout()
    fig.savefig(figures / "distributions.png", dpi=130)
    plt.close(fig)
    created.append("distributions.png")
    report.append("## Distributions")
    report.append("")
    report.append("![Numeric feature distributions](figures/distributions.png)")
    report.append("")

# --- Missing values --------------------------------------------------
if not missing_columns.empty:
    ordered = missing_columns.sort_values(ascending=False)
    fig, ax = plt.subplots(figsize=(6.4, 3.6))
    ax.bar([str(c) for c in ordered.index], ordered.values, color="#DD8452")
    ax.set_title("Missing values by column")
    ax.set_ylabel("count")
    ax.tick_params(axis="x", rotation=45, labelsize=8)
    fig.tight_layout()
    fig.savefig(figures / "missing.png", dpi=130)
    plt.close(fig)
    created.append("missing.png")
    report.append("## Missing values")
    report.append("")
    report.append("![Missing values](figures/missing.png)")
    report.append("")

# --- Correlation -----------------------------------------------------
if len(numeric.columns) >= 2:
    size = max(3.2, 0.55 * len(numeric.columns))
    fig, ax = plt.subplots(figsize=(size, size))
    corr = numeric.corr()
    im = ax.imshow(corr.values, cmap="coolwarm", vmin=-1, vmax=1)
    ax.set_xticks(range(len(corr.columns)))
    ax.set_yticks(range(len(corr.columns)))
    ax.set_xticklabels([str(c) for c in corr.columns], rotation=90, fontsize=7)
    ax.set_yticklabels([str(c) for c in corr.columns], fontsize=7)
    fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    ax.set_title("Feature correlation matrix")
    fig.tight_layout()
    fig.savefig(figures / "correlation.png", dpi=130)
    plt.close(fig)
    created.append("correlation.png")
    report.append("## Correlations")
    report.append("")
    report.append("![Feature correlation matrix](figures/correlation.png)")
    report.append("")

# --- Target distribution --------------------------------------------
if target and target in frame.columns:
    try:
        counts = frame[target].value_counts()
        top = counts.head(20)
        fig, ax = plt.subplots(figsize=(6.4, 3.6))
        ax.bar([str(v) for v in top.index], top.values, color="#55A868")
        ax.set_title(f"Target distribution: {target}")
        ax.set_ylabel("count")
        ax.tick_params(axis="x", rotation=45, labelsize=8)
        fig.tight_layout()
        fig.savefig(figures / "target.png", dpi=130)
        plt.close(fig)
        created.append("target.png")
        report.append("## Target")
        report.append("")
        report.append(f"![Target distribution](figures/target.png)")
        report.append("")
    except Exception:
        pass

# --- 保证至少一张图 ---------------------------------------------------
if not created:
    present = frame.notna().sum()
    fig, ax = plt.subplots(figsize=(6.4, 3.6))
    ax.bar([str(c) for c in frame.columns], present.values, color="#4C72B0")
    ax.set_title("Non-null counts by column")
    ax.set_ylabel("count")
    ax.tick_params(axis="x", rotation=45, labelsize=8)
    fig.tight_layout()
    fig.savefig(figures / "overview.png", dpi=130)
    plt.close(fig)

Path("report.md").write_text("\\n".join(report), encoding="utf-8")
'''


class DataAgent(BaseAgent):
    """DataAnalysis 版本的唯一提交者（确定性实现：写脚本 → 运行 → 提交）。"""

    def __init__(
        self,
        store: ArtifactStore,
        bundle: VersionedBundle,
        owner_agent_id: str,
        *,
        runtime=None,
        script: str | None = None,
    ) -> None:
        self._store = store
        self._bundle = bundle
        self._owner = owner_agent_id
        self._runtime = runtime or LocalExperimentRuntime()
        self._script = script or DEFAULT_ANALYSIS_SCRIPT
        self.analysis_id: str | None = None
        self.latest_ref: str | None = None

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        request = json.loads(ctx.input_text or "{}")
        data_path = request.get("data_path")
        if not data_path:
            raise ValueError("DataAgent request requires 'data_path'")
        target = request.get("target", "")
        workspace = Path(
            request.get("workspace") or tempfile.mkdtemp(prefix="athena-data-")
        )
        workspace.mkdir(parents=True, exist_ok=True)

        # 1. 写固定名分析脚本 + 运行配置
        (workspace / ANALYSIS_ENTRYPOINT).write_text(self._script, encoding="utf-8")
        (workspace / ANALYSIS_CONFIG).write_text(
            json.dumps(
                {"data_path": str(data_path), "target": target},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        # 2. 受信任运行时执行脚本
        output = await self._runtime.run(
            ExecutionRequest(
                entrypoint=ANALYSIS_ENTRYPOINT,
                cwd=workspace,
                timeout_s=300,
            )
        )
        if output.returncode != 0:
            detail = (output.stderr or output.stdout or "").strip()
            raise RuntimeError(f"analysis script failed: {detail}")

        # 3. 收集 report.md + figures/*.png；可选的 report 覆盖（评审闭环 failed 路径）
        files = await self._collect_files(workspace)
        if request.get("report") is not None:
            files["report.md"] = await self._store.put_text(str(request["report"]))

        # 4. 提交版本（v1 创建链；v2+ 同 owner 且 parent_ref == latest_ref）
        if self.analysis_id is None:
            self.analysis_id, self.latest_ref = await self._bundle.create(
                self._owner, files
            )
        else:
            self.latest_ref = await self._bundle.commit(
                self.analysis_id, self._owner, files, self.latest_ref
            )
        return AgentOutcome(result_ref=self.latest_ref)

    async def _collect_files(self, workspace: Path) -> dict[str, str]:
        """把工作区产出固化为 Bundle 文件：report.md + figures/*.png。"""
        report_path = workspace / "report.md"
        if not report_path.is_file():
            raise RuntimeError("analysis script did not produce report.md")
        files = {
            "report.md": await self._store.put_text(
                report_path.read_text(encoding="utf-8")
            ),
        }
        figures_dir = workspace / "figures"
        if figures_dir.is_dir():
            for path in sorted(figures_dir.iterdir()):
                if path.is_file():
                    files[f"figures/{path.name}"] = await self._store.put_bytes(
                        path.read_bytes()
                    )
        if not any(key.startswith("figures/") for key in files):
            raise RuntimeError("analysis script produced no figures")
        return files
