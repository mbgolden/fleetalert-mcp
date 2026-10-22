"""Server middleware around every `tools/call`: who, may they, how often, and
an audit line for each one.

Refusals come back as a tool result with `is_error=True`, not a protocol
error. A protocol error goes to the client application and the model never
learns why its call vanished; an error result is text the model reads
("rate limited, retry in 12 s"), so it can wait or tell the user.

Principal: in HTTP mode the SDK's bearer middleware has already verified the
token, and `get_access_token()` gives its client id and tier scope. On stdio
there is no token; the principal is `local` at the server's own tier.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from mcp.server import ServerRequestContext
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.context import CallNext, HandlerResult, ServerMiddleware
from mcp.types import CallToolResult, TextContent

from fleetalert_mcp.config import Settings
from fleetalert_mcp.ratelimit import TokenBucket
from fleetalert_mcp.tiers import Tier

audit_log = logging.getLogger("fleetalert_mcp.audit")

LOCAL_PRINCIPAL = "local"


@dataclass(frozen=True)
class Principal:
    id: str
    tier: Tier


def current_principal(server_tier: Tier) -> Principal:
    token = get_access_token()
    if token is None:
        return Principal(LOCAL_PRINCIPAL, server_tier)
    tier = _tier_from_scopes(token.scopes)
    # A token can narrow the server's tier, never widen it.
    effective = tier if server_tier.allows(tier) else server_tier
    return Principal(token.client_id, effective)


def _tier_from_scopes(scopes: list[str]) -> Tier:
    for scope in scopes:
        try:
            return Tier.parse(scope)
        except ValueError:
            continue
    return Tier.READ_ONLY


@dataclass
class Metrics:
    """Counters the `/metrics` route reports. Process-local, like the buckets."""

    calls: Counter[str] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)
    refused_tier: Counter[str] = field(default_factory=Counter)
    rate_limited: Counter[str] = field(default_factory=Counter)
    duration_ms_total: dict[str, float] = field(default_factory=lambda: defaultdict(float))

    def snapshot(self) -> dict[str, Any]:
        tools = sorted(set(self.calls) | set(self.errors) | set(self.rate_limited))
        return {
            "tools": {
                name: {
                    "calls": self.calls[name],
                    "errors": self.errors[name],
                    "refused_tier": self.refused_tier[name],
                    "rate_limited": self.rate_limited[name],
                    "avg_ms": round(self.duration_ms_total[name] / self.calls[name], 1)
                    if self.calls[name]
                    else None,
                }
                for name in tools
            },
            "totals": {
                "calls": sum(self.calls.values()),
                "errors": sum(self.errors.values()),
                "refused_tier": sum(self.refused_tier.values()),
                "rate_limited": sum(self.rate_limited.values()),
            },
        }


def _is_error(result: HandlerResult) -> bool:
    """The handler's result reaches middleware either as the model or as its dict."""
    if isinstance(result, CallToolResult):
        return bool(result.is_error)
    if isinstance(result, dict):
        return bool(result.get("isError") or result.get("is_error"))
    return False


def error_result(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


def make_guard(
    settings: Settings,
    catalog: Mapping[str, Tier],
    metrics: Metrics,
    *,
    clock: Any = time.monotonic,
) -> ServerMiddleware[Any]:
    """`catalog` maps registered tool name -> tier; it is read at call time, so
    it may be filled in after the middleware is created."""
    per_principal = TokenBucket(
        settings.calls_per_minute, settings.calls_per_minute / 60, clock=clock
    )
    per_principal_actions = TokenBucket(
        settings.actions_per_hour, settings.actions_per_hour / 3600, clock=clock
    )
    upstream = TokenBucket(
        settings.upstream_calls_per_minute, settings.upstream_calls_per_minute / 60, clock=clock
    )

    async def guard(ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        if ctx.method != "tools/call":
            return await call_next(ctx)

        params = ctx.params or {}
        tool = str(params.get("name", "?"))
        principal = current_principal(settings.tier)
        required = catalog.get(tool)
        started = time.perf_counter()

        def log(outcome: str, **extra: Any) -> None:
            record = {
                "event": "tool_call",
                "principal": principal.id,
                "principal_tier": principal.tier.value,
                "tool": tool,
                "tool_tier": required.value if required else None,
                "outcome": outcome,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                **extra,
            }
            audit_log.info(json.dumps(record, default=str))

        if required is not None and not principal.tier.allows(required):
            metrics.refused_tier[tool] += 1
            log("refused_tier")
            return error_result(
                f"{tool} needs the {required.value} tier; this client is limited to "
                f"{principal.tier.value}. Ask the operator to raise the tier, or use "
                "the FleetAlert web UI."
            )

        wait = per_principal.try_acquire(principal.id)
        if wait == 0.0 and required is not None and required is not Tier.READ_ONLY:
            wait = per_principal_actions.try_acquire(principal.id)
            scope = "action"
        else:
            scope = "call"
        if wait == 0.0:
            wait = upstream.try_acquire("upstream")
            scope = "upstream" if wait else scope
        if wait > 0.0:
            metrics.rate_limited[tool] += 1
            retry_after = max(1, math.ceil(wait))
            log("rate_limited", scope=scope, retry_after_s=retry_after)
            return error_result(
                f"Rate limit reached ({scope}). Retry in {retry_after} s. "
                "FleetAlert is a public demo with a daily cost cap; pace calls accordingly."
            )

        try:
            result = await call_next(ctx)
        except Exception as exc:
            metrics.calls[tool] += 1
            metrics.errors[tool] += 1
            metrics.duration_ms_total[tool] += (time.perf_counter() - started) * 1000
            log("exception", error=type(exc).__name__)
            raise

        metrics.calls[tool] += 1
        metrics.duration_ms_total[tool] += (time.perf_counter() - started) * 1000
        is_error = _is_error(result)
        if is_error:
            metrics.errors[tool] += 1
        log("error" if is_error else "ok")
        return result

    return guard
