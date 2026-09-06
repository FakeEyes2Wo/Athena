# GUI 控制器接线与有限环境修复

这项接线不提供外部审核服务本身，也不会以本地文件或内存对象替代生产审核服务。
控制器仍须提供 `BaselineAuthorityStore` 的实际实现。普通 `python -m gui_gateway`
未配置该组件时，可以打开界面，但不能启动需要可信基线审核的研究。

## 最小接入方式

宿主程序调用 `start_server(controller_factory=...)`。工厂按规范化项目路径和会话 ID
返回 `ControllerCapabilities`，用于初始化及工作区切换；不要为不同项目复用同一份审核状态。
直接执行 `python -m gui_gateway` 时，可由控制器设置仅含导入引用的
`ATHENA_CONTROLLER_FACTORY=deployment_controller:controller_factory`；凭据仍只在
`deployment_controller` 进程配置中。变量缺失时 GUI 使用本机权限的持久化 authority；
它适用于单机演示，不防御同一 OS 用户的其他进程。设置
`ATHENA_AUTHORITY_MODE=ssh` 会明确失败（SSH authority 尚未实现），不会回退为本机模式。
可用 `ATHENA_AUTHORITY_LOCAL_ROOT` 改写本机 authority 的控制器私有根目录；该路径不会
发送到浏览器或 Agent。

```python
import asyncio
from gui_gateway.__main__ import start_server
from gui_gateway.controller import ControllerCapabilities

# 以下两个对象由部署控制器实现，不是 Athena 自带的假服务。
from deployment_controller import authority_for, restart_registered_authority


def controller_factory(project_root, session_id):
    return ControllerCapabilities(
        baseline_authority=authority_for(project_root, session_id),
        repair_actions={"restart_service": restart_registered_authority},
    )


async def main():
    server, _ = await start_server(controller_factory=controller_factory)
    try:
        await asyncio.Future()
    finally:
        server.close()
        await server.wait_closed()


asyncio.run(main())
```

这是宿主接线示例，`deployment_controller` 必须由部署方提供，不能直接照搬后宣称服务已部署。
服务凭据留在控制器内，不通过 GUI 设置、请求参数、模型提示词或工具返回值传递。
独立审核存储及凭据边界仍是部署条件；本机同账号任意 shell 不构成防篡改隔离。

## General Agent 的权限

自动环境修复使用单独的受限 General 角色，不复用带项目文件工具和 shell 的普通 General。
它只能选择控制器预登记的无参数操作；没有任意命令、路径、代码修改、数据读取或权限修改工具。
操作回调应只完成一个固定环境动作，并返回 `None`。不要把 shell 字符串或服务输出当作回调结果。

最小实现只处理启动前审核服务的连接/超时故障。认证拒绝、缺少组件、审核证明不合法及
代码异常不交给 Agent 修复。运行中其它错误仍由原流程报告，不作无边界自动修复。
每次动作后由平台重新检查审核服务；最多两次修复尝试，Agent 总结不作为通过依据。

## 运行和验收边界

- 工厂接线及合成故障测试可以验证机制，不等于实际服务上线。
- 未配置审核组件时应在研究开始前报错，避免先消耗一轮 EDA。
- 冻结模型、审核记录、数据划分和 FINAL 访问范围不因环境修复发生改变。
- 外部客户端应把临时连接问题表现为 `ConnectionError` 或 `TimeoutError`；不要把
  认证或内容校验失败错误地包装成可重试故障。
- 接入后还须用实际部署的服务验证恢复；本文件不声称 TESS 已完成端到端运行。
