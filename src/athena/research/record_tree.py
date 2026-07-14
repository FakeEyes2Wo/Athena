"""待实现：假设与实验记录共用的异步 ``RecordTree`` 基类。

树以 ``RecordNode`` 的节点标识、父标识、状态和 payload artifact 为最小公共表示，
并由具体实现提供新增、读取、子节点、路径、状态更新和分支恢复能力。HypoTree 与
ExpCkptTree 应以多态复用该能力；传入的节点 metadata 类型、存储后端和失败回退接口
仍待细化，避免在此过早抽象出第二套事实存储。
"""
