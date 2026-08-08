"""统一请求/响应 codec（设计 agent-kernel-runtime §4.3）。

业务消息以 JSON 字符串作为 ArtifactRef；Kernel 不再要求每个 Agent 注册
专用 codec。测试中的 JsonCodec 与本实现保持一致。
"""

import json


class JsonCodec:
    """请求/响应以 JSON 字符串作为 ArtifactRef。"""

    def encode_request(self, value: object) -> str:
        return json.dumps(value, ensure_ascii=False)

    def decode_request(self, ref: str) -> object:
        return json.loads(ref)

    def encode_response(self, value: object) -> str:
        return json.dumps(value, ensure_ascii=False)

    def decode_response(self, ref: str) -> object:
        return json.loads(ref)
