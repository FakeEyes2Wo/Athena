"""InitAgent — PREPARE 第一环：task understanding → 生成 eval.py。

外层确定性编排：构造内层 LLM ReAct agent（prompt=``init_agent.md`` + 通用工具，
cwd=workspace）→ 运行（LLM 按 prompt 用 ``read_file``/``shell_command`` 探查数据集，
写出固定名 ``task_understanding.md`` 与 ``eval.py``）→ 读取产物 → ``compile``
语法自检 → 打包为 Artifact payload。任务分类/主指标由 LLM 按 prompt 判定，
不再有确定性 ``_classify_task``/``_default_eval_script`` 代码路径。evaluation
自此只依赖 prompt 约束的 ``eval.py``——Athena 侧不再静态构造评估逻辑。

请求 payload（JSON）：

- ``data_path``：数据集 CSV 路径（必需）。
- ``target``：目标列名（必需）。

产出 payload：``{"task_understanding": str, "eval_script": str,
"eval_workspace": str, "eval_metadata": {"entrypoint": str}}`` 写入
ArtifactStore，``AgentOutcome.result_ref`` 指向该 Artifact。调用方
（ResearchRuntime 的 Supervisor）从响应 JSON 读取 ``eval_script`` 提交
eval_spec_ref 事实，并把 ``eval_workspace`` 交给 ``scripts.freeze`` 冻结为
``eval_bundle_ref``（真实 frozen bundle，供可信 evaluator 消费）。

eval.py 契约（由 prompt 约束，自包含，仅用标准库）：读
当前目录的 ``predictions.csv``（``__athena_row_id``, ``prediction``）与
``labels.csv``（``__athena_row_id``, ``target``），按 row_id 对齐后计算主
指标，接收 ``--request``/``--output`` 并把
``{"primary": ..., "metric": ...}`` 写入 output 文件。
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athena.agents.prompt_agent import build_llm_agent
from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.agent.runtime import Agent, BaseAgent
from athena.core.contracts import ArtifactStore

if TYPE_CHECKING:
    from athena.execution.runtime import ExecutionRuntime

EVAL_ENTRYPOINT = "eval.py"


def _validated_workspace(raw: object, runtime: object | None) -> Path:
    if not isinstance(raw, str) or not raw:
        raise ValueError("InitAgent request requires project-local 'workspace'")
    workspace = Path(raw).resolve()
    if runtime is not None:
        project_root = Path(getattr(runtime, "project_root")).resolve()
        if not workspace.is_relative_to(project_root):
            raise ValueError(f"workspace outside project root: {workspace}")
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


class InitAgent(BaseAgent):
    """task understanding Agent：LLM 产出固定格式报告 + eval.py，打包 Artifact。

    ``inner_builder`` 测试接缝与 DataAgent 一致：缺省用
    :func:`~athena.agents.prompt_agent.build_llm_agent`；单测注入 fake provider
    预写 ``task_understanding.md`` + ``eval.py``，避免依赖真实 LLM API。
    """

    name = "init-agent"
    description = "理解任务并产出 task-understanding 报告 + eval.py"

    def __init__(
        self,
        store: ArtifactStore,
        *,
        model: str,
        client: Any = None,
        inner_builder: Callable[..., Agent] | None = None,
        runtime: "ExecutionRuntime | None" = None,
    ) -> None:
        self._store = store
        self._model = model
        self._client = client
        self._inner_builder = inner_builder or build_llm_agent
        self._runtime = runtime

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        """驱动内层 LLM agent 产出报告 + eval.py，收集后打包 Artifact payload。"""
        request = json.loads(ctx.input_text or "{}")
        data_path = request.get("data_path")
        if not data_path:
            raise ValueError("InitAgent request requires 'data_path'")
        target = request.get("target")
        if not target:
            raise ValueError("InitAgent request requires 'target'")

        # 1. 内层 LLM agent：按 init_agent.md prompt 写 task_understanding.md + eval.py
        workspace = _validated_workspace(request.get("workspace"), self._runtime)
        inner = self._inner_builder(
            "init",
            model=self._model,
            client=self._client,
            workspace=workspace,
            runtime=self._runtime,
        )
        inner_ctx = AgentContext(
            thread=ctx.thread,
            turn=ctx.turn,
            emit=ctx.emit,
            tools=inner.tools,
            cancel=ctx.cancel,
            memory=ctx.memory,
            input_text=json.dumps(
                {"data_path": str(data_path), "target": target}, ensure_ascii=False
            ),
        )
        await inner.run(inner_ctx)

        # 2. 收集固定名产物：task_understanding.md（证据报告）+ eval.py + labels.csv。
        #    labels.csv 是冻结的评估真值（PREPARE 冻结进 eval bundle，候选不可伪造）。
        report = (workspace / "task_understanding.md").read_text(encoding="utf-8")
        eval_script = (workspace / "eval.py").read_text(encoding="utf-8")
        compile(eval_script, EVAL_ENTRYPOINT, "exec")
        if not (workspace / "labels.csv").is_file():
            raise RuntimeError("InitAgent did not produce labels.csv")

        # 3. 保证工作区是可冻结的 uv 项目：缺失 pyproject.toml 时补最小清单
        #    （打包元数据，非评估语义；真实依赖由 LLM 脚本契约声明）。
        if not (workspace / "pyproject.toml").is_file():
            (workspace / "pyproject.toml").write_text(
                "[project]\n"
                "name = 'eval'\n"
                "version = '0.1.0'\n"
                "requires-python = '>=3.11'\n"
                "dependencies = []\n",
                encoding="utf-8",
            )

        payload = {
            "task_understanding": report,
            "eval_script": eval_script,
            "eval_workspace": str(workspace),
            "eval_metadata": {"entrypoint": EVAL_ENTRYPOINT},
        }
        result_ref = await self._store.put_text(json.dumps(payload, ensure_ascii=False))
        return AgentOutcome(result_ref=result_ref)
