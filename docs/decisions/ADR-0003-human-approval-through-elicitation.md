# ADR-0003: confirm_fix asks the human through elicitation; the token never reaches the model

## Status
Accepted (2026-10-22)

## Context
In FleetAlert, a proposed fix executes only when a human presses Confirm,
which sends the alert's `confirmation_token` to `POST /confirm`. The status
endpoint returns that token, because the web UI needs it. An MCP tool that
simply wrapped the endpoint would hand the token to the model via
`get_alert` and let the model call `confirm_fix` with it. The human would be
out of the loop, and the demo's point would be lost at the protocol edge.

## Decision
- **`AlertStatus` has no `confirmation_token` field.** The upstream client
  strips it from every model-visible result. A separate
  `confirmation_token()` call fetches it, and only `confirm_fix` uses that.
- **`confirm_fix` takes an alert id and nothing else from the model.** Its
  second parameter is resolved server-side with the SDK's
  `Annotated[ElicitationResult[HumanDecision], Resolve(ask_human)]`. The
  resolver checks the alert is `awaiting_confirmation`, then returns an
  `Elicit` carrying the proposed fix, the agent's confidence and its
  root-cause summary. The client renders that to the human; the answer
  (`approve: bool`, optional note) comes back validated.
- **Only after `approve=true` does the server fetch the token and call
  upstream.** Decline, cancel and `approve=false` return a `not_confirmed`
  result with nothing sent. The result never includes the token.
- **A client without the elicitation capability is refused** before any
  upstream call, with a message pointing the human at the web UI. The
  SDK would fail the call anyway; the explicit check makes the reason
  readable.
- `reject_fix` needs no approval: a rejection cannot execute anything, and
  it carries the model's reason for the audit trail.

## Consequences
- The guarantee is architectural, as upstream: there is no argument a
  model can supply to skip the human.
- The elicitation schema is flat (bool plus string), as the spec requires.
- `tests/test_tiers_and_actions.py` drives the full round-trip in memory:
  approve sends exactly `{"confirmation_token": ...}` upstream and the
  token does not appear in the result; decline sends nothing; a client with
  no elicitation callback is refused.
- **A real failure, fixed:** the first version of `server.py` had `from
  __future__ import annotations`. The SDK evaluates tool signatures with
  `inspect.signature(eval_str=True)`, and the string annotation
  `Resolve(ask_human)` could not be resolved because `ask_human` is a
  closure inside `build_server()`. Removing the future import from that
  one module fixed it; a comment at the top of the file says why so nobody
  adds it back.
