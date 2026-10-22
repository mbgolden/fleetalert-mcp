# ADR-0001: A standalone repo on the MCP Python SDK v2

## Status
Accepted (2026-10-22)

## Context
FleetAlert AI has a public demo API (API Gateway, no auth, bounded by a
daily cost guard and fixed scenarios). The goal here is an MCP server that
exposes it to Claude Code, Claude Desktop and other MCP clients, and that
shows how an MCP server is built and shipped for real: typed contracts, an
auth boundary, rate limits, tests, a CI gate.

Three choices were open: where the code lives, which language, and which
SDK generation.

## Decision
- **A standalone repository, `fleetalert-mcp`,** not an `mcp/` folder in
  `fleetalert-ai`. MCP servers are installed as packages (`uvx --from
  git+https://... fleetalert-mcp`), versioned on their own, and consumed by
  people who never clone the backend. Coupling is through the public HTTP
  API only, which is the contract FleetAlert already publishes for its own
  frontend. The cost is that an upstream response-shape change has to be
  caught here, by the live smoke test, rather than by a shared type.
- **Python 3.12 with `uv`,** the same toolchain as FleetAlert and CoverageIQ
  (ruff, mypy strict, bandit, pytest). TypeScript would have reached the
  npm-centric part of the MCP ecosystem, but the portfolio is Python
  throughout and the typed-model story (pydantic in, pydantic out) is the
  point.
- **MCP Python SDK v2 (`mcp>=2.3`, protocol 2026-07-28).** v2 renamed
  `FastMCP` to `MCPServer`, added server middleware, structured output from
  return annotations, resolver-based elicitation that works across protocol
  eras, and an in-memory `Client(server)` for tests. Each of those is load
  bearing in this design (ADR-0002 to ADR-0004). The middleware list is
  marked provisional by the SDK, which is accepted: it is used to observe
  and refuse, as the SDK recommends, not as the server's foundation.

## Consequences
- Installable with one command, no clone, no FleetAlert checkout.
- Response models are duplicated from the frontend's TypeScript types by
  hand. The live test (`tests/test_live.py`, weekly in CI) is the drift
  alarm.
- Anything written against v1 SDK docs does not apply; the v2 docs site is
  the reference.
