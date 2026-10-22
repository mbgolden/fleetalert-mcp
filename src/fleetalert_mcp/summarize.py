"""Turn an alert's raw spans into something a model can reason over cheaply.

A single FleetAlert round writes 10 to 20 spans, each with full input and
output. An alert that has been re-investigated a few times carries hundreds.
`get_trace` returns this summary by default: one entry per round with its
outcome, cost and an ordered list of compact steps. `get_raw_spans` is there
for the detail.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from fleetalert_mcp.upstream import Span


class Step(BaseModel):
    at: str = Field(description="Span timestamp, ISO 8601.")
    offset_ms: float | None = Field(description="Milliseconds after the round's first step.")
    kind: str
    name: str
    actor: str
    status: str
    latency_ms: float | None = None
    detail: str = ""


class Round(BaseModel):
    trace_id: str
    entry_point: str | None
    started_at: str
    ended_at: str | None = Field(description="When the round's root span was written.")
    outcome: str | None = Field(description="awaiting_confirmation, routed_to_support, ...")
    fix_id: str | None = None
    previous_trace_id: str | None = Field(
        default=None, description="Set when this round re-investigated after a rejection."
    )
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    denied_calls: int = Field(default=0, description="Capability calls a guardrail refused.")
    steps: list[Step]


class TraceSummary(BaseModel):
    alert_id: str
    span_count: int
    rounds: list[Round]
    total_cost_usd: float


def summarize_trace(alert_id: str, spans: list[Span]) -> TraceSummary:
    by_trace: dict[str, list[Span]] = defaultdict(list)
    for span in spans:
        by_trace[span.trace_id].append(span)

    rounds: list[Round] = []
    for trace_spans in by_trace.values():
        trace_spans.sort(key=lambda s: s.timestamp)
        rounds.append(_round(trace_spans))
    rounds.sort(key=lambda r: r.started_at)

    return TraceSummary(
        alert_id=alert_id,
        span_count=len(spans),
        rounds=rounds,
        total_cost_usd=round(sum(r.cost_usd for r in rounds), 6),
    )


def _round(spans: list[Span]) -> Round:
    root = next((s for s in spans if s.kind == "investigation"), None)
    children = [s for s in spans if s is not root]
    first_at = children[0].timestamp if children else spans[0].timestamp
    first_dt = _parse(first_at)

    steps = [
        Step(
            at=s.timestamp,
            offset_ms=_offset_ms(first_dt, s.timestamp),
            kind=s.kind,
            name=s.name,
            actor=s.actor,
            status=s.status,
            latency_ms=s.latency_ms,
            detail=_detail(s),
        )
        for s in children
    ]

    attrs: dict[str, Any] = root.attributes if root else {}
    model_calls = [s for s in children if s.kind == "model_call"]
    return Round(
        trace_id=spans[0].trace_id,
        entry_point=root.entry_point if root else _first_entry_point(children),
        started_at=first_at,
        ended_at=root.timestamp if root else None,
        outcome=_opt_str(root.output.get("outcome")) if root else None,
        fix_id=_opt_str(root.output.get("fix_id")) if root else None,
        previous_trace_id=_opt_str(root.input.get("previous_trace_id")) if root else None,
        model_calls=_int(attrs.get("model_calls"), len(model_calls)),
        input_tokens=_int(attrs.get("input_tokens"), _sum_attr(model_calls, "input_tokens")),
        output_tokens=_int(attrs.get("output_tokens"), _sum_attr(model_calls, "output_tokens")),
        cost_usd=_float(
            attrs.get("estimated_cost_usd"), _sum_attr(model_calls, "estimated_cost_usd")
        ),
        denied_calls=sum(1 for s in children if s.status == "denied"),
        steps=steps,
    )


def _detail(span: Span) -> str:
    if span.kind == "model_call":
        stop = span.output.get("stop_reason")
        tools = span.output.get("tool_calls") or []
        cost = span.attributes.get("estimated_cost_usd")
        parts = [
            f"stop={stop}" if stop else "",
            f"tools={','.join(map(str, tools))}" if tools else "",
        ]
        if cost is not None:
            parts.append(f"${_float(cost, 0.0):.4f}")
        return " ".join(p for p in parts if p)
    if span.kind == "capability":
        if span.name == "propose_fix":
            fix = span.output.get("fix_id") or span.input.get("fix_id")
            confidence = span.input.get("confidence")
            text = f"{fix}" if fix else ""
            if confidence is not None:
                text += f" (confidence {confidence})"
            return text.strip()
        tier = span.attributes.get("safety_tier")
        return f"tier={tier}" if tier else ""
    if span.kind in ("decision", "human_action"):
        reason = span.output.get("reason") or span.input.get("reason") or span.output.get("detail")
        return str(reason) if reason else ""
    return ""


def _first_entry_point(spans: list[Span]) -> str | None:
    return next((s.entry_point for s in spans if s.entry_point), None)


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _offset_ms(start: datetime | None, value: str) -> float | None:
    end = _parse(value)
    if start is None or end is None:
        return None
    return round((end - start).total_seconds() * 1000, 1)


def _sum_attr(spans: list[Span], key: str) -> float:
    return sum(_float(s.attributes.get(key), 0.0) for s in spans)


def _int(value: Any, default: float) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)
