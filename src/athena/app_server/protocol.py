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


# 错误码


class ErrorCode(IntEnum):
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
    return RpcError(code=code.value, message=message, data=data)


# 方法名常量


class Method:
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
        return method in (cls.INITIALIZE, cls.SERVER_SHUTDOWN)


# 请求 / 响应信封


class RequestEnvelope(ProtocolModel):
    request_id: int = Field(ge=0)
    method: str
    params: dict[str, Any] | None = None


class ResponseEnvelope(ProtocolModel):
    request_id: int
    result: dict[str, Any] | None = None
    error: RpcError | None = None


class ClientNotification(ProtocolModel):
    method: str
    params: dict[str, Any] | None = None


class ServerRequest(ProtocolModel):
    server_call_id: str
    method: str
    params: dict[str, Any]


class EventNotification(ProtocolModel):
    subscription_id: str
    thread_id: str
    turn_id: str | None
    sequence: int = Field(ge=1)
    kind: str
    event_ref: str
    data: dict[str, Any] | None = None


ServerEvent = ServerRequest | EventNotification

# 业务 Operation 参数


class ThreadStartParams(ProtocolModel):
    session_id: str
    context_ref: str


class TurnStartParams(ProtocolModel):
    thread_id: str
    request_ref: str


class TurnInterruptParams(ProtocolModel):
    thread_id: str
    turn_id: str
    reason: str


class ThreadForkParams(ProtocolModel):
    thread_id: str
    after_turn_id: str | None = None


class ThreadSubscribeParams(ProtocolModel):
    thread_id: str
    after_sequence: int = 0


class ThreadUnsubscribeParams(ProtocolModel):
    subscription_id: str


class ServerRequestReply(ProtocolModel):
    server_call_id: str
    result: dict[str, Any] | None = None
    error: RpcError | None = None


# 响应结果


class ThreadStartedResult(ProtocolModel):
    thread_id: str


class TurnStartedResult(ProtocolModel):
    turn_id: str


class ThreadForkedResult(ProtocolModel):
    thread_id: str


class SubscribedResult(ProtocolModel):
    subscription_id: str


class InterruptedResult(ProtocolModel):
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
