> Status: historical
> Last verified: 2026-07-30
> 早期设计副本，仅供考据；不得作为当前接口或实现依据。

我们的整套框架都需要异步
同时我们的prompt都需要使用英文。以及pydantic里面的description都是英文。因为互联网上英文语料最多
我们可以在后处理阶段将英文翻译成中文


对于下面设计中所有需要评分的部分，我们都需要写一个rubric，无论是AI写还是自己写。


## 问题定义

我们希望构建这样一个系统：
输入为 优化目标G和数据集D
输出为 可运行的代码 Code和指标

构建的自动化系统可以研究多个领域的内容，第一阶段为
机器学习，
第二阶段为天文学数据处理


### 问题分解

- 任务解析
    这一部分需要解析我们输入任务的信息，包括
    - 任务类型
        - 分类，回归，聚类
        - 异常检测，时间序列预测
        - 降维，排序或推荐
        - 图像、文本等非结构化数据任务
    - 数据类型
        - tabular
        - text
        - image
        - 时间序列
    - 预测目标
        - 目标变量或标签列
        - 需要预测的物理量或业务指标
        - 单目标任务或多目标任务
    - 优化目标
        - 分类指标：Accuracy、Precision、Recall、F1、AUC、Log Loss
        - 回归指标：MAE、MSE、RMSE、(R^2)
        - 聚类指标：Silhouette Score、Calinski-Harabasz Score
        - 资源指标：运行时间、内存占用、模型大小、推理延迟
        - 多目标指标：模型效果、计算成本和可解释性之间的权衡
    - 约束条件
        - 可用计算资源
        - 运行时间限制
        - 是否允许使用深度学习
        - 是否要求模型可解释
        后面的要求受限于实验环境暂不完成
        - 模型大小限制
        - 是否要求支持 CPU、GPU 或分布式运行

这里解析我们可以返回
```python
from pydantic import BaseModel
class TaskMetaData(BaseModel):
    data_type: List[str] = Field(
        default_factory=[],
        description="数据类型列表，表示我们输入的数据里面有哪些类型的数据，如 tabular、text、image、time_series"
    )
    target_vars: list[str] = Field(
        default_factory=list,
        description="目标变量名称，无监督任务为空"
    )
    primary_metric: str = Field(
        description="主要优化指标名称，如 F1、AUC、RMSE"
    )
    other_metrics: list[str] = Field(
        default_factory=list,
        description="其他评价指标名称"
    )
    constraints: list[str] = Field(
        default_factory=list,
        description="任务约束，如禁止深度学习、运行时间不超过30分钟"
    )


class DataCard(BaseModel):
    """
    用来记录数据集类型
    """
    data_type:""

```

目标变量或标签列
需要预测的物理量或业务指标
单目标任务或多目标任务

- 数据分析  如何分析提供的数据集。
提供的数据集来自于真实世界真实环境。所以需要大量的数据清洗。如离群点去除，缺失值填充。
如果数据量很大，那么我们应该随机抽样进行分析。
同时我们应该更换种子，随机抽样3次，最后得到的抽样数据集。然后在抽样数据集上进行分析。


- 数据建模 根据我们数据分析的结果，进行模型选择和模型设计。
首先，如果数据量很大，我们可以选择机器学习模型和传统深度学习模型。

如果是机器学习模型，那么我们应该尝试进行特征工程，或者使用深度学习Encoder。
这里需要交给AI自行决定


    - 模型拟合
        - 深度学习模型拟合
        - 机器学习模型拟合


##
Kaggle 交互的Agent 工具
### utils
这里的框架还没有决定，所以在这里记录的时候只能写出功能
我们需要能够单轮对话的 不保存对话历史的function


