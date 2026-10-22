"""Read-only smoke test against the real FleetAlert demo.

Skipped unless FLEETALERT_MCP_LIVE=1. Costs nothing upstream (reads only).
"""

from __future__ import annotations

import os

import pytest
from mcp import Client

from fleetalert_mcp.config import Settings
from fleetalert_mcp.server import build_server

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("FLEETALERT_MCP_LIVE") != "1", reason="set FLEETALERT_MCP_LIVE=1"
    ),
]


async def test_live_read_path() -> None:
    settings = Settings(_env_file=None, tokens="")  # type: ignore[call-arg]
    async with Client(build_server(settings), raise_exceptions=True) as client:
        alerts = await client.call_tool("list_alerts")
        assert alerts.is_error is False
        assert alerts.structured_content is not None
        assert alerts.structured_content["count"] >= 5
        first = alerts.structured_content["alerts"][0]["alert_id"]

        status = await client.call_tool("get_alert", {"alert_id": first})
        assert status.is_error is False
        assert "confirmation_token" not in (status.structured_content or {})

        trace = await client.call_tool("get_trace", {"alert_id": first})
        assert trace.is_error is False

        usage = await client.call_tool("get_usage")
        assert usage.structured_content is not None
        assert usage.structured_content["cost_cap_usd"] > 0
