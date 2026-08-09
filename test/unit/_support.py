"""项目 Composition Root 测试支持：带真实 model 的 make_project helper。"""

from pathlib import Path

from athena.core.agent import settings
from athena.research.project_runtime import ProjectRuntime


def make_project(tmp_path: Path) -> ProjectRuntime:
    """带 DeepSeek model 的组合根（LLM 相关测试的入口）。

    ``register_defaults`` 要求显式传 ``model``（无 model 报错）；测试统一经
    此 helper 用 ``settings.model_name()`` 装配。各 agent 本任务仍为确定性
    实现，model 仅暂存。
    """
    project = ProjectRuntime(tmp_path)
    project.register_defaults(model=settings.model_name())
    return project
