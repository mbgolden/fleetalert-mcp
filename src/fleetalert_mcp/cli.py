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

from fleetalert_mcp import __version__
from fleetalert_mcp.auth import parse_tokens
from fleetalert_mcp.config import Settings
from fleetalert_mcp.guard import Metrics
from fleetalert_mcp.http import add_admin_routes, http_auth
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
    state_keys = [settings.state_key] if settings.state_key else None
    if settings.oauth_enabled and not settings.state_key:
        raise SystemExit(
            "FLEETALERT_MCP_OAUTH_ENABLED needs FLEETALERT_MCP_STATE_KEY (32+ characters)"
        )
    oauth_key = settings.state_key if settings.oauth_enabled else None
    http = http_auth(tokens, public_url, oauth_key=oauth_key) if tokens else None
    server = build_server(
        settings,
        metrics=metrics,
        token_verifier=http.verifier if http else None,
        auth=http.settings if http else None,
        auth_server_provider=http.oauth if http else None,
        request_state_keys=state_keys,
    )
    add_admin_routes(
        server, settings, metrics, tokens, deployment="uvicorn", oauth=http.oauth if http else None
    )

    log.info(
        "starting Streamable HTTP on %s:%s (%s) at tier %s, %d token(s), oauth %s",
        args.host,
        args.port,
        resource_url,
        settings.tier.value,
        len(tokens),
        "on" if settings.oauth_enabled else "off",
    )
    anyio.run(functools.partial(server.run_streamable_http_async, host=args.host, port=args.port))


if __name__ == "__main__":  # pragma: no cover
    main()
