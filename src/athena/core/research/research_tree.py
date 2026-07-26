import json
import math
import warnings
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from athena.core.gitutils.workspace import GitWorkBranch
from athena.core.schemas import ArtifactRef, Hypothesis, CommitHash, ExperimentPlan

INTERVAL_MIDDLE = "=" * 15


class Experiment(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    commit: CommitHash
    hypothesis: Hypothesis
    plan: ExperimentPlan
    metric_type: str  # 介绍当前的指标是啥： AUC？ logloss？
    result: str | float
    gitwork: GitWorkBranch

    def result_as_float(self) -> float | None:
        try:
            value = float(self.result)
        except (TypeError, ValueError):
            # 实验结果非数字（如 "N/A", "inf" 文本）→ 返回 None

            warnings.warn(
                f"实验结果无法转换为数字：{self.result!r}",
                RuntimeWarning,
                stacklevel=2,
            )
            return None

        if not math.isfinite(value):
            warnings.warn(
                f"实验结果不是有限数字：{self.result!r}",
                RuntimeWarning,
                stacklevel=2,
            )
            return None

        return value

    # 原 这里需要进一步修改
    def to_prompt(self) -> str:
        """
        将当前实验信息转换为适合放入 prompt 的文本。
        """
        PROMPT = """
{hypothesis}

最后得到的指标{metric_type}为: {result}
        """
        return PROMPT.format(
            hypothesis=self.hypothesis.to_prompt(),
            metric_type=self.metric_type,
            result=str(self.result),
        )


class ResearchTreeNode(BaseModel):
    """
    研究树中的单个节点。
    """

    exp: Experiment
    id: str
    parent_id: str | None = None
    children_ids: list[str] = Field(default_factory=list)

    # 原 get_exp_info
    def to_prompt(self) -> str:
        """
        获取当前节点对应的实验 prompt。
        """
        exp_prompt = self.exp.to_prompt()
        return f"""
==> Research Node
{exp_prompt}
"""


class ResearchTreeNodes:
    """
    统一管理 ResearchTreeNode。

    nodes 使用 dict 存储，因此可以通过节点 ID 进行近似 O(1) 查询。
    """

    def __init__(self) -> None:
        self.nodes: dict[str, ResearchTreeNode] = {}

    # 原 get_new_node
    def create_node(
        self,
        exp: Experiment,
        parent_id: str | None = None,
    ) -> ResearchTreeNode:
        """
        创建节点并注册到节点表中。

        如果指定了父节点，会同时更新父节点的 children_ids。
        """
        if parent_id is not None and parent_id not in self.nodes:
            raise KeyError(f"父节点不存在：{parent_id}")

        node_id = uuid4().hex

        # UUID 冲突概率极低，但这里仍显式保证 ID 不重复。
        while node_id in self.nodes:
            node_id = uuid4().hex

        node = ResearchTreeNode(
            id=node_id,
            parent_id=parent_id,
            children_ids=[],
            exp=exp,
        )

        self.nodes[node_id] = node

        if parent_id is not None:
            self.nodes[parent_id].children_ids.append(node_id)

        return node


class ResearchTree:
    def __init__(self) -> None:
        self._nodes = ResearchTreeNodes()

    def get_node_by_id(self, node_id: str) -> ResearchTreeNode:
        try:
            return self._nodes.nodes[node_id]
        except KeyError as exc:
            # 节点 ID 不在内部字典中 → 包装为描述性 KeyError
            raise KeyError(f"研究树中不存在节点：{node_id}") from exc

    def get_prompt(
        self,
        node: ResearchTreeNode | str,
    ) -> str:
        """
        获取从叶子节点到根节点的实验路径，并在路径中首次出现分叉时，
        补充该分叉点的其他兄弟实验的提示。
        返回的提示顺序为从叶子节点到根节点（即从当前实验回溯到根实验）。
        """
        node_id = node.id if isinstance(node, ResearchTreeNode) else node
        current_node = self.get_node_by_id(node_id)

        visited_ids: set[str] = set()
        prompt_parts = []  # 收集各段提示，最后拼接

        # 标记是否已经处理过分叉（只处理第一个分叉）
        branch_handled = False

        while True:
            if current_node.id in visited_ids:
                raise RuntimeError(
                    f"研究树中检测到循环引用，节点 ID：{current_node.id}"
                )
            visited_ids.add(current_node.id)

            # 添加当前节点的提示
            prompt_parts.append(INTERVAL_MIDDLE + current_node.to_prompt())

            # 如果到达根节点，则退出
            if current_node.parent_id is None:
                break

            parent_node = self.get_node_by_id(current_node.parent_id)

            # 如果尚未处理过分叉，且当前父节点有多个子节点，则记录该分叉点
            if not branch_handled and len(parent_node.children_ids) > 1:
                branch_handled = True
                # 添加分叉说明
                prompt_parts.append(
                    INTERVAL_MIDDLE + "对于当前实验，我们做出了如下大分支："
                )
                # 添加所有兄弟节点的提示（排除当前节点）
                for sibling_id in parent_node.children_ids:
                    if sibling_id == current_node.id:
                        continue
                    sibling_node = self.get_node_by_id(sibling_id)
                    prompt_parts.append(sibling_node.to_prompt())
                prompt_parts.append(
                    INTERVAL_MIDDLE
                    + "要求你的下一步假设生成和修改生成必须极大的规避上述大分支"
                )

            # 向上移动到父节点
            current_node = parent_node

        # 拼接所有片段，顺序为叶子到根
        return "\n".join(prompt_parts)

    def create_node(
        self,
        exp: Experiment,
        parent_id: str | None = None,
    ) -> ResearchTreeNode:
        return self._nodes.create_node(
            exp=exp,
            parent_id=parent_id,
        )

    # TODO: 还需要保存这个树的内容，从而可以达到断点续传。这里不允许codex完成


if __name__ == "__main__":
    # uv run python -m athena.core.research.research_tree
    def make_experiment(
        name: str,
        change: str,
        result: float,
        commit: CommitHash,
    ) -> Experiment:
        return Experiment(
            commit=commit,
            hypothesis=Hypothesis(
                statement=f"{change} 可能提升验证集 AUC",
                intervention=change,
                expected_effect="验证集 AUC 提升",
                status="SUPPORTED",
            ),
            plan=ExperimentPlan(
                kind="ablation",
                change=change,
                run_config_ref=f"artifact://runs/{name}",
                budget={"epochs": 10},
                acceptance_rule="AUC 不低于父实验",
            ),
            metric_type="AUC",
            result=result,
            gitwork=GitWorkBranch(
                path=f"C:/athena-demo/{name}",
                branch=f"demo/{name}",
                base_commit=commit,
            ),
        )

    tree = ResearchTree()

    root = tree.create_node(make_experiment("baseline", "训练基线模型", 0.72, "0" * 40))
    leaf = tree.create_node(
        make_experiment("standardize", "标准化数值特征", 0.75, "1" * 40),
        parent_id=root.id,
    )
    leaf2 = tree.create_node(
        make_experiment("standardize", "标准化数值特征", 0.75, "1" * 40),
        parent_id=root.id,
    )

    print(f"根节点：{root.id}")
    print(f"叶子节点：{leaf.id}")
    print(tree.get_prompt(leaf.id))
