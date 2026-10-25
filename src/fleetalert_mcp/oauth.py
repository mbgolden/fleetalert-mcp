"""An OAuth 2.1 authorization server inside the MCP server, with no state.

Claude Desktop's custom connectors (and claude.ai) only speak OAuth: they
discover the authorization server from the protected-resource metadata,
register themselves dynamically, send the user through /authorize with
PKCE, and exchange the code at /token. Claude Code can do that or send a
bearer token; both keep working.

The server runs on Lambda, so nothing can be kept in memory between
requests and there is deliberately no database. Everything the flow needs
to remember is sealed into the token itself with AES-256-GCM under a key
derived from the shared `state_key` (ADR-0007):

- a registered client's metadata is sealed into its `client_id`
- the authorization request is sealed into a `txn` the consent page carries
- the consent decision is sealed into the authorization code
- the grant is sealed into the access and refresh tokens

Each kind uses its own derived key and prefix, so a token of one kind can
never be read as another. The consent page is where a person decides who
the connector is: a read-only visitor by default, or a governor key's
principal if they paste one.

Trade-off accepted for a demo: codes and refresh tokens cannot be revoked
or single-use without a store, so codes live five minutes and are bound to
the client and its PKCE verifier. A DynamoDB nonce table is the backlog
item if that ever matters.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import time
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from mcp.server import MCPServer
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from fleetalert_mcp.auth import StaticToken, StaticTokenVerifier
from fleetalert_mcp.tiers import Tier

VISITOR_SUBJECT = "visitor"
CONSENT_PATH = "/consent"


class Sealer:
    """AES-GCM sealing of small JSON payloads with an expiry, one per purpose."""

    def __init__(self, master_key: str, purpose: str) -> None:
        if len(master_key) < 32:
            raise ValueError("the OAuth sealing key needs at least 32 characters")
        derived = hashlib.sha256(
            f"fleetalert-mcp:{purpose}:".encode() + master_key.encode()
        ).digest()
        self._aead = AESGCM(derived)
        self._aad = purpose.encode()
        self.prefix = f"fmc{purpose[0]}_"

    def seal(self, payload: dict[str, Any], ttl_seconds: float) -> str:
        body = dict(payload)
        body["exp"] = time.time() + ttl_seconds
        nonce = os.urandom(12)
        sealed = self._aead.encrypt(
            nonce, json.dumps(body, separators=(",", ":")).encode(), self._aad
        )
        return self.prefix + base64.urlsafe_b64encode(nonce + sealed).decode().rstrip("=")

    def unseal(self, token: str) -> dict[str, Any] | None:
        if not token.startswith(self.prefix):
            return None
        raw = token[len(self.prefix) :]
        try:
            data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
            plain = self._aead.decrypt(data[:12], data[12:], self._aad)
            body: Any = json.loads(plain)
        except Exception:  # noqa: BLE001 - any failure is "not a valid token"
            return None
        if not isinstance(body, dict) or float(body.get("exp", 0)) < time.time():
            return None
        return body


class StatelessOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """`OAuthAuthorizationServerProvider` with every record sealed into the
    token that refers to it."""

    def __init__(
        self,
        master_key: str,
        public_url: str,
        tokens: list[StaticToken],
        *,
        access_ttl: int = 3600,
        refresh_ttl: int = 30 * 24 * 3600,
    ) -> None:
        self.public_url = public_url.rstrip("/")
        self.resource_url = f"{self.public_url}/mcp"
        self.tokens = tokens
        self.access_ttl = access_ttl
        self.refresh_ttl = refresh_ttl
        self._static = StaticTokenVerifier(tokens, resource=self.resource_url)
        self._clients = Sealer(master_key, "client")
        self._txns = Sealer(master_key, "txn")
        self._codes = Sealer(master_key, "authcode")
        self._access = Sealer(master_key, "access")
        self._refresh = Sealer(master_key, "refresh")

    # -- clients ---------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self._clients.unseal(client_id)
        if data is None:
            return None
        return OAuthClientInformationFull.model_validate({**data["client"], "client_id": client_id})

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # The SDK returns this same object to the client, so the sealed id
        # written here is the id the client will use.
        record = client_info.model_dump(mode="json", exclude={"client_id"})
        client_info.client_id = self._clients.seal({"client": record}, ttl_seconds=365 * 24 * 3600)

    # -- authorization ---------------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        txn = self._txns.seal(
            {
                "client_id": client.client_id,
                "client_name": client.client_name or "an MCP client",
                "redirect_uri": str(params.redirect_uri),
                "explicit": params.redirect_uri_provided_explicitly,
                "code_challenge": params.code_challenge,
                "scopes": params.scopes or [],
                "state": params.state,
                "resource": params.resource,
            },
            ttl_seconds=600,
        )
        return f"{self.public_url}{CONSENT_PATH}?txn={txn}"

    def consent_request(self, txn: str) -> dict[str, Any] | None:
        return self._txns.unseal(txn)

    def complete_consent(self, txn: str, presented_token: str | None) -> str:
        """The person decided: return the redirect back to the client, with
        the code or an error."""
        request = self._txns.unseal(txn)
        if request is None:
            raise ValueError("this sign-in request has expired; start again from your client")
        principal = self._principal_for(presented_token)
        if principal is None:
            raise ValueError("that governor key is not recognised")
        subject, tier = principal
        code = self._codes.seal(
            {
                "client_id": request["client_id"],
                "redirect_uri": request["redirect_uri"],
                "explicit": request["explicit"],
                "code_challenge": request["code_challenge"],
                "scopes": [tier.value],
                "resource": self.resource_url,
                "subject": subject,
            },
            ttl_seconds=300,
        )
        return construct_redirect_uri(
            request["redirect_uri"], code=code, state=request.get("state")
        )

    def _principal_for(self, presented: str | None) -> tuple[str, Tier] | None:
        if not presented:
            return VISITOR_SUBJECT, Tier.READ_ONLY
        digest = hashlib.sha256(presented.encode()).digest()
        matched: StaticToken | None = None
        for token in self.tokens:
            if hmac.compare_digest(digest, token.digest):
                matched = token
        return (matched.client_id, matched.tier) if matched else None

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        data = self._codes.unseal(authorization_code)
        if data is None or data["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=data["scopes"],
            expires_at=float(data["exp"]),
            client_id=client.client_id,
            code_challenge=data["code_challenge"],
            redirect_uri=data["redirect_uri"],
            redirect_uri_provided_explicitly=bool(data["explicit"]),
            resource=data.get("resource"),
            subject=data.get("subject"),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.subject)

    # -- refresh ---------------------------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        data = self._refresh.unseal(refresh_token)
        if data is None or data["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=data["scopes"],
            expires_at=int(data["exp"]),
            resource=self.resource_url,
            subject=data.get("subject"),
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        granted = scopes or refresh_token.scopes
        if not set(granted) <= set(refresh_token.scopes):
            raise TokenError(
                error="invalid_scope", error_description="cannot widen the grant on refresh"
            )
        return self._issue(client.client_id, granted, refresh_token.subject)

    # -- access ----------------------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        # A static governor key (Claude Code's bearer header) is accepted here
        # too: the SDK verifies every bearer token through the AS provider.
        static = await self._static.verify_token(token)
        if static is not None:
            return static
        data = self._access.unseal(token)
        if data is None:
            return None
        return AccessToken(
            token=token,
            client_id=data["client_id"],
            scopes=data["scopes"],
            expires_at=int(data["exp"]),
            resource=self.resource_url,
            subject=data.get("subject"),
            claims={"iss": self.public_url},
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        # Stateless: nothing to delete. Tokens expire on their own.
        return None

    def _issue(self, client_id: str, scopes: list[str], subject: str | None) -> OAuthToken:
        grant = {"client_id": client_id, "scopes": scopes, "subject": subject}
        return OAuthToken(
            access_token=self._access.seal(grant, self.access_ttl),
            token_type="Bearer",  # nosec B106 - a token type, not a secret
            expires_in=self.access_ttl,
            scope=" ".join(scopes),
            refresh_token=self._refresh.seal(grant, self.refresh_ttl),
        )


# -- the consent page ---------------------------------------------------------

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Connect to FleetAlert</title>
<style>
  :root {{ color-scheme: light dark;
    --bg: #f4f2ee; --card: #ffffff; --line: #d8d3ca; --ink: #201c16; --dim: #5c564d;
    --accent: #0f8a7a; --on-accent: #ffffff; --field: #ffffff; --err: #a13f34; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #12161c; --card: #1a212b; --line: #313b49; --ink: #e7ecf3;
      --dim: #a7b0bd; --accent: #22b39d; --on-accent: #0b1512; --field: #12161c;
      --err: #f08c7d; }} }}
  body {{ margin: 0; padding: 32px 16px; background: var(--bg); color: var(--ink);
         font: 16px/1.5 "IBM Plex Sans", -apple-system, "Segoe UI", sans-serif; }}
  .card {{ max-width: 520px; margin: 0 auto; background: var(--card); color: var(--ink);
           border: 1px solid var(--line); border-radius: 12px; padding: 24px; }}
  h1 {{ font-size: 1.3rem; margin: 0 0 8px; }} p {{ margin: 0 0 12px; }}
  .dim {{ color: var(--dim); font-size: .92rem; }}
  button {{ font: inherit; font-weight: 600; padding: 12px 20px; border-radius: 8px;
            border: 0; background: var(--accent); color: var(--on-accent); cursor: pointer; }}
  input {{ font: inherit; width: 100%; padding: 10px 12px; border: 1px solid var(--line);
           border-radius: 8px; box-sizing: border-box; background: var(--field);
           color: var(--ink); }}
  label {{ display: block; margin: 16px 0 6px; font-weight: 600; font-size: .92rem; }}
  .err {{ color: var(--err); font-weight: 600; }}
  @media (max-width: 480px) {{ body {{ padding: 16px 12px; }} button {{ width: 100%; }} }}
</style></head><body><div class="card">
<h1>Connect {client} to FleetAlert</h1>
<p>FleetAlert AI is a demo of an agent that investigates fleet-maintenance alerts
and waits for a human before anything runs. Connecting gives
<strong>{client}</strong> read-only access: it can list alerts, summarise
investigations and check the daily budget, and cannot start or confirm anything.</p>
{error}
<form method="post" action="{action}">
<input type="hidden" name="txn" value="{txn}">
<label for="token">Operator key (optional)</label>
<input id="token" name="token" type="password" autocomplete="off"
       placeholder="leave empty to connect as a read-only visitor">
<p class="dim">An operator key raises the connector to that key's tier.
Visitors do not need one.</p>
<button type="submit">Connect</button>
</form>
<p class="dim" style="margin-top:16px">Source and decisions: github.com/mbgolden/fleetalert-mcp</p>
</div></body></html>"""


