"""Runtime settings, all from `FLEETALERT_MCP_*` environment variables.

Nothing here is secret except `tokens`, which is only read in HTTP mode.
A `.env` file in the working directory is honoured for local development.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from fleetalert_mcp.tiers import Tier

# The live demo's API Gateway. Overridable for a local FleetAlert backend.
DEFAULT_API_BASE_URL = "https://u8hjw1h33h.execute-api.us-east-1.amazonaws.com"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FLEETALERT_MCP_", env_file=".env", extra="ignore")

    api_base_url: str = Field(default=DEFAULT_API_BASE_URL, description="FleetAlert API base URL.")
    tier: Tier = Field(
        default=Tier.READ_ONLY,
        description="Highest tier of tool this server registers. Default is read-only.",
    )
    tokens: str = Field(
        default="",
        description=(
            "Bearer tokens for HTTP mode: comma-separated `client_id:tier:secret` entries. "
            "The tier caps what that client may call. Secrets must be at least 16 characters."
        ),
    )
    public_url: str | None = Field(
        default=None,
        description="Public base URL of this server in HTTP mode, e.g. https://mcp.example.com.",
    )
    state_key: str | None = Field(
        default=None,
        description=(
            "Fixed key (32+ characters) sealing the elicitation state carried between requests. "
            "Required when more than one process serves HTTP, as on Lambda; a single process "
            "may leave it unset and use a per-process key."
        ),
    )
    oauth_enabled: bool = Field(
        default=False,
        description=(
            "Also act as an OAuth 2.1 authorization server (dynamic client registration, "
            "PKCE, a consent page) so Claude Desktop can connect. Needs state_key."
        ),
    )
    allow_anonymous_http: bool = Field(
        default=False,
        description="Permit HTTP mode without tokens. Only honoured on a loopback host.",
    )

    # Rate limits. The upstream demo has a daily cost guard (50 rounds or
    # $1.25); these keep one chatty agent from spending it all.
    calls_per_minute: int = Field(default=30, ge=1, description="Per principal, every tool.")
    actions_per_hour: int = Field(
        default=6, ge=1, description="Per principal, tools above read_only (they spend budget)."
    )
    upstream_calls_per_minute: int = Field(
        default=120, ge=1, description="Across all principals, protecting the upstream API."
    )
    upstream_timeout_seconds: float = Field(default=10.0, gt=0)
