"""Typed async client for the FleetAlert demo API.

The API is public and unauthenticated (FleetAlert's demo is bounded by a
daily cost guard and fixed scenarios instead). Everything DynamoDB stores as a
number comes back as a string, so numeric fields are typed as numbers and
pydantic coerces them.

One rule lives here on purpose: `AlertStatus` has no `confirmation_token`
field. The token is what a human's Confirm sends upstream to execute a fix.
It is fetched separately, by `confirmation_token()`, and only after a human
has approved through elicitation (see server.py). The model never sees it in
a tool result.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field


class UpstreamError(Exception):
    """A FleetAlert API call that did not succeed, with the HTTP status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message

    def __str__(self) -> str:
        return f"FleetAlert API {self.status}: {self.message}"


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Alert(_Model):
    alert_id: str
    machine_id: str
    machine_name: str | None = None
    machine_type: str | None = None
    org_id: str | None = None
    alert_type: str
    severity: str
    status: str
    created_at: str
    source: str = "monitoring"


class AlertList(_Model):
    alerts: list[Alert]
    count: int


class AlertStatus(_Model):
    """`GET /demo/alerts/{id}/status`, minus the confirmation token."""

    alert_id: str
    status: str
    proposed_fix: str | None = None
    confidence: float | None = None
    root_cause_summary: str | None = None
    source: str = "monitoring"
    inbound_email: dict[str, Any] | None = None
    detection: dict[str, Any] | None = None


class Span(_Model):
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    alert_id: str
    name: str
    kind: str
    actor: str
    entry_point: str | None = None
    status: str
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    latency_ms: float | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    timestamp: str


class Usage(_Model):
    """Today's spend against the demo's daily cost guard (FleetAlert ADR-0017)."""

    day: str
    investigations: int
    investigation_cap: int
    cost_usd: float
    cost_cap_usd: float
    exhausted: bool
    resets_at: str


class ActivityEvent(_Model):
    timestamp: str
    alert_id: str
    trace_id: str | None = None
    entry_point: str | None = None
    category: str
    status: str
    title: str
    detail: str = ""
    end_state: str | None = None
    count: int = 1
    alert_type: str | None = None
    machine_name: str | None = None


class ActivityFeed(_Model):
    events: list[ActivityEvent]
    count: int


class ActionOutcome(_Model):
    """What an action tool did upstream. `accepted` is false for the normal
    "not now" answers (busy, budget exhausted), which are not errors."""

    accepted: bool
    http_status: int
    alert_id: str | None = None
    outcome: str | None = None
    reason: str | None = None
    budget_exhausted: bool = False
    detail: dict[str, Any] = Field(default_factory=dict)


class FleetAlertAPI:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 10.0,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            headers={"User-Agent": "fleetalert-mcp"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- reads ---------------------------------------------------------------

    async def list_alerts(self) -> AlertList:
        body = await self._get("/demo/alerts")
        alerts = [Alert.model_validate(a) for a in body.get("alerts", [])]
        return AlertList(alerts=alerts, count=len(alerts))

    async def alert_status(self, alert_id: str) -> AlertStatus:
        return AlertStatus.model_validate(await self._get(f"/demo/alerts/{alert_id}/status"))

    async def confirmation_token(self, alert_id: str) -> str | None:
        """The token a human's Confirm needs. Kept off every tool result."""
        body = await self._get(f"/demo/alerts/{alert_id}/status")
        token = body.get("confirmation_token")
        return str(token) if token else None

    async def trace(self, alert_id: str) -> list[Span]:
        body = await self._get(f"/demo/alerts/{alert_id}/trace")
        return [Span.model_validate(s) for s in body.get("spans", [])]

    async def usage(self) -> Usage:
        return Usage.model_validate(await self._get("/demo/usage"))

    async def activity(self) -> ActivityFeed:
        body = await self._get("/demo/activity")
        events = [ActivityEvent.model_validate(e) for e in body.get("events", [])]
        return ActivityFeed(events=events, count=len(events))

    # -- actions -------------------------------------------------------------

    async def start_investigation(self, alert_id: str) -> ActionOutcome:
        # Declares this round as MCP-started; FleetAlert tags the trace and the
        # Activity feed with it (its ADR-0026, this repo's ADR-0006).
        return await self._action(
            "POST", f"/demo/alerts/{alert_id}/investigate", headers=ENTRY_POINT_HEADER
        )

    async def simulate_inbound_email(self) -> ActionOutcome:
        return await self._action("POST", "/demo/email")

    async def run_detector(self) -> ActionOutcome:
        return await self._action("POST", "/demo/detect")

    async def confirm_fix(self, alert_id: str, confirmation_token: str) -> ActionOutcome:
        return await self._action(
            "POST",
            f"/demo/alerts/{alert_id}/confirm",
            json={"confirmation_token": confirmation_token},
        )

    async def reject_fix(self, alert_id: str, reason: str | None) -> ActionOutcome:
        return await self._action(
            "POST", f"/demo/alerts/{alert_id}/reject", json={"reason": reason}
        )

    # -- plumbing ------------------------------------------------------------

    async def _get(self, path: str) -> dict[str, Any]:
        status, body = await self._request("GET", path)
        if status >= 400:
            raise UpstreamError(status, _error_message(body, status))
        return body

    async def _action(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> ActionOutcome:
        status, body = await self._request(method, path, json=json, headers=headers)
        # 409 (last alert still in flight) and 429 (daily budget spent) are
        # expected answers from a public demo, not failures: the model should
        # read them and stop, not retry.
        if status >= 400 and status not in (409, 429):
            raise UpstreamError(status, _error_message(body, status))
        return ActionOutcome(
            accepted=status < 400,
            http_status=status,
            alert_id=_opt_str(body.get("alert_id")),
            outcome=_opt_str(body.get("outcome") or body.get("status")),
            reason=_opt_str(body.get("reason") or body.get("error")),
            budget_exhausted=bool(body.get("budget_exhausted")) or status == 429,
            detail={k: v for k, v in body.items() if k not in _SECRET_KEYS},
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        try:
            response = await self._client.request(method, path, json=json, headers=headers)
        except httpx.TimeoutException as exc:
            raise UpstreamError(504, "the FleetAlert API timed out") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(502, f"could not reach the FleetAlert API: {exc}") from exc
        body: Any = {}
        if response.content:
            try:
                body = response.json()
            except ValueError:
                body = {"error": response.text[:200]}
        if not isinstance(body, dict):
            body = {"result": body}
        return response.status_code, body


ENTRY_POINT_HEADER = {"x-fleetalert-entry-point": "mcp"}
_SECRET_KEYS = frozenset({"confirmation_token", "step_functions_task_token", "task_token"})


def _error_message(body: dict[str, Any], status: int) -> str:
    message = body.get("error") or body.get("reason")
    return str(message) if message else f"HTTP {status}"


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)
