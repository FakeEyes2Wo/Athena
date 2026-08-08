"""协议契约层 — DTO、方法名常量、错误码。

所有公开 DTO 继承 ``ProtocolModel``（frozen + forbid extra）。
字段重命名、枚举值和 discriminator 属于版本化协议，不随 Python 内部重构改变。
"""

from enum import IntEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProtocolModel(BaseModel):
    """所有协议 DTO 的基类：不可变，拒绝未知字段。"""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ErrorCode(IntEnum):
    """协议错误码枚举，对齐 JSON-RPC 风格。"""

    INVALID_ARGUMENT = -32602
    NOT_FOUND = -32601
    FAILED_PRECONDITION = -32000
    NOT_INITIALIZED = -32002
    ALREADY_INITIALIZED = -32003
    DUPLICATE_REQUEST_ID = -32004
    OVERLOADED = -32005
    CLOSED = -32006
    INTERNAL = -32603


_EXCEPTION_MAP: dict[type[Exception], ErrorCode] = {
    ValueError: ErrorCode.INVALID_ARGUMENT,
    KeyError: ErrorCode.NOT_FOUND,
    RuntimeError: ErrorCode.FAILED_PRECONDITION,
}


def map_exception_to_error_code(exc: Exception) -> ErrorCode:
    """将 Python 异常映射为协议错误码。CancelledError 继承 BaseException，不经过此函数。"""
    for exc_type, code in _EXCEPTION_MAP.items():
        if isinstance(exc, exc_type):
            return code
    return ErrorCode.INTERNAL


class RpcError(ProtocolModel):
    """协议错误——仅含稳定通用文案，不含异常消息/prompt/traceback。"""

    code: int
    message: str
    data: dict[str, Any] | None = None


def rpc_error(
    code: ErrorCode, message: str, data: dict[str, Any] | None = None
) -> RpcError:
    """创建携带指定错误码的 RpcError 实例。"""
    return RpcError(code=code.value, message=message, data=data)


class Method:
    """协议方法名常量——所有 RPC 方法和通知的标识符。"""

    INITIALIZE = "initialize"
    INITIALIZED = "initialized"
    THREAD_START = "thread/start"
    THREAD_FORK = "thread/fork"
    TURN_START = "turn/start"
    TURN_INTERRUPT = "turn/interrupt"
    THREAD_SUBSCRIBE = "thread/subscribe"
    THREAD_UNSUBSCRIBE = "thread/unsubscribe"
    SERVER_SHUTDOWN = "server/shutdown"
    ITEM_APPROVAL_REQUEST = "item/approval/request"
    ITEM_USER_INPUT_REQUEST = "item/userInput/request"
    TOOL_CALL_REQUEST = "tool/call/request"

    @classmethod
    def is_control_method(cls, method: str) -> bool:
        """该方法是否为控制方法（initialize/shutdown）。"""
        return method in (cls.INITIALIZE, cls.SERVER_SHUTDOWN)


class RequestEnvelope(ProtocolModel):
    """客户端请求信封。"""

    request_id: int = Field(ge=0)
    method: str
    params: dict[str, Any] | None = None


class ResponseEnvelope(ProtocolModel):
    """服务端响应信封。"""

    request_id: int
    result: dict[str, Any] | None = None
    error: RpcError | None = None


class ClientNotification(ProtocolModel):
    """客户端单向通知（无响应）。"""

    method: str
    params: dict[str, Any] | None = None


class ServerRequest(ProtocolModel):
    """服务端发起的请求（如审批）。"""

    server_call_id: str
    method: str
    params: dict[str, Any]


class EventNotification(ProtocolModel):
    """事件通知——通过订阅推送到客户端。"""

    subscription_id: str
    thread_id: str
    turn_id: str | None
    sequence: int = Field(ge=1)
    kind: str
    event_ref: str
    data: dict[str, Any] | None = None


ServerEvent = ServerRequest | EventNotification


class ThreadStartParams(ProtocolModel):
    """创建 Thread 的请求参数。"""

    session_id: str
    context_ref: str


class TurnStartParams(ProtocolModel):
    """启动 Turn 的请求参数。"""

    thread_id: str
    request_ref: str


class TurnInterruptParams(ProtocolModel):
    """中断 Turn 的请求参数。"""

    thread_id: str
    turn_id: str
    reason: str


class ThreadForkParams(ProtocolModel):
    """Fork Thread 的请求参数。"""

    thread_id: str
    after_turn_id: str | None = None


class ThreadSubscribeParams(ProtocolModel):
    """订阅 Thread 事件的请求参数。"""

    thread_id: str
    after_sequence: int = 0


class ThreadUnsubscribeParams(ProtocolModel):
    """取消订阅的请求参数。"""

    subscription_id: str


class UserInputQuestion(ProtocolModel):
    """请求用户输入的一个问题（对齐 Codex ``RequestUserInputQuestion``）。

    出现在 Server 发出的 ``item/userInput/request`` 请求的 ``questions`` 数组中。
    """

    id: str
    question: str
    header: str = ""
    options: list[dict[str, str]] | None = None
    is_other: bool = False
    is_secret: bool = False


class ServerRequestReply(ProtocolModel):
    """Client 对 ServerRequest 的回复 DTO（协议层）。"""

    server_call_id: str
    result: dict[str, Any] | None = None
    error: RpcError | None = None


class ThreadStartedResult(ProtocolModel):
    """Thread 创建成功的响应结果。"""

    thread_id: str


class TurnStartedResult(ProtocolModel):
    """Turn 启动成功的响应结果。"""

    turn_id: str


class ThreadForkedResult(ProtocolModel):
    """Thread Fork 成功的响应结果。"""

    thread_id: str


class SubscribedResult(ProtocolModel):
    """订阅成功的响应结果。"""

    subscription_id: str


class InterruptedResult(ProtocolModel):
    """中断成功的响应结果。"""

    turn_id: str
    status: str = "interrupted"


ResponseResult = (
    ThreadStartedResult
    | TurnStartedResult
    | ThreadForkedResult
    | SubscribedResult
    | InterruptedResult
    | dict[str, Any]
)
