"""待实现：小型结构化事实和事件的唯一持久化层。

StateStore 保存 Session 阶段、Thread/Turn 生命周期、实验树节点、审批和事件索引；
不直接保存数据集、模型、长上下文或报告正文。此类大对象统一落入 ArtifactStore，
长线程历史只在此层保存其 ``context_ref``。
"""
