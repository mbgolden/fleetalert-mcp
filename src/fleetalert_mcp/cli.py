"""`fleetalert-mcp`: run the server over stdio (default) or Streamable HTTP.

stdio is what Claude Code and Claude Desktop launch locally; no tokens, the
process boundary is the auth boundary. HTTP mode is for a deployed server and
refuses to start without bearer tokens unless explicitly allowed on loopback.
"""

from __future__ import annotations

import argparse
import functools
import json
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import anyio
from mcp.server.auth.settings import AuthSettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from fleetalert_mcp import __version__
from fleetalert_mcp.auth import StaticTokenVerifier, parse_tokens
from fleetalert_mcp.config import Settings
from fleetalert_mcp.guard import Metrics
from fleetalert_mcp.server import build_server

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Audit records are already JSON; pass them through."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.name == "fleetalert_mcp.audit" and message.startswith("{"):
            return message
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": message,
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str) -> None:
    # stderr only: on stdio, stdout is the MCP wire.
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level.upper(), handlers=[handler], force=True)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="fleetalert-mcp",
        description="MCP server for FleetAlert AI. Settings come from FLEETALERT_MCP_* env vars.",
    )
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP mode bind host.")
    parser.add_argument("--port", type=int, default=8000, help="HTTP mode bind port.")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    configure_logging(args.log_level)
    log = logging.getLogger("fleetalert_mcp")
    settings = Settings()
    metrics = Metrics()

    if args.transport == "stdio":
        log.info(
            "starting on stdio at tier %s against %s", settings.tier.value, settings.api_base_url
        )
        build_server(settings, metrics=metrics).run("stdio")
        return

    tokens = parse_tokens(settings.tokens)
    if not tokens:
        if not (settings.allow_anonymous_http and args.host in LOOPBACK_HOSTS):
            raise SystemExit(
                "HTTP mode needs bearer tokens: set FLEETALERT_MCP_TOKENS to "
                "`client_id:tier:secret[,...]`. To run without them on loopback only, "
                "set FLEETALERT_MCP_ALLOW_ANONYMOUS_HTTP=1."
            )
        log.warning(
            "HTTP mode without auth on %s: every caller is `local` at tier %s",
            args.host,
            settings.tier.value,
        )

    public_url = (settings.public_url or f"http://{args.host}:{args.port}").rstrip("/")
    resource_url = f"{public_url}/mcp"
    verifier = StaticTokenVerifier(tokens, resource=resource_url) if tokens else None
    auth = (
        AuthSettings(
            issuer_url=public_url,
            resource_server_url=resource_url,
            validate_token_resource=True,
        )
        if tokens
        else None
    )
    server = build_server(settings, metrics=metrics, token_verifier=verifier, auth=auth)

    async def healthz(_: Request) -> Response:
        return JSONResponse(
            {
                "status": "ok",
                "version": __version__,
                "tier": settings.tier.value,
                "upstream": settings.api_base_url,
                "auth": "bearer" if tokens else "none",
                "clients": [t.client_id for t in tokens],
            }
        )

    async def metrics_route(_: Request) -> Response:
        return JSONResponse(metrics.snapshot())

    server.custom_route("/healthz", methods=["GET"], include_in_schema=False)(healthz)
    server.custom_route("/metrics", methods=["GET"], include_in_schema=False)(metrics_route)

    log.info(
        "starting Streamable HTTP on %s:%s (%s) at tier %s, %d token(s)",
        args.host,
        args.port,
        resource_url,
        settings.tier.value,
        len(tokens),
    )
    anyio.run(functools.partial(server.run_streamable_http_async, host=args.host, port=args.port))


if __name__ == "__main__":  # pragma: no cover
    main()
