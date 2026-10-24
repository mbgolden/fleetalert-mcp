# Backlog

Known follow-ups, each with the evidence that raised it. When an item is
done, remove it and point to the commit or ADR in the PR.

## Real authorization server
Raised by ADR-0004. Static `client_id:tier:secret` tokens are enough for a
demo and for CI, and the protected-resource metadata names this server as
its own issuer. Replace `StaticTokenVerifier` with JWT validation (or
RFC 7662 introspection) against a real issuer, point `AuthSettings.issuer_url`
at it, and map a claim to the tier scope. Bring it back when: a second
operator needs their own credentials, or an MCP client with OAuth support is
the primary consumer.

## Shared rate-limit state
Raised by ADR-0004, sharpened by ADR-0005: on Lambda each container has
its own buckets and counters, so a client's limit is per instance. The
upstream daily cost guard is the backstop. Bring it back when: the public
endpoint sees real traffic and per-instance limits prove too loose (then
DynamoDB buckets keyed by principal, next to the usage table).

## OpenTelemetry export
Raised by the audit-log design in guard.py. The SDK already emits OTel spans
per request through a built-in middleware that does nothing until an
exporter is installed. Wire an OTLP exporter and ship the `tool_call` audit
fields as span attributes, so traces line up with FleetAlert's own CloudWatch
spans by `alert_id`. Bring it back when: there is a collector to send to.

## uvicorn access log is not JSON
Raised during the 2026-10-22 HTTP verification: `INFO: 127.0.0.1 - "POST
/mcp" 200 OK` lines sit next to the JSON audit lines. Pass a `log_config`
that routes `uvicorn.access` through `JsonFormatter`, or disable it since
the audit line already records each tool call.

## Per-token tool listings
Raised by ADR-0002. A narrowed token sees every tool the server registers
and is refused at call time. The SDK lists tools per server, not per
session. Bring it back when: a client's model keeps attempting refused tools
and the refusal text is not enough to stop it.
