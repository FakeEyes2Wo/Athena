"""Export Python-side contract fixtures as stable JSON for Rust TDD.

Reads Python objects directly and writes JSON fixtures that Rust tests
can deserialize to verify cross-language compatibility.

Usage:
    uv run python scripts/export_rust_contract_fixtures.py
    uv run python scripts/export_rust_contract_fixtures.py --output athena-rust/tests/fixtures
"""

import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src" / "athena"


def default_output() -> Path:
    return PROJECT_ROOT / "athena-rust" / "tests" / "fixtures"


def export_protocol_fixtures(out: Path) -> dict:
    """Export ErrorCode values, method constants, and protocol DTOs."""
    from athena.app_server.protocol import (
        ErrorCode,
        Method,
        ClientNotification,
        RequestEnvelope,
        ResponseEnvelope,
        EventNotification,
        ServerRequest,
        RpcError,
        rpc_error,
        ThreadStartParams,
        TurnStartParams,
        TurnInterruptParams,
        ThreadForkParams,
        ThreadSubscribeParams,
        ThreadUnsubscribeParams,
    )

    fixtures = {}

    # Error codes
    fixtures["error_codes"] = {
        name: int(code) for name, code in ErrorCode.__members__.items()
    }

    # Method names
    fixtures["methods"] = [
        m
        for m in dir(Method)
        if m.isupper() and not m.startswith("_") and isinstance(getattr(Method, m), str)
    ]

    # Method -> value mapping
    fixtures["method_values"] = {
        m: getattr(Method, m)
        for m in dir(Method)
        if m.isupper() and not m.startswith("_") and isinstance(getattr(Method, m), str)
    }

    # Request envelope
    req = RequestEnvelope(
        request_id=42,
        method="turn/start",
        params={"thread_id": "t1", "request_ref": "artifact://test"},
    )
    fixtures["request_envelope"] = req.model_dump()

    # Response envelope (success)
    resp = ResponseEnvelope(request_id=42, result={"turn_id": "turn-1"})
    fixtures["response_envelope_success"] = resp.model_dump()

    # Response envelope (error)
    resp_err = ResponseEnvelope(
        request_id=42, error=rpc_error(ErrorCode.CLOSED, "server shutting down")
    )
    fixtures["response_envelope_error"] = resp_err.model_dump()

    # Client notification
    notif = ClientNotification(method="initialized", params={"protocol_version": 1})
    fixtures["client_notification"] = notif.model_dump()

    # Event notification
    ev = EventNotification(
        subscription_id="sub:abc123",
        thread_id="t1",
        turn_id="turn-1",
        sequence=5,
        kind="turn_completed",
        event_ref="athena-event:xyz",
    )
    fixtures["event_notification"] = ev.model_dump()

    # Server request
    sr = ServerRequest(
        server_call_id="call-1",
        method="item/approval/request",
        params={"thread_id": "t1", "turn_id": "turn-1", "message": "approve?"},
    )
    fixtures["server_request"] = sr.model_dump()

    # Operation params
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


def export_domain_fixtures(out: Path) -> dict:
    """Export domain DTOs and validation samples from schemas.py."""
    from athena.core.schemas import (
        Hypothesis,
        MetricSpec,
        DataCard,
        AthenaThread,
        AthenaTurn,
    )

    fixtures = {}

    # Hypothesis
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

    # Hypothesis with patience grant
    h_patience = Hypothesis(
        statement="Need more epochs to see effect",
        intervention="Train for 200 epochs instead of 100",
        expected_effect="Accuracy improves after epoch 120",
        status="REFUTED",
        patience_grant=1,
        patience_evidence_ref="artifact://exp/run-43/results",
    )
    fixtures["hypothesis_refuted_with_patience"] = h_patience.model_dump()

    # Invalid hypothesis (patience without evidence — should fail validation)
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

    # MetricSpec
    fixtures["metric_spec"] = MetricSpec(
        name="accuracy", direction="maximize"
    ).model_dump()

    fixtures["metric_spec_minimize"] = MetricSpec(
        name="loss", direction="minimize"
    ).model_dump()

    # DataCard
    fixtures["data_card"] = DataCard(
        dataset_ref="artifact://datasets/cifar10",
        fingerprint="sha256:abc123def456",
        schema_ref="artifact://schemas/cifar10_v1",
        split_manifest_ref="artifact://manifests/cifar10_splits",
    ).model_dump()

    # AthenaThread
    fixtures["athena_thread"] = AthenaThread(
        thread_id="t-001",
        session_id="sess-2026-07-25",
        status="idle",
        context_ref="artifact://ctx/initial",
    ).model_dump()

    # AthenaTurn
    fixtures["athena_turn"] = AthenaTurn(
        turn_id="turn-001",
        thread_id="t-001",
        request_ref="artifact://req/hello",
        status="running",
    ).model_dump()

    return fixtures


