"""Pieces shared by every HTTP deployment (uvicorn via the CLI, Lambda via
Mangum): bearer-token auth settings and the admin routes."""

from __future__ import annotations

from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from fleetalert_mcp import __version__
from fleetalert_mcp.auth import StaticToken, StaticTokenVerifier
from fleetalert_mcp.config import Settings
from fleetalert_mcp.guard import Metrics


def http_auth(
    tokens: list[StaticToken], public_url: str
) -> tuple[StaticTokenVerifier, AuthSettings]:
    """The SDK's bearer auth, with this server named as its own issuer: the
    right shape for static tokens, and a one-class swap for a real
    authorization server later (ADR-0004)."""
    public = public_url.rstrip("/")
    resource_url = f"{public}/mcp"
    verifier = StaticTokenVerifier(tokens, resource=resource_url)
    auth = AuthSettings(
        issuer_url=public,
        resource_server_url=resource_url,
        validate_token_resource=True,
    )
    return verifier, auth


def add_admin_routes(
    server: MCPServer,
    settings: Settings,
    metrics: Metrics,
    tokens: list[StaticToken],
    *,
    deployment: str,
) -> None:
    async def healthz(_: Request) -> Response:
        return JSONResponse(
            {
                "status": "ok",
                "version": __version__,
                "deployment": deployment,
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
