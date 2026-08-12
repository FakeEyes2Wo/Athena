"""DataAgent — 数据分析的唯一提交者：内层 LLM agent 写 analysis.py → 收集 → 提交。

外层保留确定性编排：构造内层 LLM ReAct agent（prompt=data_agent.md + 通用工具，
cwd=workspace）→ 运行（LLM 用 ``write_file``/``shell_command`` 写出并运行固定名
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
import hashlib
import json
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic_ai.messages import ModelRequest, SystemPromptPart

from athena.agents.prompt_agent import build_llm_agent
from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.agent.runtime import Agent, BaseAgent
from athena.core.contracts import ArtifactStore
from athena.core.bundle import VersionedBundle
from athena.memory.context_manager import ContextManager
from athena.research.contracts import (
    DatasetRoleProposal,
    EDAAttemptOutcome,
    EDARepairFailure,
)

if TYPE_CHECKING:
    from athena.execution.runtime import ExecutionRuntime

ANALYSIS_ENTRYPOINT = "analysis.py"
_MAX_FAILURE_STDERR_CHARS = 4_000
# DataAgent 跨重启状态 marker（memory-flow-fixes §DataAgent State Recovery）。
# 版本编码在前缀内：后续版本改前缀即可，旧代码自然忽略未知版本。
_DATA_STATE_PREFIX = "[ATHENA DATA AGENT STATE v1]"


def _repair_signature(exit_code: int, stderr: str) -> str:
    """(exit_code, 归一化 stderr 头) 的紧凑哈希，供 Supervisor 识别重复失败。"""
    return hashlib.sha256(
        f"{exit_code}\n{stderr.strip()[:1_000]}".encode()
    ).hexdigest()[:16]


def _workspace_python(workspace: Path) -> str:
    """canonical 校验的解释器：优先 workspace-local .venv，缺省回退当前解释器。

    workspace-local 环境是首选执行环境（eda-auto-repair §Workspace / Validation 10）；
    不存在 .venv 时回退 ``sys.executable`` 保证既有流程可用。
    """
    for candidate in (
        workspace / ".venv" / "Scripts" / "python.exe",  # Windows
        workspace / ".venv" / "bin" / "python",  # POSIX
    ):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def _validated_workspace(raw: object, runtime: object | None) -> Path:
    if not isinstance(raw, str) or not raw:
        raise ValueError("DataAgent request requires project-local 'workspace'")
    workspace = Path(raw).resolve()
    if runtime is not None:
        project_root = Path(getattr(runtime, "project_root")).resolve()
        if not workspace.is_relative_to(project_root):
            raise ValueError(f"workspace outside project root: {workspace}")
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


async def _run_script(
    workspace: Path, data_path: str, target: str
) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        _workspace_python(workspace),
        ANALYSIS_ENTRYPOINT,
        data_path,
        target,
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return (
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def _repair_from_failure(failure: EDARepairFailure) -> str:
    """修复 follow-up prompt：把上轮失败（命令/退出码/stderr）交给内层原地修脚本。"""
    command = " ".join(failure.command) if failure.command else "python analysis.py"
    return (
        "The previous analysis.py run failed and the framework reported this exact "
        "failure.\n"
        f"Command: {command}\n"
        f"Exit code: {failure.exit_code}\n"
        f"stderr:\n{failure.stderr}\n"
        "Inspect the current persistent workspace and edit analysis.py in place to fix "
        "this exact failure. Do not only explain the fix and do not run the script "
        "yourself; leave the workspace ready for the framework's canonical validation. "
        "The write_file tool replaces the entire file: read the current script first "
        "and write back the complete corrected script, never a partial snippet."
    )


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
        runtime: "ExecutionRuntime | None" = None,
    ) -> None:
        self._store = store
        self._bundle = bundle
        self._owner = owner_agent_id
        self._model = model
        self._client = client
        self._inner_builder = inner_builder or build_llm_agent
        self._runtime = runtime
        self.analysis_id: str | None = None
        self.latest_ref: str | None = None
        self._task: dict[str, object] = {}  # 首次运行的任务（REVISE follow-up 复用）
        self._workspace: Path | None = None  # 跨 run 复用，让 LLM 可读修自己的脚本

    def _remember_state(self, memory: ContextManager | None) -> None:
        """成功产出后把状态 marker 追加到会话记忆（随 rollout 持久化）。"""
        if memory is None:
            return
        payload = {
            "task": self._task,
            "workspace": str(self._workspace) if self._workspace is not None else None,
            "analysis_id": self.analysis_id,
            "latest_ref": self.latest_ref,
        }
        memory.append(
            ModelRequest(
                parts=[
                    SystemPromptPart(
                        content=(
                            f"{_DATA_STATE_PREFIX}\n"
                            + json.dumps(
                                payload, ensure_ascii=False, separators=(",", ":")
                            )
                        )
                    )
                ]
            )
        )

    def _restore_state(self, memory: ContextManager | None) -> None:
        """从记忆中最新的 v1 marker 恢复缺失的 Python 字段（幂等、容错）。

        损坏/未知版本继续反向扫描而非报错；已不存在的 workspace 不恢复（将新建）；
        已填字段不覆盖。
        """
        if memory is None:
            return
        prefix = f"{_DATA_STATE_PREFIX}\n"
        for message in reversed(memory.items):
            for part in reversed(message.parts):
                content = getattr(part, "content", None)
                if not isinstance(content, str) or not content.startswith(prefix):
                    continue
                try:
                    payload = json.loads(content[len(prefix) :])
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                task = payload.get("task")
                if isinstance(task, dict) and not self._task:
                    self._task = {
                        key: task[key]
                        for key in ("data_path", "target", "kind")
                        if key in task
                    }
                workspace = payload.get("workspace")
                if self._workspace is None and isinstance(workspace, str):
                    candidate = Path(workspace)
                    if candidate.is_dir():
                        self._workspace = candidate
                analysis_id = payload.get("analysis_id")
                if self.analysis_id is None and isinstance(analysis_id, str):
                    self.analysis_id = analysis_id
                latest_ref = payload.get("latest_ref")
                if self.latest_ref is None and isinstance(latest_ref, str):
                    self.latest_ref = latest_ref
                return  # 只处理最新的 v1 marker

    async def _read_failure(self, failure_ref: str) -> EDARepairFailure | None:
        """读上轮 EDARepairFailure artifact；损坏/不可解析返回 None（走通用修复提示）。"""
        try:
            return EDARepairFailure.model_validate_json(
                await self._store.get_text(failure_ref)
            )
        except Exception:
            return None

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        """驱动内层 LLM agent 产出报告，收集 report/figures 提交 DataAnalysis 版本。"""
        # 重启后从 rollout 重放的记忆恢复缺失状态（task/workspace/analysis_id/latest_ref）
        self._restore_state(ctx.memory)
        # REVISE follow-up 的输入只有评审原因（非 JSON 任务）；从首次运行的任务恢复。
        try:
            request = json.loads(ctx.input_text or "{}")
        except json.JSONDecodeError:
            # 输入不是 JSON（如纯文本评审原因）→ 空请求
            request = {}
        data_path = request.get("data_path") or self._task.get("data_path")
        if not data_path:
            raise ValueError("DataAgent request requires 'data_path'")
        if self._task:
            request = {**self._task, **request}  # follow-up 沿用首次任务字段
        target = str(request.get("target", ""))
        kind = str(request.get("kind", "eda"))
        self._task = {"data_path": str(data_path), "target": target, "kind": kind}
        # 初次运行必须由 Planner 分配项目内 workspace；follow-up 可复用已恢复路径。
        workspace = _validated_workspace(
            request.get("workspace")
            or (str(self._workspace) if self._workspace is not None else None),
            self._runtime,
        )
        self._workspace = workspace

        # 1. 内层 LLM agent：初始生成或修复 follow-up（Supervisor 拥有修复调度，
        #    每 turn 一次修复 + 一次 canonical 校验，eda-auto-repair §Decisions）
        inner = self._inner_builder(
            "data",
            model=self._model,
            client=self._client,
            workspace=workspace,
            runtime=self._runtime,
        )
        failure_ref = request.get("failure_ref")
        if isinstance(failure_ref, str):
            failure = await self._read_failure(failure_ref)
            prompt = (
                _repair_from_failure(failure)
                if failure is not None
                else "The previous analysis.py run failed. Inspect the current workspace "
                "and edit analysis.py in place to fix the failure; do not run the script "
                "yourself and leave it ready for canonical validation."
            )
        else:
            prompt = json.dumps(
                {"kind": kind, "data_path": str(data_path), "target": target},
                ensure_ascii=False,
            )
        config = getattr(inner, "config", None)
        if isinstance(failure_ref, str) and config is not None:
            inner.config = replace(config, max_tokens=max(config.max_tokens, 8192))
        script = workspace / ANALYSIS_ENTRYPOINT
        while True:
            if ctx.cancel.is_set():
                raise asyncio.CancelledError
            inner_ctx = AgentContext(
                thread=ctx.thread,
                turn=ctx.turn,
                emit=ctx.emit,
                tools=inner.tools,
                cancel=ctx.cancel,
                memory=ctx.memory,
                input_text=prompt,
            )
            await inner.run(inner_ctx)
            if ctx.cancel.is_set():
                raise asyncio.CancelledError
            if script.is_file():
                break
            if not isinstance(failure_ref, str):
                message = f"DataAgent did not produce {ANALYSIS_ENTRYPOINT}"
                failure = EDARepairFailure(
                    attempt=int(request.get("repair_count", 0) or 0),
                    command=[],
                    exit_code=None,
                    stderr=message,
                    failure_signature=_repair_signature(-1, message),
                )
                stored_failure_ref = await self._store.put_text(
                    failure.model_dump_json()
                )
                self._remember_state(ctx.memory)
                outcome = EDAAttemptOutcome(
                    status="repairable_failure",
                    execution_id=str(request.get("execution_id", "")),
                    workspace=str(workspace),
                    repair_count=int(request.get("repair_count", 0) or 0),
                    failure_ref=stored_failure_ref,
                    failure_signature=failure.failure_signature,
                )
                return AgentOutcome(
                    result_ref=await self._store.put_text(outcome.model_dump_json())
                )
            config = getattr(inner, "config", None)
            if config is not None:
                inner.config = replace(
                    config,
                    max_turns=1,
                    max_tokens=max(config.max_tokens, 8192),
                    tool_choice="required",
                )
            prompt = (
                "The previous attempt did not satisfy the output contract. "
                f"Missing files: {ANALYSIS_ENTRYPOINT}. Inspect the existing workspace "
                f"and create {ANALYSIS_ENTRYPOINT} before doing anything else. "
                "Call write_file now; do not reply with text before the tool call."
            )

        # 2. 单次 canonical 校验
        returncode, _stdout, stderr = await _run_script(
            workspace, str(data_path), target
        )
        if returncode != 0:
            failure = EDARepairFailure(
                attempt=int(request.get("repair_count", 0) or 0),
                command=[
                    _workspace_python(workspace),
                    ANALYSIS_ENTRYPOINT,
                    str(data_path),
                    str(target),
                ],
                exit_code=returncode,
                stderr=stderr[-_MAX_FAILURE_STDERR_CHARS:],
                failure_signature=_repair_signature(returncode, stderr),
            )
            failure_ref = await self._store.put_text(failure.model_dump_json())
            self._remember_state(ctx.memory)
            outcome = EDAAttemptOutcome(
                status="repairable_failure",
                execution_id=str(request.get("execution_id", "")),
                workspace=str(workspace),
                repair_count=int(request.get("repair_count", 0) or 0),
                failure_ref=failure_ref,
                failure_signature=failure.failure_signature,
            )
            return AgentOutcome(
                result_ref=await self._store.put_text(outcome.model_dump_json())
            )

        if kind == "role":
            proposal_path = workspace / "dataset_role_proposal.json"
            if not proposal_path.is_file():
                raise RuntimeError(
                    "role inspection did not produce dataset_role_proposal.json"
                )
            proposal = DatasetRoleProposal.model_validate(
                self._normalize_role_proposal(
                    json.loads(proposal_path.read_text(encoding="utf-8"))
                )
            )
            # 角色识别回合也记录状态（task + workspace；无分析链时 analysis_id/latest_ref 为 null）
            self._remember_state(ctx.memory)
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
        # 产物/版本已产出才记录状态 → 外层 turn 失败时 rollback 一并移除
        self._remember_state(ctx.memory)
        # EDA 尝试结果合同（eda-auto-repair §Agent Outcome Contract）：成功 turn 返回
        # 已验证的 EDAAttemptOutcome，Supervisor 经 executor 服务提取 bundle_ref。
        outcome = EDAAttemptOutcome(
            status="succeeded",
            execution_id=str(request.get("execution_id", "")),
            workspace=str(workspace),
            repair_count=int(request.get("repair_count", 0) or 0),
            bundle_ref=self.latest_ref,
        )
        return AgentOutcome(
            result_ref=await self._store.put_text(outcome.model_dump_json())
        )

    @staticmethod
    def _normalize_role_proposal(raw: object) -> dict[str, object]:
        """把 LLM 产出的角色提议 JSON 归一化为 DatasetRoleProposal 合同形状。

        prompt 已显式约束 schema，但 LLM 可能产出角色映射/对象列表等错误形状
        （如 ``role_proposal`` 为 ``{file: role}`` 映射、``data_files`` 为对象
        列表）。平台拥有该确定性合同（contracts.DatasetRoleProposal），先归一到
        ``str``/``list[str]`` 再校验，避免合同校验直接失败。只做形状归一，不
        补充缺失字段（如 ``reasoning`` 缺失仍按合同失败）。
        """
        if not isinstance(raw, dict):
            raise ValueError(
                f"role proposal must be a JSON object, got {type(raw).__name__}"
            )
        role = raw.get("role_proposal")
        if isinstance(role, dict):
            raw["role_proposal"] = ", ".join(f"{k}: {v}" for k, v in role.items())
        elif role is None:
            raise ValueError("role proposal requires a string 'role_proposal'")
        elif not isinstance(role, str):
            raw["role_proposal"] = str(role)
        files = raw.get("data_files")
        if isinstance(files, list):
            normalized: list[str] = []
            for entry in files:
                if isinstance(entry, str):
                    normalized.append(entry)
                elif isinstance(entry, dict):
                    # 常见错误形状：{"path": "...", "role": "..."} → 取 path
                    path = entry.get("path") or entry.get("file") or entry.get("name")
                    normalized.append(
                        str(path)
                        if path is not None
                        else json.dumps(entry, ensure_ascii=False)
                    )
                else:
                    normalized.append(str(entry))
            raw["data_files"] = normalized
        return raw

    async def _collect_files(self, workspace: Path) -> dict[str, str]:
        """把工作区产出固化为 Bundle 文件：analysis.py + report.md + figures（可选 role proposal）。

        DataAnalysis Bundle 合同要求恰一个根 ``report.md`` + 至少一张 ``figures/``
        图（VersionedBundle 校验）；``analysis.py`` 一并入包保证 EDA 可复现；
        role proposal JSON 作为同 Bundle 内的补充产物，proposal/EDA 两阶段在评审侧
        分流（role 读 proposal JSON、EDA 读 report）。
        """
        report_path = workspace / "report.md"
        if not report_path.is_file():
            raise RuntimeError("analysis script did not produce report.md")
        files = {
            "analysis.py": await self._store.put_text(
                (workspace / ANALYSIS_ENTRYPOINT).read_text(encoding="utf-8")
            ),
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
