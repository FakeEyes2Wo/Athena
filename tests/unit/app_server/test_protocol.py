import unittest

from pydantic import ValidationError

from athena.app_server.protocol import (
    ErrorCode,
    Method,
    RequestEnvelope,
    ResponseEnvelope,
    RpcError,
    ServerRequestReply,
    ThreadStartedResult,
    map_exception_to_error_code,
    rpc_error,
)


class ProtocolModelTests(unittest.TestCase):
    def test_request_rejects_negative_id(self) -> None:
        with self.assertRaises(ValidationError):
            RequestEnvelope(request_id=-1, method=Method.THREAD_START)

    def test_request_coerces_whole_number_ids(self) -> None:
        boolean = RequestEnvelope(request_id=True, method=Method.THREAD_START)
        floating = RequestEnvelope(request_id=1.0, method=Method.THREAD_START)
        self.assertEqual(boolean.request_id, 1)
        self.assertEqual(floating.request_id, 1)

    def test_request_defaults_params_to_none(self) -> None:
        request = RequestEnvelope(request_id=1, method=Method.THREAD_START)
        self.assertIsNone(request.params)

    def test_models_are_frozen_and_forbid_extra_fields(self) -> None:
        request = RequestEnvelope(request_id=1, method=Method.THREAD_START)
        with self.assertRaises(ValidationError):
            request.method = Method.TURN_START
        with self.assertRaises(ValidationError):
            RequestEnvelope(request_id=1, method="x", unexpected=True)

    def test_response_supports_result_and_error_payloads(self) -> None:
        result = ResponseEnvelope(request_id=1, result={"thread_id": "thread:1"})
        error = ResponseEnvelope(
            request_id=2,
            error=RpcError(code=ErrorCode.INTERNAL.value, message="request failed"),
        )
        self.assertEqual(result.result, {"thread_id": "thread:1"})
        self.assertEqual(error.error.code, ErrorCode.INTERNAL.value)

    def test_response_payloads_are_optional_in_current_model(self) -> None:
        empty = ResponseEnvelope(request_id=1)
        combined = ResponseEnvelope(
            request_id=2,
            result={},
            error=RpcError(code=ErrorCode.INTERNAL.value, message="error"),
        )
        self.assertIsNone(empty.result)
        self.assertIsNotNone(combined.error)

    def test_server_request_reply_current_shape(self) -> None:
        reply = ServerRequestReply(server_call_id="server:1", result={"approved": True})
        self.assertEqual(reply.result, {"approved": True})
        self.assertIsNone(reply.error)

    def test_error_factory_uses_numeric_protocol_code(self) -> None:
        error = rpc_error(ErrorCode.OVERLOADED, "busy", {"retry": True})
        self.assertEqual(error.code, -32005)
        self.assertEqual(error.data, {"retry": True})

    def test_exception_mapping_matches_current_table(self) -> None:
        cases = (
            (ValueError("bad"), ErrorCode.INVALID_ARGUMENT),
            (KeyError("missing"), ErrorCode.NOT_FOUND),
            (RuntimeError("state"), ErrorCode.FAILED_PRECONDITION),
            (OSError("io"), ErrorCode.INTERNAL),
        )
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                self.assertEqual(map_exception_to_error_code(error), expected)

    def test_method_control_classification(self) -> None:
        self.assertTrue(Method.is_control_method(Method.INITIALIZE))
        self.assertTrue(Method.is_control_method(Method.SERVER_SHUTDOWN))
        self.assertFalse(Method.is_control_method(Method.THREAD_START))

    def test_result_dto_is_frozen(self) -> None:
        result = ThreadStartedResult(thread_id="thread:1")
        with self.assertRaises(ValidationError):
            result.thread_id = "thread:2"


class ErrorCodeTests(unittest.TestCase):
    def test_current_error_code_values_are_stable(self) -> None:
        self.assertEqual(ErrorCode.INVALID_ARGUMENT.value, -32602)
        self.assertEqual(ErrorCode.NOT_FOUND.value, -32601)
        self.assertEqual(ErrorCode.INTERNAL.value, -32603)
