# ADR-0006: start_investigation declares itself as the `mcp` entry point

## Status
Accepted (2026-10-25). Pairs with fleetalert-ai ADR-0026.

## Context
FleetAlert tags every investigation round with how it arrived (web, email,
autonomous) and shows that in its trace viewer and Activity feed. Rounds
started through this server used the same route as the web UI and were
indistinguishable from a visitor's click, which hid the one thing the
hosted server is there to demonstrate.

## Decision
`start_investigation` sends `x-fleetalert-entry-point: mcp` on the upstream
request. FleetAlert honours only allowlisted values and tags the round
`mcp`. The two simulated entry points (`simulate_inbound_email`,
`run_telemetry_detector`) do not send it: the rounds they start belong to
the email and the detector.

## Consequences
- An MCP-started round shows "via mcp" in FleetAlert's trace viewer and
  Activity feed, so a visitor who connects their Claude can see their own
  investigation arrive as a fourth way in.
- The header is a label, not a privilege; budget and guardrails are the
  same on every path, so a spoofed header changes nothing that matters.
- A test pins the header on the mocked upstream call.
