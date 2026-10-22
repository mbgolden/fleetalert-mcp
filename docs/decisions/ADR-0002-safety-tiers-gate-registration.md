# ADR-0002: Safety tiers decide which tools exist, and tokens can only narrow

## Status
Accepted (2026-10-22)

## Context
FleetAlert's Capabilities Engine (its ADR-0011) tags every capability
`read_only`, `proposes_action` or `executes_action`, and the model only ever
receives the first two tiers. The MCP server sits outside that engine: it
is a client of the HTTP API, and the HTTP API exposes everything, including
`POST /confirm`. Unless something here restricts it, any model connected
through MCP could list alerts, start investigations and confirm fixes in one
breath, undoing the demo's central guarantee.

Two obvious designs: register every tool and refuse at call time based on
configuration, or register only what the configuration allows.

## Decision
- **The same three tier names, same order.** Reads are `read_only`.
  Starting agent work (`start_investigation`, the two simulated entry
  points) is `proposes_action`: it spends budget but ends awaiting a human.
  `confirm_fix` and `reject_fix` are `executes_action`.
- **The server runs at one tier, default `read_only`, and tools above it
  are not registered.** They are absent from `tools/list`, so a client
  cannot discover them, and a model cannot be talked into calling them. This
  is "unreachable", FleetAlert's own standard, rather than "refused".
- **Bearer tokens carry a tier as their only scope.** The effective tier of
  a call is the token's tier, capped by the server's. A `read_only` token
  on an `executes_action` server sees every tool (the listing is per server,
  not per token) but is refused at call time on anything above
  `read_only`, with a message that names both tiers. On stdio there is no
  token; the principal is `local` at the server's tier.
- **Tool annotations match the tier.** Reads are `read_only_hint`;
  `confirm_fix` is `destructive_hint`. Hints, not enforcement, as the spec
  says; enforcement is the registration rule above.

## Consequences
- An operator chooses the blast radius with one variable. A read-only
  install is the default and needs no further thought.
- Per-token refusal is a call-time check in middleware (ADR-0004), so a
  narrowed token still sees tools it cannot use. Per-token listings would
  need per-session tool sets, which the SDK does not offer directly; not
  worth it for this surface.
- Tests pin the registration rule: a `read_only` server lists exactly six
  tools, and a `read_only` token on an `executes_action` server is refused
  on `reject_fix` and counted in metrics.
