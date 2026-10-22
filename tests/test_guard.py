from __future__ import annotations

import json
import logging

import pytest
import respx
from mcp.types import TextContent

from fleetalert_mcp.guard import Metrics
from fleetalert_mcp.ratelimit import TokenBucket
from fleetalert_mcp.tiers import Tier
from tests.conftest import make_client, make_settings

pytestmark = pytest.mark.anyio


def text_of(result: object) -> str:
    content = getattr(result, "content", [])
    return "".join(c.text for c in content if isinstance(c, TextContent))


def test_token_bucket_refills_over_time() -> None:
    now = [0.0]
    bucket = TokenBucket(2, 1.0, clock=lambda: now[0])
    assert bucket.try_acquire("k") == 0.0
    assert bucket.try_acquire("k") == 0.0
    wait = bucket.try_acquire("k")
    assert wait == pytest.approx(1.0)
    now[0] += 0.5
    assert bucket.try_acquire("k") == pytest.approx(0.5)
    now[0] += 0.5
    assert bucket.try_acquire("k") == 0.0
    assert bucket.try_acquire("other") == 0.0  # keys are independent


async def test_per_principal_rate_limit_is_a_readable_error(
    upstream: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="fleetalert_mcp.audit")
    metrics = Metrics()
    settings = make_settings(calls_per_minute=2)
    async with make_client(settings, metrics=metrics) as client:
        assert (await client.call_tool("get_usage")).is_error is False
        assert (await client.call_tool("get_usage")).is_error is False
        third = await client.call_tool("get_usage")
    assert third.is_error is True
    assert "Rate limit reached (call)" in text_of(third)
    assert "Retry in" in text_of(third)
    assert metrics.calls["get_usage"] == 2
    assert metrics.rate_limited["get_usage"] == 1

    records = [
        json.loads(r.getMessage()) for r in caplog.records if r.name == "fleetalert_mcp.audit"
    ]
    assert [r["outcome"] for r in records] == ["ok", "ok", "rate_limited"]
    assert records[0]["principal"] == "local"
    assert records[0]["tool"] == "get_usage"
    assert records[0]["tool_tier"] == "read_only"
    assert records[2]["retry_after_s"] >= 1


async def test_action_tools_have_their_own_tighter_budget(upstream: respx.MockRouter) -> None:
    settings = make_settings(Tier.PROPOSES_ACTION, actions_per_hour=1, calls_per_minute=100)
    async with make_client(settings) as client:
        first = await client.call_tool("start_investigation", {"alert_id": "ALERT-1001"})
        second = await client.call_tool("start_investigation", {"alert_id": "ALERT-1001"})
        reads_still_work = await client.call_tool("get_usage")
    assert first.is_error is False
    assert second.is_error is True
    assert "Rate limit reached (action)" in text_of(second)
    assert reads_still_work.is_error is False


async def test_metrics_snapshot_counts_errors(upstream: respx.MockRouter) -> None:
    metrics = Metrics()
    async with make_client(make_settings(), metrics=metrics) as client:
        await client.call_tool("get_alert", {"alert_id": "ALERT-1007"})
        await client.call_tool("get_alert", {"alert_id": "NOPE"})
    snap = metrics.snapshot()
    assert snap["tools"]["get_alert"]["calls"] == 2
    assert snap["tools"]["get_alert"]["errors"] == 1
    assert snap["tools"]["get_alert"]["avg_ms"] is not None
    assert snap["totals"] == {"calls": 2, "errors": 1, "refused_tier": 0, "rate_limited": 0}
