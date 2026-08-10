"""DataAgent — 数据分析的唯一提交者：内层 LLM agent 写 analysis.py → 收集 → 提交。

外层保留确定性编排：构造内层 LLM ReAct agent（prompt=data_agent.md + 通用工具，
cwd=workspace）→ 运行（LLM 用 ``write_file``/``bash`` 写出并运行固定名
``analysis.py``，产出 ``report.md`` 与 ``figures/*.png``）→ 收集 → 提交
DataAnalysis 版本。脚本模板不再写回代码——prompt 是限制的唯一来源。

请求可选带 ``report`` 覆盖报告文本，用于评审闭环的 failed 路径（确定性空报告
触发评审）。

提交链语义不变（设计 data-analysis-agent-workflow §5）：首次 create v1；后续
v2+ 必须同 owner 且 ``parent_ref == latest_ref``，因此另一 DataAgent 实例或
Reflection/Plot 都不能提交新版本。

测试接缝：``inner_builder(agent_type, *, model, client, workspace) -> Agent``
缺省用 :func:`~athena.agents.prompt_agent.build_llm_agent`；单元测试注入 fake
provider，避免依赖真实 LLM API。
"""

import asyncio
import json
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from athena.agents.prompt_agent import build_llm_agent
from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.agent.runtime import Agent, BaseAgent
from athena.core.contracts import ArtifactStore
from athena.core.bundle import VersionedBundle
from athena.research.contracts import DatasetRoleProposal

ANALYSIS_ENTRYPOINT = "analysis.py"


class DataAgent(BaseAgent):
    """DataAnalysis 唯一提交者：LLM 按 prompt 写 analysis.py 并运行，收集后提交。"""

    def __init__(
        self,
        store: ArtifactStore,
        bundle: VersionedBundle,
        owner_agent_id: str,
        *,
        model: str,
        client: Any = None,
        inner_builder: Callable[..., Agent] | None = None,
    ) -> None:
        self._store = store
        self._bundle = bundle
        self._owner = owner_agent_id
        self._model = model
        self._client = client
        self._inner_builder = inner_builder or build_llm_agent
        self.analysis_id: str | None = None
        self.latest_ref: str | None = None

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        """驱动内层 LLM agent 产出报告，收集 report/figures 提交 DataAnalysis 版本。"""
        request = json.loads(ctx.input_text or "{}")
        data_path = request.get("data_path")
        if not data_path:
            raise ValueError("DataAgent request requires 'data_path'")
        kind = str(request.get("kind", "eda"))
        target = request.get("target", "")
        workspace = Path(
            request.get("workspace") or tempfile.mkdtemp(prefix="athena-data-")
        )
        workspace.mkdir(parents=True, exist_ok=True)

        # 1. 内层 LLM agent：写 analysis.py → 运行 → 产出 report.md + figures
        inner = self._inner_builder(
            "data", model=self._model, client=self._client, workspace=workspace
        )
        inner_ctx = AgentContext(
            thread=ctx.thread,
            turn=ctx.turn,
            emit=ctx.emit,
            tools=inner.tools,
            cancel=ctx.cancel,
            memory=ctx.memory,
            input_text=json.dumps(
                {"kind": kind, "data_path": str(data_path), "target": target},
                ensure_ascii=False,
            ),
        )
        await inner.run(inner_ctx)

        script = workspace / ANALYSIS_ENTRYPOINT
        if not script.is_file():
            raise RuntimeError(f"DataAgent did not produce {ANALYSIS_ENTRYPOINT}")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            ANALYSIS_ENTRYPOINT,
            str(data_path),
            str(target),
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await process.communicate()
        if process.returncode:
            raise RuntimeError(
                f"{ANALYSIS_ENTRYPOINT} failed: "
                f"{stderr.decode('utf-8', errors='replace')}"
            )

        if kind == "role":
            proposal_path = workspace / "dataset_role_proposal.json"
            if not proposal_path.is_file():
                raise RuntimeError(
                    "role inspection did not produce dataset_role_proposal.json"
                )
            proposal = DatasetRoleProposal.model_validate_json(
                proposal_path.read_text(encoding="utf-8")
            )
            return AgentOutcome(
                result_ref=await self._store.put_text(proposal.model_dump_json())
            )

        # 2. 收集 report.md + figures/*.png + 可选 role proposal；可选的 report
        #    覆盖（评审闭环 failed 路径）
        files = await self._collect_files(workspace)
        if request.get("report") is not None:
            files["report.md"] = await self._store.put_text(str(request["report"]))

        # 3. 提交版本（v1 创建链；v2+ 同 owner 且 parent_ref == latest_ref）
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
        """把工作区产出固化为 Bundle 文件：report.md + figures/*.png（可选 role proposal）。

        DataAnalysis Bundle 合同要求恰一个根 ``report.md`` + 至少一张 ``figures/``
        图（VersionedBundle 校验）；role proposal JSON 作为同 Bundle 内的补充产物，
        proposal/EDA 两阶段在评审侧分流（role 读 proposal JSON、EDA 读 report）。
        """
        report_path = workspace / "report.md"
        if not report_path.is_file():
            raise RuntimeError("analysis script did not produce report.md")
        files = {
            "report.md": await self._store.put_text(
                report_path.read_text(encoding="utf-8")
            ),
        }
        proposal_path = workspace / "dataset_role_proposal.json"
        if proposal_path.is_file():
            files["dataset_role_proposal.json"] = await self._store.put_text(
                proposal_path.read_text(encoding="utf-8")
            )
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
