```python
from abc import ABC, abstractmethod

  import math
  import warnings

  from pydantic import BaseModel

  from athena.core.gitutils.workspace import GitWorkBranch
  from athena.core.schemas import ExperimentPlan, Hypothesis


EXPERIMENT_PROMPT="""

当前的假设是：
结果是：
评价结果有效，有效原因是：


"""

  class Experiment(BaseModel):
      hypothesis: Hypothesis
      plan: ExperimentPlan
      result: str | float
      gitwork: GitWorkBranch

      def result_as_float(self) -> float | None:
          try:
              value = float(self.result)
          except (TypeError, ValueError):
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


        def get_propmt(self)->str:



# 这个需要一个



class ResearchTreeNode(ABC):
    # 实验信息
    exp: Experiment

    # 树相关
    id:str
    parent_id:Optional[str]
    children_id:list[str]

    def get_exp_info()->str:
        从exp里面获取当前节点有关的prompt信息。


class ResearchTreeNodes(ABC):

    def __init__(self):
        self.nodes = 这里应该直接用hash表查询
        同时生成的key不能重复。 这里还是用一个类来管理
    def get_new_node(self):
        同时生成的key不能重复。 这里还是用一个类来管理

class ResearchTree(ABC):
    _NODES:ResearchTreeNodes


    def __init__():


    def get_node_by_id(self,node_id)->RearchTreeNode:
        pass

    def get_prompt(self,node|node_id):
        判断是ResearchTreeNode还是 id

        我们取id

        node = self.get_node_by_id(id)
        for node.parent_id is not None:


        这里是根据当前的Node来获取节点的：

    def gen_node()->RearchTreeNode:
        return self._NODES.get_new_node()








```
