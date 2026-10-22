from __future__ import annotations

import pytest

from fleetalert_mcp.auth import StaticTokenVerifier, parse_tokens
from fleetalert_mcp.cli import parse_args
from fleetalert_mcp.tiers import Tier

pytestmark = pytest.mark.anyio

SECRET = "0123456789abcdefXYZ"


def test_parse_tokens_happy_path() -> None:
    tokens = parse_tokens(f"ops:executes_action:{SECRET}, ci:read_only:{SECRET}2 ,")
    assert [(t.client_id, t.tier) for t in tokens] == [
        ("ops", Tier.EXECUTES_ACTION),
        ("ci", Tier.READ_ONLY),
    ]
    assert parse_tokens("") == []


@pytest.mark.parametrize(
    "raw, message",
    [
        ("ops:read_only", "client_id:tier:secret"),
        (f"ops:god:{SECRET}", "unknown tier"),
        ("ops:read_only:short", "shorter than 16"),
        (f"ops:read_only:{SECRET},ops:read_only:{SECRET}2", "duplicate"),
    ],
)
def test_parse_tokens_rejects_bad_entries(raw: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_tokens(raw)


async def test_verifier_matches_by_digest_and_carries_tier_as_scope() -> None:
    verifier = StaticTokenVerifier(
        parse_tokens(f"ops:executes_action:{SECRET},ci:read_only:{SECRET}2"),
        resource="https://mcp.example.com/mcp",
    )
    ops = await verifier.verify_token(SECRET)
    assert ops is not None
    assert ops.client_id == "ops"
    assert ops.scopes == ["executes_action"]
    assert ops.resource == "https://mcp.example.com/mcp"

    ci = await verifier.verify_token(f"{SECRET}2")
    assert ci is not None and ci.client_id == "ci"

    assert await verifier.verify_token("nope") is None
    assert await verifier.verify_token("") is None


def test_cli_defaults_to_stdio() -> None:
    args = parse_args([])
    assert args.transport == "stdio"
    args = parse_args(["--transport", "http", "--port", "9000"])
    assert (args.transport, args.host, args.port) == ("http", "127.0.0.1", 9000)
