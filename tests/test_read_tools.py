from __future__ import annotations

import pytest
from mcp import Client
from mcp.types import TextContent

pytestmark = pytest.mark.anyio


async def test_read_only_server_lists_exactly_the_read_tools(read_client: Client) -> None:
    tools = await read_client.list_tools()
    names = sorted(t.name for t in tools.tools)
    assert names == sorted(
        ["list_alerts", "get_alert", "get_trace", "get_raw_spans", "get_usage", "get_activity"]
    )
    for tool in tools.tools:
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.output_schema is not None, tool.name


async def test_list_alerts_is_structured(read_client: Client) -> None:
    result = await read_client.call_tool("list_alerts")
    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["count"] == 2
    ids = [a["alert_id"] for a in result.structured_content["alerts"]]
    assert ids == ["ALERT-1001", "ALERT-1007"]


async def test_get_alert_never_returns_the_confirmation_token(read_client: Client) -> None:
    result = await read_client.call_tool("get_alert", {"alert_id": "ALERT-1007"})
    assert result.is_error is False
    data = result.structured_content
    assert data is not None
    assert data["status"] == "awaiting_confirmation"
    assert data["proposed_fix"] == "restart_sensor"
    assert data["confidence"] == 0.9  # coerced from the API's "0.9"
    assert "confirmation_token" not in data
    text = "".join(c.text for c in result.content if isinstance(c, TextContent))
    assert "f8c240ec" not in text


async def test_unknown_alert_is_a_tool_error_the_model_can_read(read_client: Client) -> None:
    result = await read_client.call_tool("get_alert", {"alert_id": "NOPE"})
    assert result.is_error is True
    text = "".join(c.text for c in result.content if isinstance(c, TextContent))
    assert "404" in text and "No such alert" in text


async def test_bad_alert_id_is_rejected_before_any_upstream_call(read_client: Client) -> None:
    result = await read_client.call_tool("get_alert", {"alert_id": "../admin"})
    assert result.is_error is True


async def test_get_trace_summarises_rounds(read_client: Client) -> None:
    result = await read_client.call_tool("get_trace", {"alert_id": "ALERT-1007"})
    assert result.is_error is False
    summary = result.structured_content
    assert summary is not None
    assert summary["span_count"] == 12
    assert len(summary["rounds"]) == 2
    first, second = summary["rounds"]
    assert first["outcome"] == "awaiting_confirmation"
    assert first["fix_id"] == "restart_sensor"
    assert first["model_calls"] == 2
    assert first["cost_usd"] == pytest.approx(0.014748)
    assert [s["name"] for s in first["steps"]][:3] == [
        "investigation_started",
        "model_call",
        "get_telemetry_snapshot",
    ]
    assert second["outcome"] is None  # still running
    assert second["denied_calls"] == 1
    assert summary["total_cost_usd"] == pytest.approx(0.014748)


async def test_get_raw_spans_filters_and_limits(read_client: Client) -> None:
    result = await read_client.call_tool(
        "get_raw_spans", {"alert_id": "ALERT-1007", "kind": "model_call", "limit": 1}
    )
    assert result.is_error is False
    data = result.structured_content
    assert data is not None
    assert data["total"] == 2 and data["returned"] == 1
    assert data["spans"][0]["span_id"] == "A5"


async def test_usage_and_activity(read_client: Client) -> None:
    usage = await read_client.call_tool("get_usage")
    assert usage.structured_content is not None
    assert usage.structured_content["exhausted"] is False
    assert usage.structured_content["cost_cap_usd"] == 1.25

    activity = await read_client.call_tool("get_activity", {"limit": 1})
    assert activity.structured_content is not None
    assert activity.structured_content["count"] == 1
    assert activity.structured_content["events"][0]["alert_id"] == "ALERT-1006"


async def test_resources_and_prompt(read_client: Client) -> None:
    resources = await read_client.list_resources()
    assert [str(r.uri) for r in resources.resources] == ["fleetalert://alerts"]
    templates = await read_client.list_resource_templates()
    assert [t.uri_template for t in templates.resource_templates] == [
        "fleetalert://alerts/{alert_id}/trace"
    ]
    read = await read_client.read_resource("fleetalert://alerts/ALERT-1007/trace")
    assert "awaiting_confirmation" in getattr(read.contents[0], "text", "")

    prompts = await read_client.list_prompts()
    assert [p.name for p in prompts.prompts] == ["triage_alert"]
    prompt = await read_client.get_prompt("triage_alert", {"alert_id": "ALERT-1007"})
    assert "ALERT-1007" in getattr(prompt.messages[0].content, "text", "")
