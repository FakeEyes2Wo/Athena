"""实验检查点树的模型与持久化接口。

每个实验从可运行、可评估的 baseline 分支演化而来。代码快照由 Git commit
标识，运行配置与结果由不可变 artifact 标识；树用于恢复、分支、比较与沿路径
提取已采用的措施，不创建第二套代码版本格式。
"""

from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel, Field


class ExpCkpt(BaseModel):
    """实验树中的最小节点。

    ``change`` 仅描述相对父实验的主变更；完整措施链通过 ``ExpCkptTree.path``
    获取。实验结果必须由冻结评估器产生，并保留版本化 rubric 的逐项证据。
    """
    id: str = Field(description="Unique experiment identifier.")
    parent_id: str | None = Field(default=None, description="Parent experiment identifier.")
    change: str = Field(description="The main change relative to the parent experiment.")
    commit: str = Field(description="Git commit used by the experiment.")
    run_ref: str = Field(description="Reference to the immutable run configuration.")
    result_ref: str | None = Field(default=None, description="Reference to the evaluated experiment result.")
    status: Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"] = Field(default="PENDING", description="Experiment execution status.")


class ExpCkptTree(ABC):
    """异步实验树存储契约。

    实现需要支持断点续传、等价 ``run_ref`` 去重、从 baseline 到目标实验的路径
    查询，以及失败节点后的重新分支。具体回退方法和持久化后端仍待实现，不在此
    抽象接口中预设。
    """
    @abstractmethod
    async def add(self, node: ExpCkpt) -> None:
        """新增检查点，并校验其父节点与 baseline 分支关系。"""
    @abstractmethod
    async def get(self, node_id: str) -> ExpCkpt:
        """按标识读取单个实验节点。"""
    @abstractmethod
    async def get_children(self, node_id: str) -> list[ExpCkpt]:
        """读取指定节点的直接分支，用于并行实验与失败后重试。"""
    @abstractmethod
    async def path(self, node_id: str) -> list[ExpCkpt]:
        """返回从 baseline 到目标节点的措施与结果链。"""
    @abstractmethod
    async def update(self, node_id: str, *, status: str | None = None, result_ref: str | None = None) -> None:
        """仅更新执行状态和结果引用，不改写既有运行配置或代码快照。"""
    @abstractmethod
    async def find_by_run_ref(self, run_ref: str) -> ExpCkpt | None:
        """查找等价运行，避免对同一不可变配置重复执行。"""
