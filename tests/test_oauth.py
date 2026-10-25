"""The stateless OAuth authorization server: unit flows, the consent page,
and a real SDK OAuth client through the Lambda shim end to end."""

from __future__ import annotations

import inspect
import json
import time
from collections.abc import Iterator
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from mcp import Client
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)
from pydantic import AnyUrl
from starlette.testclient import TestClient

from fleetalert_mcp import lambda_handler
from fleetalert_mcp.auth import parse_tokens
from fleetalert_mcp.config import Settings
from fleetalert_mcp.oauth import CONSENT_PATH, Sealer, StatelessOAuthProvider
from fleetalert_mcp.tiers import Tier
from tests.apigw_shim import make_event, serve
from tests.conftest import BASE

KEY = "k" * 64
OPS = "ops-token-0123456789abcdefghij"
TOKENS = parse_tokens(f"visitor:read_only:visitor-token-0123456789abcdef,ops:executes_action:{OPS}")
PUBLIC = "https://mcp.example.test"


def test_sealer_round_trip_expiry_and_isolation() -> None:
    a = Sealer(KEY, "access")
    token = a.seal({"x": 1}, ttl_seconds=60)
    assert token.startswith("fmca_")
    data = a.unseal(token)
    assert data is not None and data["x"] == 1
    assert Sealer(KEY, "refresh").unseal(token) is None  # another purpose cannot read it
    assert Sealer("z" * 64, "access").unseal(token) is None  # another key cannot either
    assert a.unseal(token[:-4] + "AAAA") is None  # tampered
    expired = a.seal({"x": 1}, ttl_seconds=-1)
    assert a.unseal(expired) is None
    with pytest.raises(ValueError):
        Sealer("short", "access")


@pytest.mark.anyio
async def test_provider_registration_consent_code_token_and_refresh() -> None:
    provider = StatelessOAuthProvider(KEY, PUBLIC, TOKENS)
    client = OAuthClientInformationFull(
        client_id="to-be-replaced",
        redirect_uris=[AnyUrl("http://127.0.0.1:9/callback")],
        client_name="Claude Desktop",
        token_endpoint_auth_method="none",
    )
    await provider.register_client(client)
    assert client.client_id.startswith("fmcc_")
    loaded = await provider.get_client(client.client_id)
    assert loaded is not None and loaded.client_name == "Claude Desktop"
    assert await provider.get_client("fmcc_nonsense") is None

    params = AuthorizationParams(
        state="st",
        scopes=None,
        code_challenge="chal",
        redirect_uri=AnyUrl("http://127.0.0.1:9/callback"),
        redirect_uri_provided_explicitly=True,
        resource=None,
    )
    consent_url = await provider.authorize(client, params)
    assert consent_url.startswith(f"{PUBLIC}{CONSENT_PATH}?txn=fmct_")
    txn = parse_qs(urlparse(consent_url).query)["txn"][0]
    assert provider.consent_request(txn)["client_name"] == "Claude Desktop"  # type: ignore[index]

    # Visitor: no key pasted -> read-only.
    location = provider.complete_consent(txn, None)
    q = parse_qs(urlparse(location).query)
    assert q["state"] == ["st"] and q["code"][0].startswith("fmca_")
    code = await provider.load_authorization_code(client, q["code"][0])
    assert code is not None and code.scopes == ["read_only"] and code.subject == "visitor"
    assert code.code_challenge == "chal" and code.resource == f"{PUBLIC}/mcp"
    other = OAuthClientInformationFull(client_id="fmcc_other", redirect_uris=None)
    assert (
        await provider.load_authorization_code(other, q["code"][0]) is None
    )  # bound to the client

    tokens = await provider.exchange_authorization_code(client, code)
    assert tokens.token_type == "Bearer" and tokens.scope == "read_only"
    access = await provider.load_access_token(tokens.access_token)
    assert access is not None and access.subject == "visitor" and access.scopes == ["read_only"]
    assert access.resource == f"{PUBLIC}/mcp" and access.claims == {"iss": PUBLIC}
    assert access.expires_at is not None and access.expires_at > time.time()

    assert tokens.refresh_token is not None
    rt = await provider.load_refresh_token(client, tokens.refresh_token)
    assert rt is not None and rt.subject == "visitor"
    refreshed = await provider.exchange_refresh_token(client, rt, [])
    assert (await provider.load_access_token(refreshed.access_token)) is not None
    from mcp.server.auth.provider import TokenError

    with pytest.raises(TokenError):
        await provider.exchange_refresh_token(client, rt, ["executes_action"])

    # Ops key pasted -> that principal's tier.
    location = provider.complete_consent(txn, OPS)
    code = await provider.load_authorization_code(
        client, parse_qs(urlparse(location).query)["code"][0]
    )
    assert code is not None and code.scopes == ["executes_action"] and code.subject == "ops"
    with pytest.raises(ValueError, match="not recognised"):
        provider.complete_consent(txn, "wrong-key-0123456789")
    with pytest.raises(ValueError, match="expired"):
        provider.complete_consent("fmct_bogus", None)


# -- end to end through the Lambda shim -----------------------------------------


class MemoryStorage(TokenStorage):
    def __init__(self) -> None:
        self.tokens: OAuthToken | None = None
        self.client: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client = client_info


