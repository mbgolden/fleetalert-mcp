# ADR-0004: This server is the auth and rate-limit boundary; refusals are tool results

## Status
Accepted (2026-10-22)

## Context
The upstream API has no authentication. FleetAlert's protections are a daily
cost guard (50 rounds or $1.25), fixed scenarios and no free text. An MCP
server deployed over HTTP would otherwise be an anonymous, unmetered proxy
onto that budget. It also runs locally over stdio, where none of this
applies.

## Decision
**Auth.** Over Streamable HTTP, every `/mcp` request needs a bearer token.
Tokens are configured as `client_id:tier:secret`, hashed with SHA-256 at
startup and compared in constant time (every entry is compared, so timing
does not reveal which one matched). The verifier implements the SDK's
`TokenVerifier` protocol and the server is configured with `AuthSettings`,
so the SDK supplies the `401`, the `WWW-Authenticate` header and the
RFC 9728 protected-resource metadata. The server refuses to start in HTTP
mode without tokens, unless anonymous access is explicitly allowed on a
loopback host. stdio has no auth: the spec puts that boundary at the
process, and the local principal gets the server's tier.

Static tokens are a deliberate first step, not the destination. A real
authorization server (JWT validation or RFC 7662 introspection) is a
one-class swap behind the same protocol and is in the backlog. The metadata
currently names this server as its own authorization server, which is true
for static tokens and would be replaced along with the verifier.

**Rate limits.** Token buckets in process: per principal for every tool
(30/min), per principal for tools above `read_only` (6/hour, since each
spends upstream budget), and one global bucket in front of the upstream API
(120/min). Per-principal first, then action, then global, so a heavy reader
cannot starve others' actions.

**Where the checks live.** A single server middleware wraps `tools/call`:
principal from `get_access_token()`, tier check (ADR-0002), buckets, then
the handler, timed. The same middleware writes one JSON audit line per call
and updates the counters `/metrics` reports. Using the SDK's middleware
rather than per-tool decorators means a new tool is covered by writing it.

**Refusals are tool results with `is_error=true`, not protocol errors.** A
protocol error goes to the client application; the model never sees it and
cannot explain the silence. An error result is text the model reads: which
tier it lacks, or how many seconds to wait. The SDK's own docs make the same
distinction.

## Consequences
- Verified end to end on 2026-10-22: `401` with `WWW-Authenticate` and
  resource metadata without a token; a `read_only` token refused on
  `reject_fix` with a readable message; a bad token rejected; `/metrics`
  and the audit log showing each call with principal and duration.
- Buckets and counters are process-local. Two replicas means two budgets;
  a shared store is a backlog item, with the evidence that would justify it.
- The SDK middleware API is provisional. The guard is small (one function)
  and only observes or returns early, so a signature change is a contained
  edit.
- `uvicorn`'s own access log is plain text next to the JSON lines. Cosmetic;
  noted in the backlog.
