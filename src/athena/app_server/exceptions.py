"""app_server 专用异常。"""

class AppServerError(Exception):
    """所有 app_server 异常的基类。"""

class OverloadedError(AppServerError):
    """有界队列满，在超时内无法入队。"""

class ClosedError(AppServerError):
    """Client、Server、Manager 或 ThreadRuntime 已关闭。"""

class ProtocolError(AppServerError):
    """协议违规（如 request_id=0 用于业务请求）。"""

class RpcException(AppServerError):
    """携带 RpcError 的异常，用于从 Client 侧向上抛出协议错误。"""
    def __init__(self, code: int, message: str, data: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data
