"""导出 Python 端契约固件为稳定 JSON，供 Rust TDD 使用。

直接读取 Python 对象并写入 JSON 固件，Rust 测试可反序列化以验证跨语言兼容性。

用法：
    uv run python scripts/export_rust_contract_fixtures.py
    uv run python scripts/export_rust_contract_fixtures.py --output athena-rust/tests/fixtures
"""

import argparse
import json
from pathlib import Path

from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from athena.app_server.protocol import (
    ClientNotification,
    ErrorCode,
    EventNotification,
    Method,
    RequestEnvelope,
    ResponseEnvelope,
    RpcError,
    ServerRequest,
    ThreadForkParams,
    ThreadStartParams,
    ThreadSubscribeParams,
    ThreadUnsubscribeParams,
    TurnInterruptParams,
    TurnStartParams,
    rpc_error,
)
from athena.core.research_models import Hypothesis
from athena.core.thread_models import AthenaThread, AthenaTurn
from athena.research.contracts import DataCard, MetricSpec

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def default_output() -> Path:
    return PROJECT_ROOT / "athena-rust" / "tests" / "fixtures"


def export_protocol_fixtures() -> dict:
    """导出 ErrorCode 值、方法常量及协议 DTO。"""
    fixtures = {}

    # 错误码
    fixtures["error_codes"] = {
        name: int(code) for name, code in ErrorCode.__members__.items()
    }

    # 方法名称
    method_values = {
        m: getattr(Method, m)
        for m in dir(Method)
        if m.isupper() and not m.startswith("_") and isinstance(getattr(Method, m), str)
    }
    fixtures["methods"] = list(method_values)
    fixtures["method_values"] = method_values

    # 请求信封
    req = RequestEnvelope(
        request_id=42,
        method="turn/start",
        params={"thread_id": "t1", "request_ref": "artifact://test"},
    )
    fixtures["request_envelope"] = req.model_dump()

    # 响应信封（成功）
    resp = ResponseEnvelope(request_id=42, result={"turn_id": "turn-1"})
    fixtures["response_envelope_success"] = resp.model_dump()

    # 响应信封（错误）
    resp_err = ResponseEnvelope(
        request_id=42, error=rpc_error(ErrorCode.CLOSED, "server shutting down")
    )
    fixtures["response_envelope_error"] = resp_err.model_dump()

    # 客户端通知
    notif = ClientNotification(method="initialized", params={"protocol_version": 1})
    fixtures["client_notification"] = notif.model_dump()

    # 事件通知
    ev = EventNotification(
        subscription_id="sub:abc123",
        thread_id="t1",
        turn_id="turn-1",
        sequence=5,
        kind="turn_completed",
        event_ref="athena-event:xyz",
    )
    fixtures["event_notification"] = ev.model_dump()

    # 服务端请求
    sr = ServerRequest(
        server_call_id="call-1",
        method="item/approval/request",
        params={"thread_id": "t1", "turn_id": "turn-1", "message": "approve?"},
    )
    fixtures["server_request"] = sr.model_dump()

    # 操作参数
    fixtures["thread_start_params"] = ThreadStartParams(
        session_id="sess-1", context_ref="artifact://ctx/init"
    ).model_dump()

    fixtures["turn_start_params"] = TurnStartParams(
        thread_id="t1", request_ref="artifact://req/test"
    ).model_dump()

    fixtures["turn_interrupt_params"] = TurnInterruptParams(
        thread_id="t1", turn_id="turn-1", reason="user_requested"
    ).model_dump()

    fixtures["thread_fork_params"] = ThreadForkParams(
        thread_id="t1", after_turn_id="turn-3"
    ).model_dump()

    fixtures["thread_subscribe_params"] = ThreadSubscribeParams(
        thread_id="t1", after_sequence=0
    ).model_dump()

    fixtures["thread_unsubscribe_params"] = ThreadUnsubscribeParams(
        subscription_id="sub:abc123"
    ).model_dump()

    return fixtures


def export_domain_fixtures() -> dict:
    """导出 schemas.py 中的领域 DTO 和验证示例。"""
    fixtures = {}

    # 假设
    h = Hypothesis(
        statement="Adding a batch-norm layer improves convergence",
        intervention="Insert BatchNorm2d after conv1 in ResNet",
        expected_effect="Training loss decreases 15% faster",
        status="PROPOSED",
        evidence_refs=[],
        patience_grant=0,
    )
    fixtures["hypothesis_proposed"] = h.model_dump()

    h_supported = Hypothesis(
        statement="Larger learning rate speeds up training",
        intervention="Change lr from 0.001 to 0.01",
        expected_effect="Loss reaches 0.1 in half the epochs",
        status="SUPPORTED",
        evidence_refs=["artifact://exp/run-42/results"],
    )
    fixtures["hypothesis_supported"] = h_supported.model_dump()

    # 带耐心授予的假设
    h_patience = Hypothesis(
        statement="Need more epochs to see effect",
        intervention="Train for 200 epochs instead of 100",
        expected_effect="Accuracy improves after epoch 120",
        status="REFUTED",
        patience_grant=1,
        patience_evidence_ref="artifact://exp/run-43/results",
    )
    fixtures["hypothesis_refuted_with_patience"] = h_patience.model_dump()

    # 无效假设（有耐心但无证据 — 验证应失败）
    try:
        Hypothesis(
            statement="Invalid",
            intervention="Test",
            expected_effect="Test",
            patience_grant=1,
            patience_evidence_ref=None,
        )
        fixtures["hypothesis_validation_error"] = {"error": "should have raised"}
    except ValueError as e:
        fixtures["hypothesis_validation_error"] = {"error": str(e)}

    # 指标规格
    fixtures["metric_spec"] = MetricSpec(
        name="accuracy", direction="maximize"
    ).model_dump()

    fixtures["metric_spec_minimize"] = MetricSpec(
        name="loss", direction="minimize"
    ).model_dump()

    # 数据卡片
    fixtures["data_card"] = DataCard(
        dataset_ref="artifact://datasets/cifar10",
        fingerprint="sha256:abc123def456",
        schema_ref="artifact://schemas/cifar10_v1",
        split_manifest_ref="artifact://manifests/cifar10_splits",
    ).model_dump()

    # Athena 线程
    fixtures["athena_thread"] = AthenaThread(
        thread_id="t-001",
        session_id="sess-2026-07-25",
        status="idle",
        context_ref="artifact://ctx/initial",
    ).model_dump()

    # Athena 轮次
    fixtures["athena_turn"] = AthenaTurn(
        turn_id="turn-001",
        thread_id="t-001",
        request_ref="artifact://req/hello",
        status="running",
    ).model_dump()

    return fixtures


