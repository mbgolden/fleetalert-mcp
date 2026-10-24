"""AWS Lambda entry point: the same server behind API Gateway, via Mangum.

Protocol 2026-07-28 over Streamable HTTP is one POST per request, so the
server runs in stateless JSON mode: no sessions, no SSE, nothing a Lambda
cannot do. Two things make that safe across cold starts and concurrent
instances:

- Tokens come from a Secrets Manager secret (`FLEETALERT_MCP_SECRET_ARN`),
  read once per container, never from the deployment package.
- The elicitation round trip for `confirm_fix` carries encrypted state
  between two requests. The SDK's default key is per process, so the second
  request could land on an instance that cannot read it. The secret also
  holds `state_key`, a fixed key every instance shares.

The MCP server object is rebuilt per invocation (cheap: eleven tools) because
the SDK's session manager runs once per app and Mangum drives the app
lifespan per event. Settings, tokens, the upstream client and the metrics
counters are built once per container and reused.

Secret shape, set by hand in the console (never via Terraform):

    {"tokens": "visitor:read_only:<32 chars>,ops:executes_action:<32 chars>",
     "state_key": "<64 hex chars from secrets.token_hex(32)>"}
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from typing import Any

from mangum import Mangum
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from fleetalert_mcp.auth import StaticToken, parse_tokens
from fleetalert_mcp.cli import configure_logging
from fleetalert_mcp.config import Settings
from fleetalert_mcp.guard import Metrics
from fleetalert_mcp.http import add_admin_routes, http_auth
from fleetalert_mcp.server import build_server
from fleetalert_mcp.upstream import FleetAlertAPI

log = logging.getLogger("fleetalert_mcp.lambda")


def load_secret(arn: str | None) -> dict[str, Any]:
    """The tokens and state key. Empty when no ARN is configured, so a
    misconfigured deployment refuses every call instead of crashing."""
    if not arn:
        return {}
    import boto3  # the Lambda runtime provides it

    value = boto3.client("secretsmanager").get_secret_value(SecretId=arn).get("SecretString") or ""
    try:
        parsed = json.loads(value)
    except ValueError:
        log.warning("secret is not JSON; expected {tokens, state_key}")
        return {}
    return parsed if isinstance(parsed, dict) else {}


class Deployment:
    """Everything that survives between invocations in one container."""

    def __init__(self, settings: Settings, secret: dict[str, Any]) -> None:
        self.settings = settings
        self.metrics = Metrics()
        self.tokens: list[StaticToken] = parse_tokens(str(secret.get("tokens") or settings.tokens))
        state_key = str(secret.get("state_key") or settings.state_key or "")
        self.state_keys = [state_key] if state_key else None
        self.public_url = (settings.public_url or "").rstrip("/")
        self.api = FleetAlertAPI(settings.api_base_url, settings.upstream_timeout_seconds)
        if not self.public_url:
            log.error("FLEETALERT_MCP_PUBLIC_URL is not set; bearer auth cannot be configured")
        if not self.tokens:
            log.error("no tokens configured; every MCP call will be refused")
        if self.state_keys is None:
            log.warning("no state_key configured; confirm_fix cannot complete across instances")

    def app(self) -> Starlette:
        verifier, auth = (
            http_auth(self.tokens, self.public_url)
            if self.tokens and self.public_url
            else (None, None)
        )
        server = build_server(
            self.settings,
            api=self.api,
            metrics=self.metrics,
            token_verifier=verifier,
            auth=auth,
            request_state_keys=self.state_keys,
        )
        add_admin_routes(server, self.settings, self.metrics, self.tokens, deployment="lambda")
        # API Gateway terminates TLS and owns the hostname; the SDK's DNS-rebinding
        # check is for servers listening on localhost, so it is switched off here.
        return server.streamable_http_app(
            stateless_http=True,
            json_response=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )


@lru_cache(maxsize=1)
def deployment() -> Deployment:
    configure_logging(os.environ.get("FLEETALERT_MCP_LOG_LEVEL", "INFO"))
    settings = Settings()
    secret = load_secret(os.environ.get("FLEETALERT_MCP_SECRET_ARN"))
    dep = Deployment(settings, secret)
    log.info(
        "cold start: tier %s, %d token(s), public %s, upstream %s",
        settings.tier.value,
        len(dep.tokens),
        dep.public_url or "(unset)",
        settings.api_base_url,
    )
    return dep


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """The Lambda handler: API Gateway HTTP API (payload 2.0) events."""
    adapter = Mangum(deployment().app(), lifespan="auto")
    result: dict[str, Any] = adapter(event, context)
    return result
