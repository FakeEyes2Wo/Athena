import unittest

import athena.app_server as app_server
from athena.app_server.exceptions import AppServerError, RpcException

EXPECTED_EXPORTS = {
    "AthenaClient",
    "ClientWorker",
    "Sequencer",
    "AppServer",
    "Transport",
    "ProtocolModel",
    "RequestEnvelope",
    "ResponseEnvelope",
    "ClientNotification",
    "EventNotification",
    "ServerRequest",
    "RpcError",
    "ErrorCode",
    "Method",
    "AppServerError",
    "ClosedError",
    "OverloadedError",
    "ProtocolError",
    "RpcException",
    "SubscriptionRegistry",
    "ThreadEventHandlerRegistry",
}


class PublicApiTests(unittest.TestCase):
    def test_all_matches_current_package_exports(self) -> None:
        self.assertEqual(set(app_server.__all__), EXPECTED_EXPORTS)

    def test_every_declared_export_is_available(self) -> None:
        for name in app_server.__all__:
            with self.subTest(name=name):
                self.assertTrue(hasattr(app_server, name))

    def test_rpc_exception_retains_protocol_fields(self) -> None:
        error = RpcException(-32000, "failed", {"retry": False})
        self.assertIsInstance(error, AppServerError)
        self.assertEqual(error.code, -32000)
        self.assertEqual(error.message, "failed")
        self.assertEqual(error.data, {"retry": False})
