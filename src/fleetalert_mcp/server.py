"""The MCP server: tools, resources and a prompt over the FleetAlert API.

Tools are registered by tier (tiers.py). At the default `read_only` tier the
action tools do not exist on the wire at all, so a client cannot discover
them, let alone call them. `confirm_fix` is the one tool that makes
something execute upstream, and it asks the human through MCP elicitation
before it fetches the confirmation token; the model supplies the alert id
and nothing else.
"""

# No `from __future__ import annotations` here on purpose: the SDK evaluates tool
# signatures with eval_str, and `Resolve(ask_human)` names a closure that string
# annotations could not resolve.
from collections.abc import Awaitable
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.auth.provider import OAuthAuthorizationServerProvider, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import (
    AcceptedElicitation,
    CancelledElicitation,
    Context,
    DeclinedElicitation,
    Elicit,
    ElicitationResult,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.request_state import RequestStateSecurity
from mcp.types import ClientCapabilities, ElicitationCapability, ToolAnnotations
from pydantic import BaseModel, Field

from fleetalert_mcp import __version__
from fleetalert_mcp.config import Settings
from fleetalert_mcp.guard import Metrics, make_guard
from fleetalert_mcp.summarize import TraceSummary, summarize_trace
from fleetalert_mcp.tiers import Tier
from fleetalert_mcp.upstream import (
    ActionOutcome,
    ActivityFeed,
    AlertList,
    AlertStatus,
    FleetAlertAPI,
    Span,
    UpstreamError,
    Usage,
)

AlertId = Annotated[
    str,
    Field(description="FleetAlert alert id, e.g. ALERT-1001.", pattern=r"^[A-Za-z0-9_-]{1,64}$"),
]

INSTRUCTIONS = """\
FleetAlert AI is a live demo of an agent that investigates fleet-maintenance
alerts and proposes a fix which a human must confirm before it runs.

Start with list_alerts, then get_alert and get_trace for one alert. A trace
summary shows each investigation round: what the agent read, what it
proposed, what the guardrails decided, and what it cost.

Tools are tiered like FleetAlert's own capabilities. read_only tools are
always available. proposes_action tools (start_investigation and the two
simulated entry points) spend the demo's daily model budget; check
get_usage first and never loop on them. executes_action tools confirm or
reject a proposed fix; confirm_fix asks the human directly before anything
executes, so do not try to confirm on their behalf. If a tool is missing,
this server is running at a lower tier on purpose.
"""


class HumanDecision(BaseModel):
    """What elicitation asks the human. Flat on purpose: elicitation schemas
    must be primitives only."""

    approve: bool = Field(description="Execute this fix on the machine?")
    note: str = Field(default="", description="Optional note for the audit trail.")


class RawSpans(BaseModel):
    alert_id: str
    total: int
    returned: int
    spans: list[Span]


class ToolCatalog(dict[str, Tier]):
    """Registered tool name -> tier. Shared with the guard middleware."""


def build_server(
    settings: Settings,
    *,
    api: FleetAlertAPI | None = None,
    metrics: Metrics | None = None,
    token_verifier: TokenVerifier | None = None,
    auth: AuthSettings | None = None,
    request_state_keys: list[str] | None = None,
    auth_server_provider: OAuthAuthorizationServerProvider[Any, Any, Any] | None = None,
) -> MCPServer:
    api = api or FleetAlertAPI(settings.api_base_url, settings.upstream_timeout_seconds)
    metrics = metrics or Metrics()
    catalog = ToolCatalog()

    mcp = MCPServer(
        name="FleetAlert",
        title="FleetAlert AI",
        description="Alerts, investigation traces and human-gated actions for FleetAlert AI.",
        instructions=INSTRUCTIONS,
        website_url="https://fleetalert.10finger.dev",
        version=__version__,
        token_verifier=token_verifier,
        auth=auth,
        auth_server_provider=auth_server_provider,
        middleware=[make_guard(settings, catalog, metrics)],
        # Elicitation state (confirm_fix) is sealed with this key; shared across
        # instances when given, per process otherwise.
        request_state_security=(
            RequestStateSecurity(keys=request_state_keys) if request_state_keys else None
        ),
    )
    mcp.state = {"metrics": metrics, "catalog": catalog, "settings": settings}  # type: ignore[attr-defined]

    def register(fn: Any, tier: Tier, annotations: ToolAnnotations) -> None:
        if settings.tier.allows(tier):
            mcp.add_tool(fn, annotations=annotations)
            catalog[fn.__name__] = tier

    read = ToolAnnotations(read_only_hint=True, open_world_hint=False, idempotent_hint=True)
    spends = ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
    )
    executes = ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
    )

    # -- read_only -----------------------------------------------------------

    async def list_alerts() -> AlertList:
        """List every demo alert with its machine, severity and current status.

        Statuses: open, queued, investigating, awaiting_confirmation,
        resolved, rejected, routed_to_support, failed.
        """
        return await _upstream(api.list_alerts())

    async def get_alert(alert_id: AlertId) -> AlertStatus:
        """One alert's current status and, once investigated, the proposed fix,
        the agent's confidence and its root-cause summary. For email- or
        detector-raised alerts, the inbound email or detection rule too."""
        return await _upstream(api.alert_status(alert_id))

    async def get_trace(alert_id: AlertId) -> TraceSummary:
        """The investigation history of an alert, one entry per round: entry
        point, outcome, proposed fix, model calls, tokens, cost, and the
        ordered steps (model calls, capability calls, guardrail decisions,
        human actions). Use get_raw_spans for full inputs and outputs."""
        spans = await _upstream(api.trace(alert_id))
        return summarize_trace(alert_id, spans)

    async def get_raw_spans(
        alert_id: AlertId,
        limit: Annotated[int, Field(ge=1, le=200, description="Most recent spans to return.")] = 50,
        kind: Annotated[
            Literal["all", "model_call", "capability", "decision", "human_action", "lifecycle"],
            Field(description="Filter by span kind."),
        ] = "all",
    ) -> RawSpans:
        """Raw trace spans for an alert, newest last, with full input and
        output. Large; prefer get_trace unless you need the detail."""
        spans = await _upstream(api.trace(alert_id))
        if kind != "all":
            spans = [s for s in spans if s.kind == kind]
        spans.sort(key=lambda s: s.timestamp)
        selected = spans[-limit:]
        return RawSpans(alert_id=alert_id, total=len(spans), returned=len(selected), spans=selected)

    async def get_usage() -> Usage:
        """Today's investigations and model spend against the demo's daily
        cost cap. When exhausted, action tools will be refused upstream."""
        return await _upstream(api.usage())

    async def get_activity(
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> ActivityFeed:
        """Recent cross-alert activity: end states, guardrail decisions, human
        confirm/reject actions and proposals, newest first."""
        feed = await _upstream(api.activity())
        events = feed.events[:limit]
        return ActivityFeed(events=events, count=len(events))

    register(list_alerts, Tier.READ_ONLY, read)
    register(get_alert, Tier.READ_ONLY, read)
    register(get_trace, Tier.READ_ONLY, read)
    register(get_raw_spans, Tier.READ_ONLY, read)
    register(get_usage, Tier.READ_ONLY, read)
    register(get_activity, Tier.READ_ONLY, read)

    # -- proposes_action -----------------------------------------------------

    async def start_investigation(alert_id: AlertId) -> ActionOutcome:
        """Start an agent investigation round on an open alert. Spends model
        budget; the round ends awaiting a human, never executing anything.
        Poll get_alert for the result. Not accepted when the daily budget is
        exhausted."""
        return await _upstream(api.start_investigation(alert_id))

    async def simulate_inbound_email() -> ActionOutcome:
        """Deliver the demo's seeded inbound maintenance email, which raises
        ALERT-1006 and investigates it. Not accepted (409) while the last
        email alert is still in flight."""
        return await _upstream(api.simulate_inbound_email())

    async def run_telemetry_detector() -> ActionOutcome:
        """Run the rule-based telemetry detector once. It raises ALERT-1007
        and investigates when a rule trips; 200 with nothing raised when the
        readings are normal."""
        return await _upstream(api.run_detector())

    register(start_investigation, Tier.PROPOSES_ACTION, spends)
    register(simulate_inbound_email, Tier.PROPOSES_ACTION, spends)
    register(run_telemetry_detector, Tier.PROPOSES_ACTION, spends)

    # -- executes_action -----------------------------------------------------

    async def ask_human(alert_id: str, ctx: Context[Any, Any]) -> Elicit[HumanDecision]:
        """Resolver for confirm_fix: puts the proposal in front of the human."""
        if not ctx.session.check_client_capability(
            ClientCapabilities(elicitation=ElicitationCapability())
        ):
            raise ToolError(
                "confirm_fix needs a client that supports elicitation, so the human can "
                "approve the fix themselves. This client does not; confirm in the "
                "FleetAlert web UI instead."
            )
        status = await _upstream(api.alert_status(alert_id))
        if status.status != "awaiting_confirmation":
            raise ToolError(
                f"{alert_id} is {status.status}, not awaiting_confirmation; nothing to confirm."
            )
        confidence = f"{status.confidence:.0%}" if status.confidence is not None else "n/a"
        message = (
            f"FleetAlert proposes `{status.proposed_fix}` for {alert_id} "
            f"(agent confidence {confidence}).\n\n"
            f"Root cause: {status.root_cause_summary or 'not given'}\n\n"
            "Approve executing this fix on the machine?"
        )
        return Elicit(message, HumanDecision)

    async def confirm_fix(
        alert_id: AlertId,
        decision: Annotated[ElicitationResult[HumanDecision], Resolve(ask_human)],
    ) -> ActionOutcome:
        """Confirm the fix proposed for an alert so FleetAlert executes it.

        The human is asked directly, in their MCP client, before anything is
        sent upstream. The confirmation token is fetched by this server only
        after they approve; it is never exposed to the model.
        """
        match decision:
            case AcceptedElicitation(data=HumanDecision(approve=True)):
                token = await _upstream(api.confirmation_token(alert_id))
                if not token:
                    raise ToolError(f"{alert_id} has no confirmation token; it may have moved on.")
                return await _upstream(api.confirm_fix(alert_id, token))
            case AcceptedElicitation(data=HumanDecision(note=note)):
                return ActionOutcome(
                    accepted=False,
                    http_status=0,
                    alert_id=alert_id,
                    outcome="not_confirmed",
                    reason=f"human did not approve{': ' + note if note else ''}",
                )
            case DeclinedElicitation():
                return ActionOutcome(
                    accepted=False,
                    http_status=0,
                    alert_id=alert_id,
                    outcome="not_confirmed",
                    reason="human declined",
                )
            case CancelledElicitation():
                return ActionOutcome(
                    accepted=False,
                    http_status=0,
                    alert_id=alert_id,
                    outcome="not_confirmed",
                    reason="human cancelled",
                )
        raise ToolError("unexpected elicitation outcome")  # pragma: no cover

    async def reject_fix(
        alert_id: AlertId,
        reason: Annotated[
            str, Field(min_length=3, max_length=500, description="Why the fix is wrong.")
        ],
    ) -> ActionOutcome:
        """Reject the fix proposed for an alert. FleetAlert re-investigates
        once with the rejection as evidence, then routes to support."""
        return await _upstream(api.reject_fix(alert_id, reason))

    register(confirm_fix, Tier.EXECUTES_ACTION, executes)
    register(reject_fix, Tier.EXECUTES_ACTION, executes)

    # -- resources and prompts ----------------------------------------------

    @mcp.resource("fleetalert://alerts", mime_type="application/json", title="All alerts")
    async def alerts_resource() -> str:
        return (await _upstream(api.list_alerts())).model_dump_json(indent=2)

    @mcp.resource(
        "fleetalert://alerts/{alert_id}/trace",
        mime_type="application/json",
        title="Alert trace summary",
    )
    async def trace_resource(alert_id: str) -> str:
        spans = await _upstream(api.trace(alert_id))
        return summarize_trace(alert_id, spans).model_dump_json(indent=2)

    @mcp.prompt(title="Triage an alert")
    def triage_alert(alert_id: str) -> str:
        """Review one alert's proposal and traces and advise the human."""
        return (
            f"Triage FleetAlert alert {alert_id}.\n\n"
            "1. Call get_alert and get_trace for it.\n"
            "2. Say what the agent read (telemetry, knowledge base, service history), "
            "what it proposed, how confident it was, and what the guardrails did.\n"
            "3. Judge whether the evidence in the trace supports the proposed fix. "
            "Flag conflicting knowledge-base entries or thin evidence.\n"
            "4. Recommend confirm, reject (with the reason you would give), or wait. "
            "Do not call confirm_fix or reject_fix unless the human asks you to."
        )

    return mcp


async def _upstream[T](call: Awaitable[T]) -> T:
    """Turn an upstream failure into a tool error the model can read."""
    try:
        return await call
    except UpstreamError as exc:
        raise ToolError(str(exc)) from exc