### 实验记录树
```python
from abc import ABC
class ExpCkpt(ABC):
    """
    记录一个实验的最小单位
    必须要选取做实验需要记录的最必要的信息

    这里整理我们需要做出来的方面
    如  机器学习方面
        这个方面包括  EDA等等, 比方对于Kaggle竞赛， 再比如说机器学习问题的探索

        还有竞赛有关的天文学方面
        这个方面包括
        1、基于活动区磁场数据，提炼可量化其复杂度的参数，建立参数与爆发强度或频次之间的定量关系，并基于 JW-SSD 数据集构建太阳黑子的表征及分类方法；
        2、基于多模态时序数据，探索数据特征与爆发活动之间的时序关联，并基于 JW-FD 数据集完成太阳耀斑爆发的精细化预测；
        3、基于公开 TESS 光变曲线，自动识别恒星耀发，并统计其形态、能量和频率分布。

    还需要保存当次实验的代码，环境等Snapshot。
    但是环境这个除非有减少某个pkg的，否则不保存。因为增加pkg不会影响之前的环境。
    """
    def __init__(self):
        actions: List[str] # 用来记录当前实验使用的所有机制
        delta_action: "str" # 当前实验增加的机制
        result: Literal[float,str]# 这里的指标还不确定   我们做两套内容，一套用在机器学习上，一套用在那个天文数据集上。

    def get_prompt(self)->str:


class ExpCkptTree(ABC):
    """"
    需要与数据库进行交互，方便我们进行实验断点续传。那么这里有第二个问题，如何判断是同一个实验。可以维护一个 实验Session和实验名称的对应关系，方便断点继续
    同时这个Tree需要有沿着路径实验消融的机制。方便我们进行多轮消融实验找到最优路径。
    同时需要能够异步访问。

    """
    def __init__(self):

```
下面这个是AI_Scientist_v2里面的关于实验的记录。可以参考他的设计模式
```json
{
"Title": "Enhancing Compositional Generalization in Neural Networks via Compositional
Regularization",
"Short Hypothesis": "Introducing a compositional regularization term during training can
encourage neural networks to develop compositional representations, thereby improving their
ability to generalize to novel combinations of known components.",
"Experiments": [
"Implement the compositional regularization term and integrate it into the loss function of
standard sequence-to-sequence neural network architectures with attention mechanisms.",
"Train models on synthetic datasets like SCAN and COGS, evaluating performance on
compositional generalization tasks with and without the regularization term.",
"Apply the method to real-world tasks such as machine translation using the IWSLT dataset
and semantic parsing with the GeoQuery dataset, assessing improvements in generalization to
new language constructs.",
"Analyze the learned representations by visualizing embedding spaces and utilizing
compositionality metrics to assess how the regularization affects internal
representations.",
"Conduct ablation studies to determine the impact of different strengths of the
regularization term, identifying the optimal balance between enforcing compositionality and
maintaining overall performance.",
"Compare the proposed method against other approaches aimed at improving compositional
generalization, such as meta-learning techniques and specialized architectures."
],
}


```

我们在进行实验的时候应该可以接入Qcoder，让Qcoder来完成代码。为了方便调试，
应该将Human in Loop设置为一个可选项。  Human in Loop是 每次Agent完成一件事就交由用户判断，像claude code一样。
Debug设置为一个可选项。
Debug为 每次和大模型交互即交给用户判断

```bash
uv add qoder-agent-sdk
有Qcoder sdk

这里需要有一点验证实验
```
AgentMonitor  在Qcoder完成过程中我们需要监控完成过程，防止死锁 这个Monitor的过程需要设计

对于AI调用工具，我们需要一个线程管理器。项目需要能够对于sub Agent，tool call等调用新开线程，同时维护里面的优先级关系。

```python
"""
关于每个线程应该保留的信息有哪些需要进一步设计。

"""

# AthenaThread.py
class AthenaThread():
    # TODO:一个Thread 应该包含这个线程应该做的任务，同时应该返回的信息。  不过我还是应该参考codex的设计。这里还需要查看Codex的源码
    #

class BackgroundInfo():


# ThreadManger.py
class ThreadManager():


```

对于我们的工作流设计，我们采取
Supervisor的方式进行设计。

然后剩下的按照
### DataAnalysis
对于这部分我们应该尽可能对数据进行清洗。同时需要检查数据清洗的效果，同时对数据进行一个全面的EDA。

这里是需要Loop进行的。
实际上我们Agent在保存这里内容的时候也应该使用树这种方式。

这里应该保存一个csv或者其他类型的表格，同时允许Agent在目录下生成其他辅助资料或者分析代码

./data_analyze
./data_analyze/feature_process.csv
这里的feature_process.csv就是我们记录每一列分别进行了怎样处理的文件

我们通过**Agent工具** 注意，这里一定要通过工具交互。因为某些Dataset过于庞大同时表头过于丰富。这种类型的Dataset会挤占上下文。

同时我们处理之前需要对原始数据保存一个副本。

### IdeaGeneration

#### Paper Reading


这个需要生成HypoTree

```python
from pydantic import BaseModel
class Hypothesis(BaseModel):
    def __init__(self):


class HypoTree(RecordTree,ABC):
    def __init(self):


```
这里的HypoTree和上面的ExpCkptTree有相似
所以应该使用多态，需要创建一个Tree的基类。记录树 RecordTree。
这个基类需要传入的是MetaData。如Hypothesis，ExpCkpt

同时这个代理需要进行RAG查询。
他需要查看的是


相关领域的论文，相关的blog，相关的技术报告
这里可以参考Co_Scientist论文里面的做法。

这里还需要一个基础设施:
相关领域论文检索并输入IdeaGeneration


同时我们还应该维护一个
```python
HypoPriList:List
# 这个List是一个优先队列List。我们使用ELO算法来对里面的Hypothesis进行一个排序，选出里面优先级最高的Hypo来进行实验验证。
# 这里在做的时候，在注释里面引用Co Scientist
```
#### Ranking
ELO为每个候选假设进行一个排序，最后做出最好的假设

注意，这里的排序是纯算法的，不应该引入LLM的交互。这一部分主要是为功能提效。

### Proximity
这一部分用来绘制究
假设和研究提案之间的相似度，并结合具体研究目标构建一个 proximity graph


###
