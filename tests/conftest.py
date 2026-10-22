"""Shared fixtures: a mocked FleetAlert API (respx) and a server factory.

Fixture data mirrors the live demo's shapes as of 2026-10-22, numbers as
strings included, since that is what DynamoDB hands back.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import respx
from mcp import Client
from mcp.client.session import ElicitationFnT
from mcp.types import ElicitRequestParams, ElicitResult

from fleetalert_mcp.config import Settings
from fleetalert_mcp.guard import Metrics
from fleetalert_mcp.server import build_server
from fleetalert_mcp.tiers import Tier

BASE = "https://api.test"

ALERTS: list[dict[str, Any]] = [
    {
        "alert_id": "ALERT-1001",
        "alert_type": "coolant_temp_spike",
        "created_at": "2026-09-17T08:00:00+00:00",
        "machine_id": "M-1001",
        "org_id": "org-demo",
        "severity": "high",
        "status": "open",
        "machine_name": "Truck 14 - Engine",
        "machine_type": "diesel_engine",
    },
    {
        "alert_id": "ALERT-1007",
        "alert_type": "coolant_temp_spike",
        "created_at": "2026-10-22T12:21:00+00:00",
        "machine_id": "M-1004",
        "org_id": "org-demo",
        "severity": "medium",
        "status": "awaiting_confirmation",
        "source": "autonomous",
        "machine_name": "Truck 31 - Engine",
        "machine_type": "diesel_engine",
    },
]

STATUS_1007: dict[str, Any] = {
    "alert_id": "ALERT-1007",
    "status": "awaiting_confirmation",
    "proposed_fix": "restart_sensor",
    "confidence": "0.9",
    "root_cause_summary": "KB entries disagree; telemetry supports KB-001 (one spike, level flat).",
    "confirmation_token": "f8c240ec-df48-439b-884a-91d3eb6a0d25",
    "source": "autonomous",
    "inbound_email": None,
    "detection": {"rule": "coolant_temp_c >= 105", "peak_value": "121", "tripped_readings": "1"},
}

STATUS_1001: dict[str, Any] = {
    "alert_id": "ALERT-1001",
    "status": "open",
    "proposed_fix": None,
    "confidence": None,
    "root_cause_summary": None,
    "confirmation_token": None,
    "source": "monitoring",
    "inbound_email": None,
    "detection": None,
}

USAGE: dict[str, Any] = {
    "day": "2026-10-22",
    "investigations": 5,
    "investigation_cap": 50,
    "cost_usd": 0.0767,
    "cost_cap_usd": 1.25,
    "exhausted": False,
    "resets_at": "2026-10-23T00:00:00+00:00",
}

ACTIVITY: list[dict[str, Any]] = [
    {
        "timestamp": "2026-10-22T12:23:43+00:00",
        "alert_id": "ALERT-1006",
        "trace_id": "trace-7c18",
        "entry_point": "email",
        "category": "end_state",
        "status": "success",
        "title": "Routed to support",
        "detail": "no human answer within 2 hours (restart_sensor)",
        "end_state": "routed_to_support",
        "count": 1,
        "alert_type": "coolant_temp_spike",
        "machine_name": "Truck 22 - Engine",
    },
    {
        "timestamp": "2026-10-22T12:21:19+00:00",
        "alert_id": "ALERT-1007",
        "trace_id": "trace-927d",
        "entry_point": "autonomous",
        "category": "outcome",
        "status": "success",
        "title": "Proposed restart_sensor",
        "detail": "waiting for a human to confirm or reject",
        "end_state": None,
        "count": 1,
    },
]


def _span(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "trace_id": "trace-A",
        "parent_span_id": "root-A",
        "alert_id": "ALERT-1007",
        "actor": "agent",
        "entry_point": "autonomous",
        "status": "success",
        "input": {},
        "output": {},
        "latency_ms": None,
        "attributes": {},
    }
    base.update(kw)
    return base


def trace_spans() -> list[dict[str, Any]]:
    """Two rounds: A ended awaiting confirmation, B (after a rejection) in flight."""
    return [
        _span(
            span_id="root-A",
            parent_span_id=None,
            name="investigation",
            kind="investigation",
            timestamp="2026-10-21T20:21:19.857+00:00",
            latency_ms="6799.9",
            input={"alert_type": "coolant_temp_spike", "machine_id": "M-1004"},
            output={
                "alert_id": "ALERT-1007",
                "confirmation_token": "[redacted]",
                "fix_id": "restart_sensor",
                "outcome": "awaiting_confirmation",
            },
            attributes={
                "estimated_cost_usd": "0.014748",
                "input_tokens": "4724",
                "output_tokens": "530",
                "model": "claude-sonnet-5-5",
                "model_calls": "2",
            },
        ),
        _span(
            span_id="A1",
            name="investigation_started",
            kind="lifecycle",
            actor="system",
            timestamp="2026-10-21T20:21:13.057+00:00",
        ),
        _span(
            span_id="A2",
            name="model_call",
            kind="model_call",
            timestamp="2026-10-21T20:21:15.838+00:00",
            latency_ms="2701.8",
            input={"iteration": "1"},
            output={
                "stop_reason": "tool_use",
                "text": "",
                "tool_calls": ["get_telemetry_snapshot", "search_knowledge_base"],
            },
            attributes={
                "estimated_cost_usd": "0.004468",
                "input_tokens": "1514",
                "output_tokens": "144",
            },
        ),
        _span(
            span_id="A3",
            parent_span_id="A2",
            name="get_telemetry_snapshot",
            kind="capability",
            timestamp="2026-10-21T20:21:15.938+00:00",
            latency_ms="55.9",
            attributes={"idempotent": True, "safety_tier": "read_only"},
        ),
        _span(
            span_id="A4",
            parent_span_id="A2",
            name="search_knowledge_base",
            kind="capability",
            timestamp="2026-10-21T20:21:16.038+00:00",
            latency_ms="60.5",
            attributes={"idempotent": True, "safety_tier": "read_only"},
        ),
        _span(
            span_id="A5",
            name="model_call",
            kind="model_call",
            timestamp="2026-10-21T20:21:19.680+00:00",
            latency_ms="3600.1",
            input={"iteration": "2"},
            output={"stop_reason": "tool_use", "text": "", "tool_calls": ["propose_fix"]},
            attributes={
                "estimated_cost_usd": "0.01028",
                "input_tokens": "3210",
                "output_tokens": "386",
            },
        ),
        _span(
            span_id="A6",
            parent_span_id="A5",
            name="propose_fix",
            kind="capability",
            timestamp="2026-10-21T20:21:19.697+00:00",
            latency_ms="0",
            input={"confidence": "0.9", "fix_id": "restart_sensor", "description": "..."},
            output={"fix_id": "restart_sensor", "received": True},
            attributes={"idempotent": True, "safety_tier": "proposes_action"},
        ),
        _span(
            span_id="A7",
            name="guardrail.whitelist",
            kind="decision",
            actor="system",
            timestamp="2026-10-21T20:21:19.757+00:00",
        ),
        _span(
            span_id="A8",
            name="awaiting_human_confirmation",
            kind="lifecycle",
            actor="system",
            timestamp="2026-10-21T20:21:22.778+00:00",
        ),
        _span(
            span_id="A9",
            name="reject",
            kind="human_action",
            actor="human",
            timestamp="2026-10-21T21:00:00.000+00:00",
            input={"reason": "sensor was replaced last week"},
        ),
        # Round B: re-investigation, no root span yet (still running).
        _span(
            span_id="B1",
            trace_id="trace-B",
            parent_span_id=None,
            name="investigation_started",
            kind="lifecycle",
            actor="system",
            timestamp="2026-10-21T21:00:01.000+00:00",
        ),
        _span(
            span_id="B2",
            trace_id="trace-B",
            parent_span_id=None,
            name="execute_fix",
            kind="capability",
            actor="agent",
            status="denied",
            timestamp="2026-10-21T21:00:02.000+00:00",
            attributes={"safety_tier": "executes_action"},
        ),
    ]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def upstream() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        router.get("/demo/alerts").respond(json={"alerts": ALERTS})
        router.get("/demo/alerts/ALERT-1007/status").respond(json=STATUS_1007)
        router.get("/demo/alerts/ALERT-1001/status").respond(json=STATUS_1001)
        router.get("/demo/alerts/NOPE/status").respond(404, json={"error": "No such alert: NOPE"})
        router.get("/demo/alerts/ALERT-1007/trace").respond(
            json={"alert_id": "ALERT-1007", "spans": trace_spans()}
        )
        router.get("/demo/alerts/ALERT-1001/trace").respond(
            json={"alert_id": "ALERT-1001", "spans": []}
        )
        router.get("/demo/usage").respond(json=USAGE)
        router.get("/demo/activity").respond(json={"events": ACTIVITY})
        router.post("/demo/alerts/ALERT-1001/investigate").respond(
            202, json={"alert_id": "ALERT-1001", "status": "investigation_started"}
        )
        router.post("/demo/alerts/ALERT-1007/confirm", name="confirm").respond(
            200, json={"alert_id": "ALERT-1007", "outcome": "confirmed"}
        )
        router.post("/demo/alerts/ALERT-1007/reject", name="reject").respond(
            200, json={"alert_id": "ALERT-1007", "outcome": "investigating"}
        )
        router.post("/demo/email").respond(
            409, json={"started": False, "alert_id": "ALERT-1006", "reason": "still in flight"}
        )
        yield router


def make_settings(tier: Tier = Tier.READ_ONLY, **overrides: Any) -> Settings:
    return Settings(_env_file=None, api_base_url=BASE, tier=tier, tokens="", **overrides)  # type: ignore[call-arg]


def make_client(
    settings: Settings,
    *,
    metrics: Metrics | None = None,
    elicitation_callback: ElicitationFnT | None = None,
) -> Client:
    server = build_server(settings, metrics=metrics)
    return Client(server, raise_exceptions=True, elicitation_callback=elicitation_callback)


@pytest.fixture
async def read_client(upstream: respx.MockRouter) -> AsyncIterator[Client]:
    async with make_client(make_settings()) as client:
        yield client


def approve_callback(approve: bool, note: str = "") -> ElicitationFnT:
    async def callback(context: Any, params: ElicitRequestParams) -> ElicitResult:
        return ElicitResult(action="accept", content={"approve": approve, "note": note})

    return callback


def decline_callback() -> ElicitationFnT:
    async def callback(context: Any, params: ElicitRequestParams) -> ElicitResult:
        return ElicitResult(action="decline")

    return callback
