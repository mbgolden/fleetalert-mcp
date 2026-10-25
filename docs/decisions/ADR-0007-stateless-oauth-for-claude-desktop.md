# ADR-0007: A stateless OAuth 2.1 authorization server, so Claude Desktop can connect

## Status
Accepted (2026-10-25)

## Context
Claude Code connects with a bearer header, and the static governor keys of
ADR-0004 serve it well. Claude Desktop's custom connectors and claude.ai do
not send headers: they follow the MCP authorization spec, which means
discovering an authorization server from the protected-resource metadata,
registering dynamically (RFC 7591), sending the person through `/authorize`
with PKCE, and exchanging the code at `/token`. Without that, the hosted
server (ADR-0005) was invisible to the most common Claude client.

The SDK ships the authorization-server routes and handlers; what it needs
from an application is an `OAuthAuthorizationServerProvider` that stores
clients, codes and tokens, and a place for the person to consent. The
server runs on Lambda, where there is no memory between requests and
deliberately no database.

## Decision
- **The server is its own authorization server**, through the SDK's
  provider protocol, with dynamic client registration on, PKCE S256 only,
  refresh tokens, and a revocation endpoint. Issuer and resource are both
  the API Gateway endpoint, as ADR-0004 already chose.
- **No state. Every record is sealed into the token that refers to it**,
  AES-256-GCM under a key derived from the shared `state_key` of ADR-0005,
  with a separate derived key and prefix per kind: the client's metadata
  is the `client_id`; the authorization request is a `txn` the consent
  page carries; the person's decision is the authorization code; the grant
  is the access and refresh token. Each carries its own expiry.
- **A consent page decides who the connector is.** With nothing entered,
  the grant is the read-only `visitor`. A pasted governor key grants that
  key's principal and tier, checked by digest in constant time. Scopes are
  tier names, the same vocabulary the guard already reads, so an OAuth
  token narrows the server exactly as a static one does. The principal in
  the audit log is the token's subject (`visitor`, `ops`), not the
  registered app.
- **Static keys keep working.** The SDK verifies every bearer token through
  the provider when one is configured, so `load_access_token` tries the
  static keys first and only then unseals. Claude Code users notice nothing.
- **OAuth is a switch** (`FLEETALERT_MCP_OAUTH_ENABLED`) that requires the
  state key; the Lambda in fleetalert-ai turns it on.

## Consequences
- Verified end to end with the SDK's own OAuth client through the Lambda
  shim: registration, the redirect to the consent page, the code, the token
  exchange, then tool calls as `visitor` with `reject_fix` refused. The
  metadata documents are served from the handler.
- Codes and refresh tokens cannot be made single-use or revoked without a
  store. Codes live five minutes and are bound to the registering client
  and its PKCE verifier; refresh tokens thirty days; access tokens one
  hour. A DynamoDB nonce table is the backlog item if the demo ever needs
  real revocation.
- A registered `client_id` is a few hundred characters. Every client seen so
  far handles that; it is the price of having no client table.
- Claude Desktop itself has not been exercised from this repo's tests; the
  spec flow it implements is the one the SDK client exercised, and the
  first real connection is the acceptance test.
