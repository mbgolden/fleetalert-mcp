from __future__ import annotations

import json

import pytest
import respx
from mcp import MCPError
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.types import TextContent

from fleetalert_mcp.guard import Metrics
from fleetalert_mcp.tiers import Tier
from tests.conftest import approve_callback, decline_callback, make_client, make_settings

pytestmark = pytest.mark.anyio


def text_of(result: object) -> str:
    content = getattr(result, "content", [])
    return "".join(c.text for c in content if isinstance(c, TextContent))


async def test_tier_controls_which_tools_exist(upstream: respx.MockRouter) -> None:
    async with make_client(make_settings(Tier.PROPOSES_ACTION)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert {"start_investigation", "simulate_inbound_email", "run_telemetry_detector"} <= names
    assert not {"confirm_fix", "reject_fix"} & names

    async with make_client(make_settings(Tier.EXECUTES_ACTION)) as client:
        tools = (await client.list_tools()).tools
    by_name = {t.name: t for t in tools}
    assert {"confirm_fix", "reject_fix"} <= set(by_name)
    assert by_name["confirm_fix"].annotations is not None
    assert by_name["confirm_fix"].annotations.destructive_hint is True
    # The human's decision is resolved server-side; the model only passes the alert id.
    assert list(by_name["confirm_fix"].input_schema["properties"]) == ["alert_id"]


async def test_start_investigation_reports_the_upstream_answer(upstream: respx.MockRouter) -> None:
    async with make_client(make_settings(Tier.PROPOSES_ACTION)) as client:
        result = await client.call_tool("start_investigation", {"alert_id": "ALERT-1001"})
        assert result.is_error is False
        assert result.structured_content is not None
        assert result.structured_content["accepted"] is True
        assert result.structured_content["http_status"] == 202

        busy = await client.call_tool("simulate_inbound_email")
        assert busy.is_error is False  # 409 is an answer, not a failure
        assert busy.structured_content is not None
        assert busy.structured_content["accepted"] is False
        assert busy.structured_content["reason"] == "still in flight"


async def test_confirm_fix_asks_the_human_then_sends_the_token(upstream: respx.MockRouter) -> None:
    settings = make_settings(Tier.EXECUTES_ACTION)
    async with make_client(settings, elicitation_callback=approve_callback(True)) as client:
        result = await client.call_tool("confirm_fix", {"alert_id": "ALERT-1007"})
    assert result.is_error is False, text_of(result)
    assert result.structured_content is not None
    assert result.structured_content["outcome"] == "confirmed"
    confirm = upstream["confirm"]
    assert confirm.called
    sent = json.loads(confirm.calls.last.request.content)
    assert sent == {"confirmation_token": "f8c240ec-df48-439b-884a-91d3eb6a0d25"}
    # The token went upstream, not into the model-visible result.
    assert "f8c240ec" not in text_of(result)


async def test_confirm_fix_without_approval_sends_nothing(upstream: respx.MockRouter) -> None:
    settings = make_settings(Tier.EXECUTES_ACTION)
    confirm = upstream["confirm"]

    async with make_client(settings, elicitation_callback=approve_callback(False, "wait")) as c:
        result = await c.call_tool("confirm_fix", {"alert_id": "ALERT-1007"})
    assert result.structured_content is not None
    assert result.structured_content["outcome"] == "not_confirmed"
    assert "wait" in result.structured_content["reason"]

    async with make_client(settings, elicitation_callback=decline_callback()) as c:
        result = await c.call_tool("confirm_fix", {"alert_id": "ALERT-1007"})
    assert result.structured_content is not None
    assert result.structured_content["reason"] == "human declined"

    assert not confirm.called


async def test_confirm_fix_refuses_when_the_alert_is_not_awaiting(
    upstream: respx.MockRouter,
) -> None:
    settings = make_settings(Tier.EXECUTES_ACTION)
    async with make_client(settings, elicitation_callback=approve_callback(True)) as client:
        result = await client.call_tool("confirm_fix", {"alert_id": "ALERT-1001"})
    assert result.is_error is True
    assert "not awaiting_confirmation" in text_of(result)


async def test_confirm_fix_needs_a_client_that_can_ask(upstream: respx.MockRouter) -> None:
    settings = make_settings(Tier.EXECUTES_ACTION)
    confirm = upstream["confirm"]
    async with make_client(settings) as client:  # no elicitation callback
        try:
            result = await client.call_tool("confirm_fix", {"alert_id": "ALERT-1007"})
        except MCPError as exc:
            message = str(exc)
        else:
            assert result.is_error is True
            message = text_of(result)
    assert "elicitation" in message.lower()
    assert not confirm.called


async def test_reject_fix(upstream: respx.MockRouter) -> None:
    async with make_client(make_settings(Tier.EXECUTES_ACTION)) as client:
        result = await client.call_tool(
            "reject_fix", {"alert_id": "ALERT-1007", "reason": "sensor replaced last week"}
        )
    assert result.structured_content is not None
    assert result.structured_content["outcome"] == "investigating"
    reject = upstream["reject"]
    assert json.loads(reject.calls.last.request.content) == {"reason": "sensor replaced last week"}


async def test_a_token_can_narrow_but_not_widen_the_tier(upstream: respx.MockRouter) -> None:
    """A read_only token on an executes_action server may list but not act."""
    metrics = Metrics()
    user = AuthenticatedUser(
        AccessToken(token="x", client_id="ci-bot", scopes=["read_only"], expires_at=None)
    )
    reset = auth_context_var.set(user)
    try:
        async with make_client(make_settings(Tier.EXECUTES_ACTION), metrics=metrics) as client:
            ok = await client.call_tool("list_alerts")
            refused = await client.call_tool(
                "reject_fix", {"alert_id": "ALERT-1007", "reason": "nope"}
            )
    finally:
        auth_context_var.reset(reset)
    assert ok.is_error is False
    assert refused.is_error is True
    assert "executes_action" in text_of(refused) and "read_only" in text_of(refused)
    assert metrics.refused_tier["reject_fix"] == 1
    assert metrics.calls["list_alerts"] == 1