def export_message_fixtures() -> dict:
    """导出 PydanticAI ModelMessage 固件。"""
    fixtures = {}
    adapter = ModelMessagesTypeAdapter

    # 系统消息
    sys_msg = ModelRequest(
        parts=[SystemPromptPart(content="You are a helpful assistant.")]
    )
    fixtures["system_message"] = adapter.dump_python([sys_msg], mode="json")

    # 用户消息
    user_msg = ModelRequest(parts=[UserPromptPart(content="What is 2+2?")])
    fixtures["user_message"] = adapter.dump_python([user_msg], mode="json")

    # 助手文本回复
    asst_msg = ModelResponse(parts=[TextPart(content="2+2 equals 4.")])
    fixtures["assistant_text"] = adapter.dump_python([asst_msg], mode="json")

    # 带工具调用的助手消息
    asst_tool_msg = ModelResponse(
        parts=[
            TextPart(content="Let me search for that."),
            ToolCallPart(
                tool_name="search",
                tool_call_id="call_abc123",
                args='{"query": "rust programming"}',
            ),
            ToolCallPart(
                tool_name="read_file",
                tool_call_id="call_def456",
                args='{"path": "/tmp/test.rs"}',
            ),
        ]
    )
    fixtures["assistant_with_tool_calls"] = adapter.dump_python(
        [asst_tool_msg], mode="json"
    )

    # 工具返回
    tool_ret_msg = ModelRequest(
        parts=[
            ToolReturnPart(
                tool_name="search",
                content='{"results": ["Rust official site", "Rust book"]}',
                tool_call_id="call_abc123",
            )
        ]
    )
    fixtures["tool_return"] = adapter.dump_python([tool_ret_msg], mode="json")

    return fixtures


def export_rollout_fixtures() -> dict:
    """导出 rollout 记录固件。"""
    fixtures = {}
    adapter = ModelMessagesTypeAdapter

    # 完整对话：system → user → assistant → user → assistant
    conversation = [
        ModelRequest(parts=[SystemPromptPart(content="You are a coding assistant.")]),
        ModelRequest(parts=[UserPromptPart(content="Write a hello world in Rust.")]),
        ModelResponse(
            parts=[TextPart(content='fn main() {\n    println!("Hello, world!");\n}')]
        ),
    ]

    fixtures["conversation"] = {
        "messages": [adapter.dump_python([m], mode="json") for m in conversation],
    }

    # 压缩检查点
    fixtures["compaction_checkpoint"] = {
        "type": "compaction",
        "version": 3,
        "summary": "User asked about Rust. Assistant provided hello world code.",
    }

    # 消息记录
    fixtures["message_record"] = {
        "seq": 7,
        "ts": "2026-07-25T10:00:00+00:00",
        "msg": adapter.dump_python([conversation[1]], mode="json"),
    }

    return fixtures


def main():
    parser = argparse.ArgumentParser(description="Export Rust contract fixtures")
    parser.add_argument(
        "--output",
        type=Path,
        default=default_output(),
        help="Output directory for fixtures",
    )
    args = parser.parse_args()

    fixtures_dir = args.output

    # 确保输出目录存在
    for sub in ["protocol", "messages", "rollout"]:
        (fixtures_dir / sub).mkdir(parents=True, exist_ok=True)

    # 导出协议固件
    protocol = export_protocol_fixtures()
    (fixtures_dir / "protocol" / "protocol.json").write_text(
        json.dumps(protocol, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    print(f"  OK protocol.json ({len(protocol)} fixture groups)")

    # 导出领域固件
    domain = export_domain_fixtures()
    (fixtures_dir / "protocol" / "domain.json").write_text(
        json.dumps(domain, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    print(f"  OK domain.json ({len(domain)} fixture groups)")

    # 导出消息固件
    messages = export_message_fixtures()
    (fixtures_dir / "messages" / "messages.json").write_text(
        json.dumps(messages, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    print(f"  OK messages.json ({len(messages)} fixture groups)")

    # 导出 rollout 固件
    rollout = export_rollout_fixtures()
    (fixtures_dir / "rollout" / "rollout.json").write_text(
        json.dumps(rollout, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    print(f"  OK rollout.json ({len(rollout)} fixture groups)")

    print(f"\nAll fixtures exported to: {fixtures_dir}")


if __name__ == "__main__":
    main()
