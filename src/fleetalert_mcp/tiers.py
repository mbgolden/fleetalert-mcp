"""Safety tiers, mirroring FleetAlert's Capabilities Engine (its ADR-0011).

FleetAlert registers every capability with a tier, and the model only ever
gets `read_only` and `proposes_action`; `executes_action` is unreachable from
anything it says. This server keeps the same three names and the same
ordering so a tool's tier here means what it means upstream:

- `read_only`: reads alerts, traces, usage. Costs nothing upstream.
- `proposes_action`: starts agent work (an investigation round). Spends the
  demo's daily model budget, but nothing executes without a human.
- `executes_action`: confirms or rejects a proposed fix. Confirming makes
  the upstream state machine run the fix.

A server runs at one tier (`FLEETALERT_MCP_TIER`); tools above it are not
registered at all. A bearer token carries its own tier as a scope, which
can only narrow what the server allows, never widen it.
"""

from __future__ import annotations

from enum import StrEnum


class Tier(StrEnum):
    READ_ONLY = "read_only"
    PROPOSES_ACTION = "proposes_action"
    EXECUTES_ACTION = "executes_action"

    @property
    def rank(self) -> int:
        return _ORDER.index(self)

    def allows(self, required: Tier) -> bool:
        """True when a principal at this tier may call a tool at `required`."""
        return self.rank >= required.rank

    @classmethod
    def parse(cls, value: str) -> Tier:
        try:
            return cls(value.strip().lower())
        except ValueError:
            names = ", ".join(t.value for t in _ORDER)
            raise ValueError(f"unknown tier {value!r}; expected one of {names}") from None


_ORDER: tuple[Tier, ...] = (Tier.READ_ONLY, Tier.PROPOSES_ACTION, Tier.EXECUTES_ACTION)
