# ADR-0005: Hosted as a Lambda behind API Gateway, in stateless JSON mode

## Status
Accepted (2026-10-24). Closes the "deploy the HTTP mode somewhere real"
backlog item.

## Context
Until now the server ran only on the author's machine over stdio, or over
HTTP on localhost. Nothing a visitor to the FleetAlert demo could connect
to. Hosting options were a home box behind a tunnel (down when the box is),
an always-on container (a second platform to run and pay for), or a Lambda
in the FleetAlert AWS account, which is where the API it fronts already
lives, costs nothing idle, and is deployed by the same Terraform and gated
workflow as the rest of FleetAlert.

Two properties of the MCP protocol decide whether a Lambda can serve it at
all. Protocol 2026-07-28 over Streamable HTTP is a single POST per request:
no `initialize` handshake, no session id, one JSON-RPC request in and one
response out. And the SDK's `Resolve`/`Elicit` flow for `confirm_fix` is
designed for that: the server returns the question, the client's next
request carries the answer plus an opaque, encrypted `request_state`. The
only thing that state needs is a key every instance shares.

## Decision
- **A Lambda entry point** (`fleetalert_mcp.lambda_handler.handler`) wraps
  the Starlette app with Mangum for API Gateway HTTP API (payload 2.0)
  events. It is an optional extra (`fleetalert-mcp[lambda]`), so the stdio
  install stays small.
- **Stateless JSON mode.** `streamable_http_app(stateless_http=True,
  json_response=True)`: no sessions, no SSE, nothing a Lambda cannot do.
  Legacy-protocol clients that need a session still work against the
  uvicorn deployment; the hosted endpoint targets current clients.
- **A fixed request-state key.** The SDK seals elicitation state with a
  per-process key by default; on Lambda the second request can land on a
  different instance. `RequestStateSecurity(keys=[state_key])` is wired
  into `build_server`, and the CLI accepts the same setting for multi-worker
  uvicorn.
- **Tokens and the key come from Secrets Manager**, read once per container
  from `FLEETALERT_MCP_SECRET_ARN`, following FleetAlert's rule that secret
  values are set by hand in the console and never pass through Terraform or
  a repository. A deployment with no secret value refuses every MCP call
  and says so on `/healthz`, rather than failing to start.
- **The server object is rebuilt per invocation.** The SDK's session
  manager runs once per app and Mangum drives the app lifespan per event, so
  a fresh app per call is the simple correct shape. The expensive pieces
  (settings, token verifier, upstream client, metrics) live for the life of
  the container.
- **The Terraform lives in fleetalert-ai** (its ADR-0025): a `mcp_server`
  module with its own HTTP API, the secret placeholder, and a build script
  that installs this package from a pinned git tag. This repo ships code and
  a tag; the demo environment decides when to deploy it.

## Consequences
- Verified end to end before any AWS deploy: a real MCP client over a
  socket, through a local API Gateway shim, into the real handler, with a
  mocked FleetAlert API. Reads work with a read-only token, a refusal still
  reads as a tool error, and `confirm_fix` completes across two separate
  invocations with the shared key.
- Rate-limit buckets and `/metrics` counters are per container. For a
  public read-only token that is fine: each container still caps a chatty
  client, and the upstream daily cost guard is the backstop. A shared store
  stays in the backlog.
- The protected-resource metadata names the API Gateway endpoint as both
  resource and issuer, as ADR-0004 chose for static tokens. A custom domain
  would change only the public URL setting.
- `confirm_fix` on the hosted endpoint needs a client that supports
  elicitation and a token at the `executes_action` tier. The published
  visitor token is read-only, so visitors can look at everything and change
  nothing.