@pytest.fixture
def oauth_lambda(upstream: respx.MockRouter, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    upstream.route(host="127.0.0.1").pass_through()
    server, port = serve(lambda_handler.handler)
    public = f"http://127.0.0.1:{port}"
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_base_url=BASE,
        tier=Tier.EXECUTES_ACTION,
        tokens="",
        public_url=public,
        oauth_enabled=True,
    )
    secret = {
        "tokens": f"visitor:read_only:visitor-token-0123456789abcdef,ops:executes_action:{OPS}",
        "state_key": KEY,
    }
    dep = lambda_handler.Deployment(settings, secret)
    monkeypatch.setattr(lambda_handler, "deployment", lambda: dep)
    try:
        yield public
    finally:
        server.shutdown()


def test_metadata_endpoints_through_the_lambda(oauth_lambda: str) -> None:
    rs = lambda_handler.handler(
        make_event("GET", "/.well-known/oauth-protected-resource/mcp", "", {"host": "x"}, b""), None
    )
    assert rs["statusCode"] == 200
    meta = json.loads(rs["body"])
    assert meta["resource"] == f"{oauth_lambda}/mcp"
    issuer = meta["authorization_servers"][0]
    assert issuer.rstrip("/") == oauth_lambda

    asm = lambda_handler.handler(
        make_event("GET", "/.well-known/oauth-authorization-server", "", {"host": "x"}, b""), None
    )
    assert asm["statusCode"] == 200
    doc = json.loads(asm["body"])
    assert doc["registration_endpoint"].endswith("/register")
    assert doc["authorization_endpoint"].endswith("/authorize")
    assert doc["token_endpoint"].endswith("/token")
    assert doc["code_challenge_methods_supported"] == ["S256"]
    assert "read_only" in doc["scopes_supported"]

    health = json.loads(
        lambda_handler.handler(make_event("GET", "/healthz", "", {"host": "x"}, b""), None)["body"]
    )
    assert health["auth"] == "bearer+oauth"


@pytest.mark.anyio
async def test_sdk_oauth_client_signs_in_as_visitor_through_the_lambda(oauth_lambda: str) -> None:
    """Dynamic registration, /authorize -> consent page -> code, /token, then
    tool calls with the issued token; every step through the Lambda handler."""
    captured: dict[str, str] = {}

    async def redirect_handler(url: str) -> None:
        # A browser would open this. The test drives the consent page itself.
        assert url.startswith(f"{oauth_lambda}/authorize")
        async with httpx.AsyncClient(follow_redirects=False) as browser:
            first = await browser.get(url)
            assert first.status_code == 302
            consent_url = first.headers["location"]
            assert consent_url.startswith(f"{oauth_lambda}{CONSENT_PATH}?txn=")
            page = await browser.get(consent_url)
            assert page.status_code == 200 and "Connect" in page.text and "read-only" in page.text
            txn = parse_qs(urlparse(consent_url).query)["txn"][0]
            done = await browser.post(
                f"{oauth_lambda}{CONSENT_PATH}", data={"txn": txn, "token": ""}
            )
            assert done.status_code == 302
            back = parse_qs(urlparse(done.headers["location"]).query)
            captured["code"] = back["code"][0]
            captured["state"] = back["state"][0]

    async def callback_handler() -> AuthorizationCodeResult:
        return AuthorizationCodeResult(code=captured["code"], state=captured["state"])

    storage = MemoryStorage()
    auth = OAuthClientProvider(
        server_url=f"{oauth_lambda}/mcp",
        client_metadata=OAuthClientMetadata(
            client_name="test desktop",
            redirect_uris=[AnyUrl("http://127.0.0.1:9/callback")],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
    import mcp.client.streamable_http as sh

    http_client_cls = (
        inspect.signature(sh.streamable_http_client).parameters["http_client"].annotation
    )
    try:
        import httpx2  # the SDK's HTTP library

        client_cls = httpx2.AsyncClient
    except ImportError:  # pragma: no cover
        client_cls = httpx.AsyncClient
    assert "AsyncClient" in str(http_client_cls)

    async with Client(
        sh.streamable_http_client(f"{oauth_lambda}/mcp", http_client=client_cls(auth=auth))
    ) as c:
        names = [t.name for t in (await c.list_tools()).tools]
        assert "list_alerts" in names
        usage = await c.call_tool("get_usage")
        assert usage.is_error is False, usage.content
        refused = await c.call_tool("reject_fix", {"alert_id": "ALERT-1007", "reason": "no"})
        assert refused.is_error is True  # visitor is read-only

    assert storage.client is not None and storage.client.client_id.startswith("fmcc_")
    assert storage.tokens is not None and storage.tokens.access_token.startswith("fmca_")


def test_consent_page_rejects_a_bad_transaction(oauth_lambda: str) -> None:
    dep = lambda_handler.deployment()
    with TestClient(dep.app()) as client:
        r = client.get(f"{CONSENT_PATH}?txn=fmct_nonsense")
        assert r.status_code == 400 and "expired" in r.text
        r = client.post(CONSENT_PATH, data={"txn": "fmct_nonsense", "token": ""})
        assert r.status_code == 400