def render_consent(provider: StatelessOAuthProvider, txn: str, error: str | None = None) -> str:
    request = provider.consent_request(txn)
    client = html.escape(str(request["client_name"])) if request else "this client"
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return _PAGE.format(client=client, txn=html.escape(txn), action=CONSENT_PATH, error=err)


def add_consent_routes(server: MCPServer, provider: StatelessOAuthProvider) -> None:
    async def consent_get(request: Request) -> Response:
        txn = request.query_params.get("txn", "")
        if provider.consent_request(txn) is None:
            return HTMLResponse(
                render_consent(
                    provider, txn, "This sign-in request has expired; start again from your client."
                ),
                status_code=400,
            )
        return HTMLResponse(render_consent(provider, txn), headers={"Cache-Control": "no-store"})

    async def consent_post(request: Request) -> Response:
        form = await request.form()
        txn = str(form.get("txn", ""))
        token = str(form.get("token", "")).strip() or None
        try:
            location = provider.complete_consent(txn, token)
        except ValueError as exc:
            return HTMLResponse(render_consent(provider, txn, str(exc)), status_code=400)
        return RedirectResponse(location, status_code=302, headers={"Cache-Control": "no-store"})

    server.custom_route(CONSENT_PATH, methods=["GET"], include_in_schema=False)(consent_get)
    server.custom_route(CONSENT_PATH, methods=["POST"], include_in_schema=False)(consent_post)
