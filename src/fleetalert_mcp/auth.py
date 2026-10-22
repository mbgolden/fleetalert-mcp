"""Static bearer tokens for HTTP mode.

The upstream demo API has no auth, so this server is where identity starts.
Tokens are configured as `client_id:tier:secret` and compared by SHA-256
digest in constant time. The tier rides along as the token's only scope and
caps what that client may call (see guard.py).

This implements the SDK's `TokenVerifier` protocol, so swapping in JWT
validation or RFC 7662 introspection against a real authorization server is
a one-class change. stdio mode has no tokens: the process boundary is the
auth boundary there, as the MCP spec intends.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from mcp.server.auth.provider import AccessToken

from fleetalert_mcp.tiers import Tier

MIN_SECRET_LENGTH = 16


@dataclass(frozen=True)
class StaticToken:
    client_id: str
    tier: Tier
    digest: bytes


def parse_tokens(raw: str) -> list[StaticToken]:
    """`"ops:executes_action:s3cr3t...,ci:read_only:..."` -> tokens.

    Raises ValueError on a malformed entry, a duplicate client id, or a
    secret shorter than MIN_SECRET_LENGTH. Fail at startup, not on first use.
    """
    tokens: list[StaticToken] = []
    seen: set[str] = set()
    for entry in (e.strip() for e in raw.split(",") if e.strip()):
        parts = entry.split(":", 2)
        if len(parts) != 3 or not all(parts):
            raise ValueError("each token must be `client_id:tier:secret`")
        client_id, tier_name, secret = parts
        if client_id in seen:
            raise ValueError(f"duplicate token client_id {client_id!r}")
        if len(secret) < MIN_SECRET_LENGTH:
            raise ValueError(
                f"secret for {client_id!r} is shorter than {MIN_SECRET_LENGTH} characters"
            )
        seen.add(client_id)
        tokens.append(StaticToken(client_id, Tier.parse(tier_name), _digest(secret)))
    return tokens


class StaticTokenVerifier:
    """`TokenVerifier` over a fixed list of hashed secrets."""

    def __init__(self, tokens: list[StaticToken], *, resource: str | None = None) -> None:
        self._tokens = tokens
        self._resource = resource

    async def verify_token(self, token: str) -> AccessToken | None:
        presented = _digest(token)
        matched: StaticToken | None = None
        # Compare against every entry so timing does not reveal which one matched.
        for candidate in self._tokens:
            if hmac.compare_digest(presented, candidate.digest):
                matched = candidate
        if matched is None:
            return None
        return AccessToken(
            token=token,
            client_id=matched.client_id,
            scopes=[matched.tier.value],
            expires_at=None,
            resource=self._resource,
        )


def _digest(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("utf-8")).digest()
