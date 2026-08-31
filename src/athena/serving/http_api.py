"""Thin HTTP adapter over :mod:`athena.serving.model`.

Only routes/HTTP concerns live here. No business logic, no raw preproc dict
access beyond routed methods on ModelBundle.
"""

from __future__ import annotations

import argparse
import json
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from athena.serving.model import ModelBundle, ValidationError


class Handler(BaseHTTPRequestHandler):
    bundle: ModelBundle
    server_version = "athena-predictions-api/1.0"
    sys_version = ""

    def _send(self, status: HTTPStatus, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(HTTPStatus.NO_CONTENT, {})

    def do_GET(self) -> None:  # noqa: N802
        route = self.path.split("?", 1)[0].rstrip("/") or "/"
        bundle = self.bundle
        if route == "/":
            self._send(
                HTTPStatus.OK,
                bundle.describe(self.headers.get("Host", "127.0.0.1:8000")),
            )
        elif route == "/health":
            self._send(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "model": "LightGBM booster, frozen",
                    "weights": bundle.source,
                    "n_features": len(bundle.preproc["feature_columns"]),
                    "trained_with": bundle.preproc.get("versions", {}),
                },
            )
        elif route == "/schema":
            self._send(HTTPStatus.OK, bundle.schema())
        elif route == "/example":
            self._send(HTTPStatus.OK, bundle.example())
        else:
            self._send(
                HTTPStatus.NOT_FOUND,
                {
                    "error": f"没有这个路由 {route}",
                    "routes": ["/", "/health", "/schema", "/example", "POST /predict"],
                },
            )

    def do_POST(self) -> None:  # noqa: N802
        route = self.path.split("?", 1)[0].rstrip("/") or "/"
        if route != "/predict":
            self._send(
                HTTPStatus.NOT_FOUND,
                {"error": f"没有这个路由 POST {route}", "routes": ["POST /predict"]},
            )
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._send(
                HTTPStatus.BAD_REQUEST,
                {"error": "body 是空的；GET /example 可以拿到一个能直接 POST 的 body"},
            )
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": f"body 不是合法 JSON：{exc}"})
            return

        try:
            request = self.bundle.validate(payload)
        except ValidationError as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        try:
            predictions = self.bundle.predict(request)
        except Exception as exc:  # noqa: BLE001 - uncaught malformed records
            self._send(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"打分失败：{type(exc).__name__}: {exc}"},
            )
            return

        self._send(
            HTTPStatus.OK,
            {
                "n": len(predictions),
                "threshold": request.threshold,
                "threshold_rule": self.bundle.preproc.get("threshold_rule", ""),
                "predictions": predictions,
            },
        )


def build_server(bundle: ModelBundle, host: str, port: int) -> ThreadingHTTPServer:
    """Build (but do not serve) the HTTP server."""
    handler = type("BoundHandler", (Handler,), {"bundle": bundle})
    return ThreadingHTTPServer((host, port), handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weights", type=Path, required=True, help="含 model.txt 与 preproc.json 的目录")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    server = build_server(ModelBundle.load(args.weights), args.host, args.port)
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    print(f"listening on http://{shown}:{args.port}  (试试 GET / )", file=sys.stderr)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"注意：绑在 {args.host}，别的主机能连上来。这个端点只读、无状态，"
            "但没有限流——面向公网时请放在反向代理后面。",
            file=sys.stderr,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    finally:
        server.server_close()
    return 0


__all__ = ["Handler", "build_server", "main"]
