# fleetalert-mcp

An MCP server for [FleetAlert AI](https://fleetalert.10finger.dev), the
agentic alert-investigation demo in
[mbgolden/fleetalert-ai](https://github.com/mbgolden/fleetalert-ai). It lets
Claude Code, Claude Desktop or any MCP client read FleetAlert's alerts and
investigation traces, start investigations, and confirm or reject a proposed
fix, with the same safety tiers FleetAlert enforces internally, plus the
bearer-token auth and rate limits the public demo API does not have itself.

Built on the [MCP Python SDK v2](https://py.sdk.modelcontextprotocol.io/)
(protocol 2026-07-28), Python 3.12.

## What it demonstrates

| | |
|---|---|
| **Typed tools, structured output** | Every tool has a JSON Schema input (from type hints) and a JSON Schema output (from pydantic models). `get_trace` turns dozens of raw spans into one summary per investigation round: what the agent read, what it proposed, what the guardrails decided, what it cost. |
| **Safety tiers carried through the protocol** | Tools are tagged `read_only`, `proposes_action` or `executes_action`, FleetAlert's own tier names. The server runs at one tier (default read-only) and tools above it are not registered at all, so a client cannot discover them. A bearer token carries a tier too, and can only narrow what the server allows. |
| **Human approval, structurally** | `confirm_fix` is the only tool that makes something execute upstream. It asks the human through MCP elicitation, in their own client, before anything is sent. The model passes an alert id and nothing else; the confirmation token is fetched by the server only after approval and never appears in a tool result. A client that cannot ask is refused. |
| **The server is the auth boundary** | Over Streamable HTTP, requests need a bearer token (`401` with `WWW-Authenticate` and RFC 9728 protected-resource metadata otherwise). Tokens are static, compared by digest in constant time, through the SDK's `TokenVerifier` protocol, so a real authorization server is a one-class swap. stdio has no tokens: the process boundary is the auth boundary, as the spec intends. |
| **Rate limits that protect the upstream budget** | FleetAlert caps itself at 50 investigations or $1.25 a day. This server adds per-principal buckets (all tools, and a tighter one for tools that spend budget) and a global bucket in front of the upstream API. A refusal is a tool result the model can read ("retry in 12 s"), not a protocol error it never sees. |
| **Audit and metrics** | One JSON line per tool call: principal, tier, tool, outcome, duration. `/metrics` reports calls, errors, tier refusals and rate limits per tool; `/healthz` reports version, tier and configured clients. |
| **Tested without the network** | The SDK's in-memory client drives the real server against a mocked FleetAlert API, including the elicitation round-trip for `confirm_fix`. A read-only smoke test runs against the live demo on demand and weekly. |

## Tools

| Tool | Tier | What it does |
|---|---|---|
| `list_alerts` | read_only | Every demo alert with machine, severity and status |
| `get_alert` | read_only | One alert's status, proposed fix, confidence and root-cause summary |
| `get_trace` | read_only | Investigation history, one summary per round |
| `get_raw_spans` | read_only | Raw spans with full input and output, filterable by kind |
| `get_usage` | read_only | Today's spend against the daily cost guard |
| `get_activity` | read_only | Recent cross-alert activity feed |
| `start_investigation` | proposes_action | Start an agent round on an open alert (spends budget) |
| `simulate_inbound_email` | proposes_action | Deliver the demo's seeded maintenance email |
| `run_telemetry_detector` | proposes_action | Run the rule-based detector once |
| `confirm_fix` | executes_action | Ask the human, then confirm the proposed fix upstream |
| `reject_fix` | executes_action | Reject the proposed fix with a reason |

Also: resources `fleetalert://alerts` and `fleetalert://alerts/{alert_id}/trace`,
and a `triage_alert` prompt that walks a model through reviewing one alert's
evidence and recommending confirm, reject or wait.

## Quick start

The hosted demo instance (read-only visitor token, nothing to install):

```bash
claude mcp add --transport http fleetalert https://xx8eypbgv2.execute-api.us-east-1.amazonaws.com/mcp \
  --header "Authorization: Bearer d2067598688a6ca159dec88d3b0911805d09a2940513e869"
```

Or run it locally over stdio, straight from GitHub:

```bash
claude mcp add fleetalert -- uvx --from git+https://github.com/mbgolden/fleetalert-mcp fleetalert-mcp
```

To allow actions, set the tier (the tools appear only when you do):

```bash
claude mcp add fleetalert -e FLEETALERT_MCP_TIER=executes_action -- uvx --from git+https://github.com/mbgolden/fleetalert-mcp fleetalert-mcp
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "fleetalert": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/mbgolden/fleetalert-mcp", "fleetalert-mcp"],
      "env": { "FLEETALERT_MCP_TIER": "read_only" }
    }
  }
}
```

Then ask: *"List the FleetAlert alerts and summarise the trace for the one
awaiting confirmation."*

## Configuration

All settings are `FLEETALERT_MCP_*` environment variables (a `.env` file in
the working directory is honoured; see `.env.example`).

| Variable | Default | Meaning |
|---|---|---|
| `TIER` | `read_only` | Highest tier of tool to register: `read_only`, `proposes_action`, `executes_action` |
| `API_BASE_URL` | the live demo | FleetAlert API base URL |
| `TOKENS` | empty | HTTP mode bearer tokens: `client_id:tier:secret[,...]`, secrets 16+ chars |
| `PUBLIC_URL` | `http://host:port` | Public URL of this server in HTTP mode (for the protected-resource metadata) |
| `ALLOW_ANONYMOUS_HTTP` | `false` | Run HTTP mode without tokens, loopback only |
| `STATE_KEY` | unset | Fixed key sealing elicitation state across processes (required on Lambda) |
| `CALLS_PER_MINUTE` | `30` | Per principal, every tool |
| `ACTIONS_PER_HOUR` | `6` | Per principal, tools above read_only |
| `UPSTREAM_CALLS_PER_MINUTE` | `120` | Across all principals |
| `UPSTREAM_TIMEOUT_SECONDS` | `10` | |

## HTTP mode

```bash
export FLEETALERT_MCP_TIER=executes_action
export FLEETALERT_MCP_TOKENS="ops:executes_action:$(openssl rand -hex 24),ci:read_only:$(openssl rand -hex 24)"
fleetalert-mcp --transport http --host 0.0.0.0 --port 8000
```

- `POST /mcp` is the MCP endpoint. Without a valid `Authorization: Bearer`
  header it answers `401` with a `WWW-Authenticate` header pointing at
  `/.well-known/oauth-protected-resource/mcp`.
- The `ci` token above sees every tool the server registers but is refused on
  anything above `read_only`, with a message the model can read.
- `GET /healthz` and `GET /metrics` are unauthenticated and JSON.

The server refuses to start in HTTP mode without tokens unless
`FLEETALERT_MCP_ALLOW_ANONYMOUS_HTTP=1` and the host is loopback.

## Hosted on AWS Lambda

The same server runs as a Lambda behind API Gateway, which is how the
FleetAlert demo exposes it. Install the extra and point the function at
`fleetalert_mcp.lambda_handler.handler`:

```bash
uv pip install "fleetalert-mcp[lambda] @ git+https://github.com/mbgolden/fleetalert-mcp@v0.2.0"
```

| Variable | |
|---|---|
| `FLEETALERT_MCP_PUBLIC_URL` | the API Gateway endpoint (issuer and resource for the bearer auth) |
| `FLEETALERT_MCP_SECRET_ARN` | a Secrets Manager secret holding `{"tokens": "...", "state_key": "..."}` |
| `FLEETALERT_MCP_TIER` | the server's tier; tokens narrow it per client |

The Lambda serves protocol 2026-07-28 in stateless JSON mode (one POST per
request, no sessions, no SSE). `state_key` (32+ characters, for example
`secrets.token_hex(32)`) seals the elicitation state that `confirm_fix`
carries between two requests, so it works across instances. The Terraform
that deploys it lives in
[fleetalert-ai](https://github.com/mbgolden/fleetalert-ai) (its ADR-0025);
see [ADR-0005](docs/decisions/ADR-0005-lambda-hosting-stateless-json.md)
here for the reasoning.

The FleetAlert demo's instance is live. Connect with its published read-only
visitor token:

```bash
claude mcp add --transport http fleetalert https://xx8eypbgv2.execute-api.us-east-1.amazonaws.com/mcp \
  --header "Authorization: Bearer d2067598688a6ca159dec88d3b0911805d09a2940513e869"
```

Health: [https://xx8eypbgv2.execute-api.us-east-1.amazonaws.com/healthz](https://xx8eypbgv2.execute-api.us-east-1.amazonaws.com/healthz).

## How confirm_fix works

```
model: confirm_fix(alert_id="ALERT-1007")
  server: GET /status -> awaiting_confirmation? (else: tool error, nothing sent)
  server: elicit -> client shows the human the proposed fix, confidence, root cause
    human: approve=true          human: approve=false / decline / cancel
  server: GET /status -> token     server: result "not_confirmed", nothing sent
  server: POST /confirm {token}
  model: result "confirmed" (the token is not in it)
```

If the client did not advertise elicitation, the tool is refused before any
upstream call, with a message pointing the human at the FleetAlert web UI.

## Development

```bash
uv sync --group dev
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run bandit -q -r src
uv run pytest
FLEETALERT_MCP_LIVE=1 uv run pytest tests/test_live.py   # read-only, against the real demo
```

Run locally over stdio with `uv run fleetalert-mcp`, or over HTTP as above.
`uv run mcp dev src/fleetalert_mcp/cli.py` is not supported (the server is
built by `build_server()` from settings); use the MCP Inspector against the
HTTP endpoint instead.

## Repo layout

```
src/fleetalert_mcp/
  server.py      tools, resources, prompt; build_server(settings)
  tiers.py       the three tiers and their ordering
  guard.py       middleware: principal, tier check, rate limits, audit log, metrics
  auth.py        static bearer tokens as a TokenVerifier
  ratelimit.py   token buckets
  upstream.py    typed httpx client for the FleetAlert API
  summarize.py   spans -> per-round summary
  config.py      FLEETALERT_MCP_* settings
  cli.py         stdio / HTTP entry point
  http.py        bearer auth settings and /healthz, /metrics, shared by uvicorn and Lambda
  lambda_handler.py  AWS Lambda entry point (Mangum, stateless JSON mode)
tests/           in-memory client against a mocked API; one live smoke test
docs/decisions/  ADR log
docs/BACKLOG.md  follow-ups, with the evidence that raised them
```

## Decisions

- [ADR-0001](docs/decisions/ADR-0001-standalone-python-mcp-server.md): a standalone repo on the Python SDK v2, not a folder in fleetalert-ai
- [ADR-0002](docs/decisions/ADR-0002-safety-tiers-gate-registration.md): tiers decide which tools exist, default read-only, tokens narrow only
- [ADR-0003](docs/decisions/ADR-0003-human-approval-through-elicitation.md): confirm_fix asks the human through elicitation; the token never reaches the model
- [ADR-0004](docs/decisions/ADR-0004-auth-and-rate-limit-boundary.md): this server is the auth and rate-limit boundary; static tokens now, OAuth later; refusals as tool results
- [ADR-0005](docs/decisions/ADR-0005-lambda-hosting-stateless-json.md): hosted as a Lambda behind API Gateway in stateless JSON mode, with a shared state key and tokens from Secrets Manager

## License

MIT.
