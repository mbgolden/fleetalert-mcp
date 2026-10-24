"""The Lambda entry point, end to end: real MCP client -> local API Gateway
shim -> Mangum -> the stateless JSON server -> mocked FleetAlert API."""

from __future__ import annotations

import inspect
import json
from collections.abc import Iterator

import httpx
import pytest
import respx
from mcp import Client
from mcp.types import ElicitRequestParams, ElicitResult

from fleetalert_mcp import lambda_handler
from fleetalert_mcp.config import Settings
from fleetalert_mcp.tiers import Tier
from tests.apigw_shim import make_event, serve
from tests.conftest import BASE

VISITOR = "visitor-token-0123456789abcdef"
OPS = "ops-token-0123456789abcdefghij"
STATE_KEY = "f" * 64


@pytest.fixture
def lambda_url(upstream: respx.MockRouter, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    settings = Settings(_env_file=None, api_base_url=BASE, tier=Tier.EXECUTES_ACTION, tokens="")  # type: ignore[call-arg]
    secret = {
        "tokens": f"visitor:read_only:{VISITOR},ops:executes_action:{OPS}",
        "state_key": STATE_KEY,
    }
    # The client's own traffic to the local shim must reach the socket, not the mock.
    upstream.route(host="127.0.0.1").pass_through()
    server, port = serve(lambda_handler.handler)
    public = f"http://127.0.0.1:{port}"
    settings.public_url = public
    # Install the deployment the handler will use (no AWS, no env).
    dep = lambda_handler.Deployment(settings, secret)
    monkeypatch.setattr(lambda_handler, "deployment", lambda: dep)
    try:
        yield public
    finally:
        server.shutdown()


def transport(url: str, token: str | None):  # type: ignore[no-untyped-def]
    import mcp.client.streamable_http as sh

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    sig = inspect.signature(sh.streamable_http_client)
    if "headers" in sig.parameters:
        return sh.streamable_http_client(url, headers=headers)
    return sh.streamable_http_client(url, http_client=httpx.AsyncClient(headers=headers))


def test_healthz_and_unauthenticated_mcp_via_raw_events(lambda_url: str) -> None:
    health = lambda_handler.handler(make_event("GET", "/healthz", "", {"host": "x"}, b""), None)
    assert health["statusCode"] == 200
    body = json.loads(health["body"])
    assert body["deployment"] == "lambda" and body["clients"] == ["visitor", "ops"]

    denied = lambda_handler.handler(
        make_event(
            "POST",
            "/mcp",
            "",
            {"host": "x", "content-type": "application/json", "accept": "application/json"},
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
        ),
        None,
    )
    assert denied["statusCode"] == 401
    www = {k.lower(): v for k, v in denied["headers"].items()}["www-authenticate"]
    assert "oauth-protected-resource" in www


@pytest.mark.anyio
async def test_real_client_reads_through_the_lambda(lambda_url: str) -> None:
    async with Client(transport(f"{lambda_url}/mcp", VISITOR)) as c:
        names = [t.name for t in (await c.list_tools()).tools]
        assert "confirm_fix" in names  # listed (server tier), refused for this token
        usage = await c.call_tool("get_usage")
        assert usage.is_error is False
        assert usage.structured_content is not None
        assert usage.structured_content["cost_cap_usd"] == 1.25
        refused = await c.call_tool("reject_fix", {"alert_id": "ALERT-1007", "reason": "no"})
        assert refused.is_error is True


@pytest.mark.anyio
async def test_confirm_fix_round_trip_survives_stateless_requests(
    lambda_url: str, upstream: respx.MockRouter
) -> None:
    """Two separate Lambda invocations: the first returns the elicitation, the
    second carries the human's answer back with the sealed state."""
    seen: list[str] = []

    async def approve(context: object, params: ElicitRequestParams) -> ElicitResult:
        seen.append(getattr(params, "message", ""))
        return ElicitResult(action="accept", content={"approve": True, "note": "ok"})

    async with Client(transport(f"{lambda_url}/mcp", OPS), elicitation_callback=approve) as c:
        result = await c.call_tool("confirm_fix", {"alert_id": "ALERT-1007"})
    assert result.is_error is False, result.content
    assert result.structured_content is not None
    assert result.structured_content["outcome"] == "confirmed"
    assert seen and "restart_sensor" in seen[0]
    confirm = upstream["confirm"]
    assert confirm.called
    assert json.loads(confirm.calls.last.request.content) == {
        "confirmation_token": "f8c240ec-df48-439b-884a-91d3eb6a0d25"
    }


def test_deployment_without_secret_refuses_everything() -> None:
    settings = Settings(_env_file=None, api_base_url=BASE, tokens="", public_url="http://x")  # type: ignore[call-arg]
    dep = lambda_handler.Deployment(settings, {})
    assert dep.tokens == [] and dep.state_keys is None
    app = dep.app()
    assert app is not None
    assert lambda_handler.load_secret(None) == {}
