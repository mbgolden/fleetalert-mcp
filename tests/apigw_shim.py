"""A local HTTP server that turns requests into API Gateway HTTP API (payload
2.0) events, calls a Lambda handler, and writes its response back. Lets the
real MCP client talk to the real Lambda entry point over a socket."""

from __future__ import annotations

import base64
import json
import socket
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

Handler = Callable[[dict[str, Any], Any], dict[str, Any]]


class _Context:
    function_name = "fleetalert-ai-demo-mcp"
    aws_request_id = "req-local"
    memory_limit_in_mb = 256

    @staticmethod
    def get_remaining_time_in_millis() -> int:
        return 30_000


def make_event(
    method: str, path: str, query: str, headers: dict[str, str], body: bytes
) -> dict[str, Any]:
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": query,
        "headers": headers,
        "requestContext": {
            "accountId": "000000000000",
            "apiId": "localtest",
            "domainName": headers.get("host", "localhost"),
            "http": {
                "method": method,
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": "127.0.0.1",
                "userAgent": headers.get("user-agent", ""),
            },
            "requestId": "req-local",
            "stage": "$default",
            "time": "22/Oct/2026:00:00:00 +0000",
            "timeEpoch": 0,
        },
        "body": base64.b64encode(body).decode() if body else "",
        "isBase64Encoded": bool(body),
    }


def serve(handler: Handler) -> tuple[ThreadingHTTPServer, int]:
    class Shim(BaseHTTPRequestHandler):
        def _handle(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length) if length else b""
            path, _, query = self.path.partition("?")
            headers = {k.lower(): v for k, v in self.headers.items()}
            event = make_event(self.command, path, query, headers, body)
            result = handler(event, _Context())
            payload = result.get("body") or ""
            raw = base64.b64decode(payload) if result.get("isBase64Encoded") else payload.encode()
            self.send_response(int(result.get("statusCode", 200)))
            for k, v in (result.get("headers") or {}).items():
                if k.lower() != "content-length":
                    self.send_header(k, v)
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = _handle
        do_POST = _handle
        do_DELETE = _handle

        def log_message(self, *args: Any) -> None:  # quiet
            pass

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    server = ThreadingHTTPServer(("127.0.0.1", port), Shim)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def pretty(result: dict[str, Any]) -> str:
    return json.dumps(result, indent=1)[:500]