def export_message_fixtures(out: Path) -> dict:
    """Export PydanticAI ModelMessage fixtures."""
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        SystemPromptPart,
        UserPromptPart,
        TextPart,
        ToolCallPart,
        ToolReturnPart,
        ModelMessagesTypeAdapter,
    )

    fixtures = {}
    adapter = ModelMessagesTypeAdapter

    # System message
    sys_msg = ModelRequest(
        parts=[SystemPromptPart(content="You are a helpful assistant.")]
    )
    fixtures["system_message"] = adapter.dump_python([sys_msg], mode="json")

    # User message
    user_msg = ModelRequest(parts=[UserPromptPart(content="What is 2+2?")])
    fixtures["user_message"] = adapter.dump_python([user_msg], mode="json")

    # Assistant text response
    asst_msg = ModelResponse(parts=[TextPart(content="2+2 equals 4.")])
    fixtures["assistant_text"] = adapter.dump_python([asst_msg], mode="json")

    # Assistant with tool calls
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

    # Tool return
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


def export_rollout_fixtures(out: Path) -> dict:
    """Export rollout record fixtures."""
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        SystemPromptPart,
        UserPromptPart,
        TextPart,
        ToolCallPart,
        ToolReturnPart,
        ModelMessagesTypeAdapter,
    )

    fixtures = {}
    adapter = ModelMessagesTypeAdapter

    # A full conversation: system → user → assistant → user → assistant
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

    # Compaction checkpoint
    fixtures["compaction_checkpoint"] = {
        "type": "compaction",
        "version": 3,
        "summary": "User asked about Rust. Assistant provided hello world code.",
    }

    # Message record
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

    out = args.output
    fixtures_dir = out

    # Ensure output directories exist
    for sub in ["protocol", "messages", "rollout"]:
        (fixtures_dir / sub).mkdir(parents=True, exist_ok=True)

    # Export protocol fixtures
    protocol = export_protocol_fixtures(fixtures_dir)
    (fixtures_dir / "protocol" / "protocol.json").write_text(
        json.dumps(protocol, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    print(f"  ✓ protocol.json ({len(protocol)} fixture groups)")

    # Export domain fixtures
    domain = export_domain_fixtures(fixtures_dir)
    (fixtures_dir / "protocol" / "domain.json").write_text(
        json.dumps(domain, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    print(f"  ✓ domain.json ({len(domain)} fixture groups)")

    # Export message fixtures
    messages = export_message_fixtures(fixtures_dir)
    (fixtures_dir / "messages" / "messages.json").write_text(
        json.dumps(messages, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    print(f"  ✓ messages.json ({len(messages)} fixture groups)")

    # Export rollout fixtures
    rollout = export_rollout_fixtures(fixtures_dir)
    (fixtures_dir / "rollout" / "rollout.json").write_text(
        json.dumps(rollout, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    print(f"  ✓ rollout.json ({len(rollout)} fixture groups)")

    print(f"\nAll fixtures exported to: {fixtures_dir}")


if __name__ == "__main__":
    main()
