"""Pieces shared by every HTTP deployment (uvicorn via the CLI, Lambda via
Mangum): bearer-token auth settings, the optional OAuth authorization
server, and the admin routes."""

from __future__ import annotations

from dataclasses import dataclass

from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from fleetalert_mcp import __version__
from fleetalert_mcp.auth import StaticToken, StaticTokenVerifier
from fleetalert_mcp.config import Settings
from fleetalert_mcp.guard import Metrics
from fleetalert_mcp.oauth import StatelessOAuthProvider, add_consent_routes
from fleetalert_mcp.tiers import Tier


@dataclass
class HttpAuth:
    # With an OAuth provider the SDK verifies every bearer token through it,
    # static governor keys included, so `verifier` is None in that case.
    verifier: TokenVerifier | None
    settings: AuthSettings
    oauth: StatelessOAuthProvider | None


def http_auth(
    tokens: list[StaticToken], public_url: str, *, oauth_key: str | None = None
) -> HttpAuth:
    """Bearer auth with this server as its own issuer (ADR-0004). With an
    `oauth_key`, the server is also an OAuth authorization server with
    dynamic client registration, so Claude Desktop can connect (ADR-0007)."""
    public = public_url.rstrip("/")
    resource_url = f"{public}/mcp"
    static = StaticTokenVerifier(tokens, resource=resource_url)
    if oauth_key is None:
        return HttpAuth(
            verifier=static,
            settings=AuthSettings(
                issuer_url=public,
                resource_server_url=resource_url,
                validate_token_resource=True,
            ),
            oauth=None,
        )
    provider = StatelessOAuthProvider(oauth_key, public, tokens)
    return HttpAuth(
        verifier=None,
        settings=AuthSettings(
            issuer_url=public,
            resource_server_url=resource_url,
            validate_token_resource=True,
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=[t.value for t in Tier],
                default_scopes=[Tier.READ_ONLY.value],
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
        oauth=provider,
    )


def add_admin_routes(
    server: MCPServer,
    settings: Settings,
    metrics: Metrics,
    tokens: list[StaticToken],
    *,
    deployment: str,
    oauth: StatelessOAuthProvider | None = None,
) -> None:
    async def healthz(_: Request) -> Response:
        return JSONResponse(
            {
                "status": "ok",
                "version": __version__,
                "deployment": deployment,
                "tier": settings.tier.value,
                "upstream": settings.api_base_url,
                "auth": "bearer+oauth" if oauth else ("bearer" if tokens else "none"),
                "clients": [t.client_id for t in tokens],
            }
        )

    async def metrics_route(_: Request) -> Response:
        return JSONResponse(metrics.snapshot())

    server.custom_route("/healthz", methods=["GET"], include_in_schema=False)(healthz)
    server.custom_route("/metrics", methods=["GET"], include_in_schema=False)(metrics_route)
    if oauth is not None:
        add_consent_routes(server, oauth)
